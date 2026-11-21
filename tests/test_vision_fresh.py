"""Her eyes close when the camera does (speech/api.py).

The vision service keeps answering GET /api/latest with its last description after the frames stop. The gateway
must not go on adding that scene to his turns once no camera is connected. The few functions involved are taken out
of api.py by AST, so the speech stack (torch, funasr, FastAPI) is never imported.
"""
import ast
import time
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "speech" / "api.py").read_text(encoding="utf-8")
WANTED_FUNCS = {"_vision_fresh_desc", "_vision_take", "_vision_camera_joined", "_vision_camera_left", "vision_suffix_for"}
WANTED_NAMES = {"VISION_EXPIRE_SEC", "VISION_STALE_GRACE_SEC", "_vision_desc", "_vision_desc_ts", "_vision_lang",
                "_vision_camera_clients", "_vision_stale", "_vision_camera_since", "_VISION_SUFFIX_TEMPLATES"}


def load():
    chunks = []
    for node in ast.parse(SRC).body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            chunks.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(getattr(t, "id", None) in WANTED_NAMES for t in targets):
                chunks.append(ast.get_source_segment(SRC, node))
    scope = {"time": time, "VLM_URL": "http://127.0.0.1:8014"}
    exec("\n\n".join(chunks), scope)
    return scope


class EyesFollowTheCamera(unittest.TestCase):
    def setUp(self):
        self.g = load()

    def connect(self):
        self.g["_vision_camera_joined"]()

    def later(self, seconds):
        """The camera has been on for `seconds` longer (the clock is not waited for: the moment it joined moves back)."""
        self.g["_vision_camera_since"] -= seconds

    def test_nothing_is_seen_before_a_camera_connects(self):
        self.g["_vision_take"]("a desk and a lamp")
        self.assertEqual(self.g["_vision_fresh_desc"](), "")
        self.assertEqual(self.g["vision_suffix_for"]("hello"), "")

    def test_a_description_counts_while_the_camera_is_on(self):
        self.connect()
        self.g["_vision_take"]("a desk and a lamp")
        self.assertEqual(self.g["_vision_fresh_desc"](), "a desk and a lamp")
        self.assertIn("a desk and a lamp", self.g["vision_suffix_for"]("hello"))

    def test_camera_off_ends_it_though_the_service_repeats_its_last_description(self):
        self.connect()
        self.g["_vision_take"]("three people at a table")
        self.g["_vision_camera_left"]()
        for _ in range(3):                                  # the poll loop keeps getting the same answer
            self.g["_vision_take"]("three people at a table")
        self.assertEqual(self.g["_vision_fresh_desc"](), "")
        self.assertEqual(self.g["vision_suffix_for"]("hello"), "")

    def test_a_returning_camera_does_not_bring_the_old_scene_back(self):
        self.connect()
        self.g["_vision_take"]("three people at a table")
        self.g["_vision_camera_left"]()
        self.connect()
        self.g["_vision_take"]("three people at a table")   # the service has not seen a new frame yet
        self.assertEqual(self.g["_vision_fresh_desc"](), "")
        self.g["_vision_take"]("an empty room")
        self.assertEqual(self.g["_vision_fresh_desc"](), "an empty room")

    def test_a_camera_that_returns_to_the_same_scene_is_seen_again(self):
        """The service describes a frame only when the picture has changed: back on the same scene it goes on saying
        what it said. That is held back for a moment, not for good, or she would be blind with the camera on."""
        self.connect()
        self.g["_vision_take"]("a desk and a lamp")
        self.g["_vision_camera_left"]()
        self.connect()
        self.g["_vision_take"]("a desk and a lamp")
        self.assertEqual(self.g["_vision_fresh_desc"](), "", "held back right after the camera came back")
        self.later(self.g["VISION_STALE_GRACE_SEC"] + 0.1)
        self.g["_vision_take"]("a desk and a lamp")          # the next poll: still the same text, so the same scene
        self.assertEqual(self.g["_vision_fresh_desc"](), "a desk and a lamp")
        self.assertIn("a desk and a lamp", self.g["vision_suffix_for"]("hello"))

    def test_a_description_that_arrived_after_the_camera_left_is_old_too(self):
        """The service may finish describing the last frame after the camera went off: that text is of the old
        picture as well, and it is the one the service repeats when a camera returns."""
        self.connect()
        self.g["_vision_take"]("three people at a table")
        self.g["_vision_camera_left"]()
        self.g["_vision_take"]("three people leaving the table")     # polled with no camera on
        self.assertEqual(self.g["_vision_fresh_desc"](), "")
        self.connect()
        self.g["_vision_take"]("three people leaving the table")
        self.assertEqual(self.g["_vision_fresh_desc"](), "", "the late description is not taken for a new one")
        self.g["_vision_take"]("an empty room")
        self.assertEqual(self.g["_vision_fresh_desc"](), "an empty room")

    def test_what_the_service_said_before_any_camera_is_held_back_at_the_first_one(self):
        self.g["_vision_take"]("yesterday's room")            # left over in the service from an earlier run
        self.connect()
        self.g["_vision_take"]("yesterday's room")
        self.assertEqual(self.g["_vision_fresh_desc"](), "")
        self.g["_vision_take"]("a face close to the camera")
        self.assertEqual(self.g["_vision_fresh_desc"](), "a face close to the camera")

    def test_the_camera_socket_and_the_poll_loop_use_these_functions(self):
        """The wiring: the socket counts cameras through the two functions above, the poll loop hands every answer
        to _vision_take, and nothing else writes the description."""
        tree = ast.parse(SRC)
        funcs = {n.name: ast.get_source_segment(SRC, n) for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("_vision_camera_joined()", funcs["ws_camera"])
        self.assertIn("_vision_camera_left()", funcs["ws_camera"])
        self.assertNotIn("_vision_camera_clients", funcs["ws_camera"])
        self.assertIn("_vision_take(desc)", funcs["_vision_poll_loop"])
        writers = [name for name, src in funcs.items()
                   if any(line.strip().startswith("_vision_desc =") for line in src.splitlines())]
        self.assertEqual(sorted(writers), ["_vision_camera_left", "_vision_take"])

    def test_one_of_two_cameras_leaving_changes_nothing(self):
        self.connect()
        self.connect()
        self.g["_vision_take"]("a desk and a lamp")
        self.g["_vision_camera_left"]()
        self.assertEqual(self.g["_vision_fresh_desc"](), "a desk and a lamp")

    def test_a_description_still_expires_when_the_service_stops_answering(self):
        self.connect()
        self.g["_vision_take"]("a desk and a lamp")
        self.g["_vision_desc_ts"] -= self.g["VISION_EXPIRE_SEC"] + 1
        self.assertEqual(self.g["_vision_fresh_desc"](), "")


if __name__ == "__main__":
    unittest.main()
