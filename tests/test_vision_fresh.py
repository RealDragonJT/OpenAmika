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
WANTED_FUNCS = {"_vision_fresh_desc", "_vision_take", "_vision_camera_left", "vision_suffix_for"}
WANTED_NAMES = {"VISION_EXPIRE_SEC", "_vision_desc", "_vision_desc_ts", "_vision_lang", "_vision_camera_clients",
                "_vision_stale", "_VISION_SUFFIX_TEMPLATES"}


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
        self.g["_vision_camera_clients"] += 1

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
