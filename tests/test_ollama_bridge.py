"""contrib/ollama-bridge: Ollama's API in front of a local OpenAI-compatible server.

The bridge exists so that OpenAmika's own Ollama transport works unchanged against vLLM, so most of these tests go
through that transport (brain/f_ollama.py: discover, OllamaClient) with the real `requests`, a bridge on a free
loopback port and a small stand-in for the model server behind it. A change to what f_ollama asks of Ollama fails
here. Offline: loopback only.
"""
import contextlib
import http.client
import io
import json
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "contrib" / "ollama-bridge"))
import ollama_bridge as bridge                                                  # noqa: E402
from f_ollama import OllamaClient, discover, ollama_role_config                 # noqa: E402

MODEL = "Gemma-4-26B"


def chunk(content=None, reasoning=None, finish=None, usage=None):
    """One chat-completions stream chunk as vLLM writes it."""
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    out = {"id": "chatcmpl-1", "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        out = {"id": "chatcmpl-1", "model": MODEL, "choices": [], "usage": usage}
    return out


class ModelServer:
    """A stand-in OpenAI-compatible server: it keeps what it was asked and answers from a script."""

    def __init__(self):
        self.asked, self.headers = [], []
        self.chunks, self.status, self.error = [], 200, None
        self.end_marker, self.endless = True, False
        self.whole = None                       # a whole (not streamed) answer to give as it is
        self.wait = 0.0                         # seconds before the first byte of an answer
        self.hung_up = threading.Event()
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "max_model_len": 24576}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stand_in.asked.append(body)
                stand_in.headers.append({name.lower(): value for name, value in self.headers.items()})
                time.sleep(stand_in.wait)
                if stand_in.status != 200:
                    return self.send(stand_in.status, {"error": {"message": stand_in.error, "type": "BadRequestError"}})
                if not body.get("stream") and stand_in.whole is not None:
                    return self.send(200, stand_in.whole)
                if not body.get("stream"):
                    text = "".join(c["choices"][0]["delta"].get("content", "") for c in stand_in.chunks if c["choices"])
                    usage = next((c["usage"] for c in stand_in.chunks if c.get("usage")), {})
                    return self.send(200, {"model": MODEL, "usage": usage, "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for item in stand_in.chunks:
                        self.wfile.write(b"data: " + json.dumps(item).encode() + b"\n\n")
                        self.wfile.flush()
                    while stand_in.endless:
                        self.wfile.write(b"data: " + json.dumps(chunk("more ")).encode() + b"\n\n")
                        self.wfile.flush()
                        time.sleep(0.02)
                    if stand_in.end_marker:
                        self.wfile.write(b"data: [DONE]\n\n")
                except OSError:
                    stand_in.hung_up.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.endless = False
        self.server.shutdown()
        self.server.server_close()


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.model_server = ModelServer()
        self.bridge = bridge.make_server(self.model_server.url, "127.0.0.1:0")
        self.url = "http://127.0.0.1:%d" % self.bridge.server_address[1]
        threading.Thread(target=self.bridge.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.model_server.close)
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)

    def client(self):
        """OpenAmika's own Ollama client, pointed at the bridge, with the models its own discovery found."""
        found = discover(requests, self.url)
        self.ollama = {"base_url": self.url, "num_ctx": 16384, "keep_alive": "30m", "models": found["models"]}
        self.receipts = []
        return OllamaClient(requests, lambda request: "h1", lambda handle, receipt: self.receipts.append(receipt),
                            base_url=self.url)


class ThroughOpenAmikaTests(BridgeCase):
    def test_discovery_finds_the_served_model_with_its_window_and_the_thinking_switch(self):
        found = discover(requests, self.url)
        self.assertIn("ollama-bridge", found["version"])
        self.assertEqual([(m["name"], m["context_length"], m["thinking"], m["vision"]) for m in found["models"]],
                         [(MODEL, 24576, True, False)])

    def test_a_streamed_turn_arrives_whole_with_its_token_counts(self):
        client = self.client()
        self.model_server.chunks = [chunk('[casual_happy]"Hel'), chunk('lo."'), chunk(finish="stop"),
                                    chunk(usage={"prompt_tokens": 812, "completion_tokens": 9, "total_tokens": 821})]
        config = ollama_role_config(self.ollama, MODEL, "deep", reasoning_enabled=False)
        text = "".join(client.stream(config, [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                     metadata={"phase": "deep", "turn_id": "t1"}))
        self.assertEqual(text, '[casual_happy]"Hello."')
        receipt = self.receipts[0]
        self.assertEqual((receipt["status"], receipt["finish_reason"], receipt["truncated"]), ("complete", "stop", False))
        self.assertEqual(receipt["usage"]["tokens"]["total_input_tokens"], 812)
        self.assertEqual(receipt["usage"]["tokens"]["output_tokens"], 9)
        asked = self.model_server.asked[0]
        self.assertEqual(asked["messages"], [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
        self.assertEqual((asked["model"], asked["max_tokens"], asked["stream"]), (MODEL, 1500, True))
        self.assertEqual(asked["chat_template_kwargs"], {"enable_thinking": False})     # a spoken role does not think
        self.assertNotIn("authorization", self.model_server.headers[0])                 # there is no key to send

    def test_reflection_may_think_and_the_thought_is_not_part_of_her_words(self):
        client = self.client()
        self.model_server.chunks = [chunk(reasoning="He sounds tired. "), chunk(reasoning="Keep it short."),
                                    chunk("Ask about his day."), chunk(finish="stop"),
                                    chunk(usage={"prompt_tokens": 100, "completion_tokens": 20})]
        config = ollama_role_config(self.ollama, MODEL, "reflection", reasoning_enabled=True)
        text = "".join(client.stream(config, [{"role": "user", "content": "u"}], metadata={"phase": "reflection"}))
        self.assertEqual(text, "Ask about his day.")
        self.assertEqual(self.model_server.asked[0]["chat_template_kwargs"], {"enable_thinking": True})

    def test_the_memory_writer_gets_json_mode_and_one_whole_answer(self):
        client = self.client()
        self.model_server.chunks = [chunk('{"facts": []}'), chunk(usage={"prompt_tokens": 50, "completion_tokens": 5})]
        config = ollama_role_config(self.ollama, MODEL, "memory_write")
        result = client.complete(config, [{"role": "system", "content": "reply in JSON"}, {"role": "user", "content": "u"}],
                                 metadata={"phase": "memory_write"})
        self.assertEqual(result["text"], '{"facts": []}')
        asked = self.model_server.asked[0]
        self.assertEqual((asked["stream"], asked["response_format"]), (False, {"type": "json_object"}))
        self.assertNotIn("stream_options", asked)
        self.assertEqual(self.receipts[0]["usage"]["tokens"]["output_tokens"], 5)

    def test_an_answer_cut_at_the_token_cap_is_marked_truncated(self):
        client = self.client()
        self.model_server.chunks = [chunk("and then"), chunk(finish="length"),
                                    chunk(usage={"prompt_tokens": 10, "completion_tokens": 400})]
        config = ollama_role_config(self.ollama, MODEL, "surface")
        "".join(client.stream(config, [{"role": "user", "content": "u"}], metadata={"phase": "surface"}))
        self.assertEqual((self.receipts[0]["finish_reason"], self.receipts[0]["truncated"]), ("length", True))

    def test_a_refusal_by_the_model_server_reaches_openamika_with_its_reason(self):
        client = self.client()
        self.model_server.status, self.model_server.error = 400, "This model's maximum context length is 24576 tokens"
        config = ollama_role_config(self.ollama, MODEL, "deep")
        with self.assertRaises(Exception) as raised:
            list(client.stream(config, [{"role": "user", "content": "u"}], metadata={"phase": "deep"}))
        self.assertIn("HTTP 400", str(raised.exception))
        self.assertIn("maximum context length is 24576", str(raised.exception))

    def test_an_answer_that_breaks_off_is_an_error_and_not_a_reply(self):
        client = self.client()
        self.model_server.chunks, self.model_server.end_marker = [chunk("I was about to")], False
        config = ollama_role_config(self.ollama, MODEL, "deep")
        with self.assertRaises(Exception):
            list(client.stream(config, [{"role": "user", "content": "u"}], metadata={"phase": "deep"}))
        self.assertNotEqual(self.receipts[0]["status"], "complete")

    def test_hanging_up_stops_the_model_server(self):
        self.model_server.chunks, self.model_server.endless = [chunk("first ")], True
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=10)
        connection.request("POST", "/api/chat", body=json.dumps(
            {"model": MODEL, "messages": [{"role": "user", "content": "u"}]}), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(json.loads(response.readline())["message"]["content"], "first ")
        connection.close()                      # Unlock, a new message, the page closed
        self.assertTrue(self.model_server.hung_up.wait(5), "the model server was left generating")


class WhenTheModelServerFailsTests(BridgeCase):
    """What the review of 2026-11-20 found: three ways a failing server passed for a finished reply or broke the
    bridge itself."""

    def chat(self, **more):
        printed = io.StringIO()
        with contextlib.redirect_stderr(printed):
            answer = requests.post(self.url + "/api/chat", timeout=5, stream=True, json=dict(
                {"model": MODEL, "messages": [{"role": "user", "content": "u"}]}, **more))
            lines = [json.loads(line) for line in answer.iter_lines() if line]
        return answer, lines, printed.getvalue()

    def test_an_error_object_in_the_middle_of_an_answer_is_an_error_and_not_its_end(self):
        """Some servers write {"object": "error", ...} and then "[DONE]". What came before is not a finished reply."""
        self.model_server.chunks = [chunk("Hel"), {"object": "error", "message": "CUDA out of memory", "code": 500}]
        answer, lines, printed = self.chat()
        self.assertEqual(lines[0]["message"]["content"], "Hel")
        self.assertEqual(lines[-1], {"error": "CUDA out of memory"})
        self.assertFalse(any(line.get("done") for line in lines), "no line says the reply is finished")
        self.assertIn("CUDA out of memory", printed)
        client = self.client()                  # and OpenAmika does not book the fragment as something she said
        config = ollama_role_config(self.ollama, MODEL, "deep")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Exception):
            list(client.stream(config, [{"role": "user", "content": "u"}], metadata={"phase": "deep"}))
        self.assertNotEqual(self.receipts[0]["status"], "complete")

    def test_a_whole_answer_with_no_reply_in_it_is_an_error(self):
        for whole, reason in (({"model": MODEL, "choices": [], "usage": {}}, "no reply in it"),
                              ({"object": "error", "message": "the model is not loaded"}, "the model is not loaded"),
                              ({"error": {"message": "out of memory"}}, "out of memory")):
            self.model_server.whole = whole
            answer, lines, _ = self.chat(stream=False)
            self.assertEqual(answer.status_code, 502, whole)
            self.assertIn(reason, lines[0]["error"])
        self.model_server.whole = {"model": MODEL, "choices": [{"index": 0, "finish_reason": "stop",
                                                                 "message": {"role": "assistant", "content": ""}}]}
        answer, lines, _ = self.chat(stream=False)      # a reply that is empty is still a reply
        self.assertEqual((answer.status_code, lines[0]["done"], lines[0]["message"]["content"]), (200, True, ""))

    def test_a_token_count_that_is_not_a_number_does_not_break_the_answer(self):
        self.model_server.chunks = [chunk("Hi"), chunk(finish="stop"),
                                    chunk(usage={"prompt_tokens": "many", "completion_tokens": {"n": 2}})]
        answer, lines, printed = self.chat()
        self.assertEqual([line.get("done") for line in lines], [False, True])
        self.assertEqual((lines[-1]["prompt_eval_count"], lines[-1]["eval_count"]), (0, 0))
        self.assertEqual(printed, "")
        self.assertEqual([bridge.count(v) for v in (7, "8", None, -3, "x", [1], 2.9, float("inf"))], [7, 8, 0, 0, 0, 0, 2, 0])

    def test_an_answer_that_is_only_the_end_marker_is_an_error(self):
        self.model_server.chunks = []               # the server writes "[DONE]" and nothing before it
        answer, lines, _ = self.chat()
        self.assertIn("no reply in it", lines[-1]["error"])
        self.assertFalse(any(line.get("done") for line in lines))

    def test_the_idle_limit_is_not_the_patience_with_an_answer(self):
        """The limit is for a caller that sends nothing. A model server that thinks before its first word, and a
        caller that reads a long answer slowly, both take longer than that and must not be cut off."""
        with mock.patch.object(bridge.Handler, "timeout", 0.3):
            self.model_server.wait = 1.0
            self.model_server.chunks = [chunk("Hi"), chunk(finish="stop")]
            answer, lines, _ = self.chat()
            self.assertEqual([line.get("done") for line in lines], [False, True], "a slow first word")
            for stream in (False,):
                answer, lines, _ = self.chat(stream=stream)
                self.assertEqual((answer.status_code, lines[0]["done"]), (200, True), "a slow whole answer")
            self.model_server.wait = 0.0
            self.model_server.chunks = [chunk("word " * 40) for _ in range(3000)] + [chunk(finish="stop")]
            caller = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=20)
            self.addCleanup(caller.close)
            caller.request("POST", "/api/chat", body=json.dumps(
                {"model": MODEL, "messages": [{"role": "user", "content": "u"}]}), headers={"Content-Type": "application/json"})
            response = caller.getresponse()
            first = json.loads(response.readline())
            time.sleep(1.0)                         # the caller stops reading for longer than the idle limit
            rest = [json.loads(line) for line in response.read().splitlines() if line.strip()]
            self.assertEqual(first["message"]["content"], "word " * 40)
            self.assertEqual(len(rest), 3000, "every line of a long answer arrives")
            self.assertTrue(rest[-1]["done"])

    def test_a_caller_that_says_nothing_gives_its_thread_back(self):
        self.assertTrue(0 < bridge.Handler.timeout <= 120)
        with mock.patch.object(bridge.Handler, "timeout", 0.3):
            idle = socket.create_connection(("127.0.0.1", self.bridge.server_address[1]), timeout=5)
            try:
                self.assertEqual(idle.recv(1), b"", "the bridge closes a connection that sends nothing")
            finally:
                idle.close()


class OllamaShapeTests(BridgeCase):
    def get(self, path, **headers):
        return requests.get(self.url + path, headers=headers, timeout=5)

    def test_the_lines_of_a_stream_are_ollamas(self):
        self.model_server.chunks = [chunk(reasoning="hm"), chunk("Hi"), chunk(finish="stop"),
                                    chunk(usage={"prompt_tokens": 7, "completion_tokens": 2})]
        response = requests.post(self.url + "/api/chat", json={
            "model": MODEL, "messages": [{"role": "user", "content": "u"}], "keep_alive": "30m",
            "options": {"num_ctx": 16384, "num_predict": 64}}, stream=True, timeout=5)
        self.assertTrue(response.headers["Content-Type"].startswith("application/x-ndjson"))
        lines = [json.loads(line) for line in response.iter_lines() if line]
        self.assertEqual([line["done"] for line in lines], [False, False, True])
        self.assertEqual(lines[0]["message"], {"role": "assistant", "content": "", "thinking": "hm"})
        self.assertEqual(lines[1]["message"], {"role": "assistant", "content": "Hi"})
        self.assertEqual((lines[2]["done_reason"], lines[2]["prompt_eval_count"], lines[2]["eval_count"]), ("stop", 7, 2))
        asked = self.model_server.asked[0]
        self.assertEqual(asked["max_tokens"], 64)
        self.assertEqual(asked["stream_options"], {"include_usage": True})
        for absent in ("num_ctx", "keep_alive", "options", "chat_template_kwargs", "response_format"):
            self.assertNotIn(absent, asked)     # nothing the request did not ask for, nothing the server cannot use

    def test_tags_show_and_the_loaded_list(self):
        self.assertEqual([m["name"] for m in self.get("/api/tags").json()["models"]], [MODEL])
        self.assertEqual([m["name"] for m in self.get("/api/ps").json()["models"]], [MODEL])
        shown = requests.post(self.url + "/api/show", json={"model": MODEL}, timeout=5).json()
        self.assertEqual(shown["capabilities"], ["completion", "thinking"])
        self.assertEqual(shown["model_info"]["openai.context_length"], 24576)
        missing = requests.post(self.url + "/api/show", json={"model": "nope"}, timeout=5)
        self.assertEqual((missing.status_code, missing.json()["error"]), (404, "model 'nope' not found"))

    def test_the_thinking_switch_can_be_left_out(self):
        self.bridge.think_switch = False
        shown = requests.post(self.url + "/api/show", json={"model": MODEL}, timeout=5).json()
        self.assertEqual(shown["capabilities"], ["completion"])
        self.assertEqual(discover(requests, self.url)["models"][0]["thinking"], False)

    def test_a_web_page_cannot_use_it(self):
        refused = self.get("/api/tags", Origin="https://example.com")
        self.assertEqual((refused.status_code, refused.json()["error"]), (403, "Requests from web pages are refused"))
        renamed = self.get("/api/tags", Host="rebound.example.com")
        self.assertEqual(renamed.status_code, 403)
        self.assertEqual(self.get("/api/tags", Host="localhost:11435").status_code, 200)

    def test_what_it_does_not_answer_says_so(self):
        for path in ("/api/pull", "/api/generate", "/v1/models"):
            answer = requests.post(self.url + path, json={}, timeout=5)
            self.assertEqual(answer.status_code, 404, path)
            self.assertIn("does not answer", answer.json()["error"])
        self.assertEqual(self.get("/").text, "Ollama is running")

    def test_a_caller_that_drops_its_connection_is_not_reported_as_a_failure(self):
        for error, reported in ((ConnectionResetError(10054, "reset"), False), (ValueError("a real fault"), True)):
            printed = io.StringIO()
            with contextlib.redirect_stderr(printed):
                try:
                    raise error
                except Exception:
                    self.bridge.handle_error(None, ("127.0.0.1", 1))
            self.assertEqual(bool(printed.getvalue()), reported, type(error).__name__)

    def test_without_a_model_server_the_bridge_does_not_pass_for_a_running_ollama(self):
        self.model_server.close()
        printed = io.StringIO()
        with contextlib.redirect_stderr(printed):
            answer = self.get("/api/version")       # all that OpenAmika's status line and pre-flight ask
        self.assertEqual(answer.status_code, 502)
        self.assertIn("is not answering", answer.json()["error"])
        self.assertIn("/api/version -> 502", printed.getvalue())    # OpenAmika shows no reason, so the bridge does
        chat = requests.post(self.url + "/api/chat", timeout=5, json={
            "model": MODEL, "messages": [{"role": "user", "content": "u"}]})
        self.assertEqual(chat.status_code, 502)
        with self.assertRaises(Exception):
            discover(requests, self.url)


class LocalOnlyTests(unittest.TestCase):
    def test_this_machine_and_private_networks_are_accepted(self):
        for url, kept in (("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
                          ("http://localhost:8000", "http://localhost:8000/v1"),
                          ("http://192.168.1.20:8000/v1/", "http://192.168.1.20:8000/v1"),
                          ("http://10.0.0.5:8080/api/v1", "http://10.0.0.5:8080/api/v1"),
                          ("http://100.64.0.7:8000/v1", "http://100.64.0.7:8000/v1")):
            self.assertEqual(bridge.Backend(url).url, kept)

    def test_public_addresses_keys_and_other_schemes_are_refused(self):
        for url in ("http://8.8.8.8:8000/v1", "https://127.0.0.1:8000/v1", "http://user:secret@127.0.0.1:8000/v1",
                    "http://127.0.0.1:8000/v1?api_key=secret", "127.0.0.1:8000", "", "http://127.0.0.1:notaport/v1"):
            with self.assertRaises(ValueError, msg=url):
                bridge.Backend(url)

    def test_an_address_is_checked_again_at_every_call(self):
        backend = bridge.Backend("http://127.0.0.1:9/v1")
        backend.host = "8.8.8.8"                # as if the name had come to stand for a public address
        with self.assertRaises(bridge.BridgeError) as raised:
            backend.request("GET", "/models")
        self.assertIn("only talks to local servers", raised.exception.message)


class EveryAddressOfANameTests(BridgeCase):
    """On Windows "localhost" stands for ::1 before 127.0.0.1, and the recipes start the model server on 127.0.0.1:
    the bridge used the first address only and reported a running server as not answering."""

    def record(self):
        tried = []
        real = http.client.HTTPConnection

        class Recording(real):
            def __init__(self, host, *args, **kwargs):
                tried.append(host)
                super().__init__(host, *args, **kwargs)

        patch = mock.patch.object(bridge.http.client, "HTTPConnection", Recording)
        patch.start()
        self.addCleanup(patch.stop)
        return tried

    def test_a_name_whose_first_address_refuses_is_reached_at_the_next_and_then_at_once(self):
        backend, tried = self.bridge.backend, self.record()
        with mock.patch.object(bridge, "local_addresses", return_value=["127.0.0.2", "127.0.0.1", "127.0.0.1"]):
            self.assertEqual([row["id"] for row in backend.models()], [MODEL])
            self.assertEqual(tried, ["127.0.0.2", "127.0.0.1"])         # nothing listens on the first
            del tried[:]
            self.assertEqual([row["id"] for row in backend.models()], [MODEL])
            self.assertEqual(tried, ["127.0.0.1"], "the address that answered is tried first from then on")

    def test_ipv4_is_tried_before_ipv6(self):
        backend, tried = self.bridge.backend, self.record()
        with mock.patch.object(bridge, "local_addresses", return_value=["::1", "127.0.0.1"]):
            self.assertEqual([row["id"] for row in backend.models()], [MODEL])
        self.assertEqual(tried, ["127.0.0.1"])

    def test_when_no_address_answers_the_reason_is_the_last_refusal(self):
        self.model_server.close()
        with self.assertRaises(bridge.BridgeError) as raised:
            self.bridge.backend.models()
        self.assertEqual(raised.exception.status, 502)
        self.assertIn("is not answering", raised.exception.message)


class RequestTests(unittest.TestCase):
    def test_what_cannot_be_carried_is_refused(self):
        for body in ({}, {"model": MODEL}, {"model": MODEL, "messages": []},
                     {"model": MODEL, "messages": [{"role": "user", "content": [{"type": "image"}]}]}):
            with self.assertRaises(bridge.BridgeError) as raised:
                bridge.chat_request(body)
            self.assertEqual(raised.exception.status, 400)

    def test_a_schema_format_becomes_a_json_schema(self):
        schema = {"type": "object", "properties": {"facts": {"type": "array"}}}
        asked = bridge.chat_request({"model": MODEL, "messages": [{"role": "user", "content": "u"}], "format": schema})
        self.assertEqual(asked["response_format"]["json_schema"]["schema"], schema)


if __name__ == "__main__":
    unittest.main()
