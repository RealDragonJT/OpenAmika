"""Single text reply: real turn runner, fake gated streams and a manually advanced clock."""
import ast
import copy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from _po_support import extract_webui, prepare_prompt_namespace
from f_text_reply import ReplyCancel
import test_f_bridge


@pytest.fixture(scope="module")
def namespace():
    return prepare_prompt_namespace(extract_webui())


class Turn:
    def __init__(self, namespace, mode="think", reply="Let me think.", *, enabled=True,
                 local=False, surface_error=False, deep_error=False, raw=None, interrupt_on_chunk=False):
        self.ns = dict(namespace)
        # Extracted functions refer to their original namespace: isolate a fresh globals dict.
        import types
        for key, value in list(self.ns.items()):
            if isinstance(value, types.FunctionType) and value.__globals__ is namespace:
                self.ns[key] = types.FunctionType(value.__code__, self.ns, value.__name__, value.__defaults__, value.__closure__)
        self.now = 0
        self.started = {p: threading.Event() for p in ("surface", "deep")}
        self.release = {p: threading.Event() for p in self.started}
        self.generated = threading.Event()
        self.settled = threading.Event()
        self.events, self.calls, self.tools = [], [], []
        self.cancel = threading.Event()
        self.entry = {"cancel": self.cancel, "single_reply": enabled}
        self.history = [{"turn_id": "text-1", "f_user_text": "Question", "user": "Question",
                         "ts": "2026-01-02 12:00:00", "surface_reply": "", "deep_raw": ""}]
        self.receipt = None
        self.deep_cancel = None
        surface_raw = raw if raw is not None else f"status: ok\ntone: casual_neutral\nmode: {mode}\nreply: {reply}"

        def stream(parts, config, request_cancel=None):
            phase = config["phase"]
            self.calls.append((phase, self.now, copy.deepcopy(parts)))
            self.started[phase].set()
            assert self.release[phase].wait(5), f"{phase} test gate timed out"
            if phase == "surface":
                if surface_error:
                    raise RuntimeError("surface offline")
                yield surface_raw
            else:
                try:
                    if deep_error:
                        raise RuntimeError("deep offline")
                    yield '[casual_neutral]"Full answer."\n我心想：private'
                    self.generated.set()
                finally:
                    self.receipt = request_cancel.finish_status("unknown" if deep_error else "complete") if request_cancel else "complete"
                    self.settled.set()

        def prepare(parts, config, request_cancel):
            self.deep_cancel = request_cancel
            return lambda: stream(parts, config, request_cancel)

        def emit(event, payload, **kwargs):
            self.events.append((event, copy.deepcopy(payload), self.now))
            if interrupt_on_chunk and event == "deep_chunk":
                self.entry["reason"] = "interrupt"
                self.cancel.set()

        def surface(tid, reply, status, tone):
            self.history[0].update(surface_reply=reply, surface_status=status, surface_tone=tone)

        def deep(tid, raw):
            self.history[0]["deep_raw"] = raw

        self.ns.update(socketio=SimpleNamespace(emit=emit), _pick_stream=stream, _prepare_text_deep=prepare,
                       AVAILABLE_MODELS={p: {"phase": p, "provider": "ollama" if local else "openrouter"} for p in self.started},
                       _find_turn=lambda tid: self.history[0] if self.history else None,
                       build_surface_messages=lambda *a, **kw: {"role": "surface"},
                       build_messages=lambda *a, **kw: {"role": "deep", **kw},
                       _with_round_context=lambda p, c: p, _with_capabilities=lambda p: p,
                       update_turn_surface=surface, update_turn_deep=deep,
                       f_mark_complete=lambda tid: self.history[0].update(f_generation_complete=True),
                       discard_turn=lambda tid: self.history.clear(),
                       _tools_after_turn=lambda *args: self.tools.append(args), _describe_provider_error=lambda e: None,
                       _save_history=lambda: None, _CURRENT_CHANNEL="discord", _voice_pending={}, _ws_active={"text-1": self.entry})
        self.thread = threading.Thread(target=self.ns["_ws_run_generation"],
                                       args=("sid", "text-1", "Question", "surface", "deep", self.entry))
        self.thread.start()
        assert self.started["surface"].wait(5)
        if enabled and not local:
            assert self.started["deep"].wait(5)

    def finish(self):
        for gate in self.release.values():
            gate.set()
        self.thread.join(5)
        assert not self.thread.is_alive()
        if self.started["deep"].is_set():
            assert self.settled.wait(5)
        return self

    def replies(self):
        return [d.get("reply") if e == "surface_done" else d.get("speech")
                for e, d, _ in self.events if e in ("surface_done", "deep_done") and (d.get("reply") or d.get("speech"))]


def test_say_keeps_turn_and_cancels_only_deep(namespace):
    t = Turn(namespace, mode="say", reply="Hey!")
    t.release["surface"].set()
    t.thread.join(5)  # the reply must not wait for the deep's blocked read
    assert not t.thread.is_alive()
    assert t.replies() == ["Hey!"]
    assert not t.cancel.is_set() and t.deep_cancel.is_set()
    t.finish()
    assert t.receipt == "cancelled"
    assert t.history[0]["surface_reply"] == "Hey!" and not t.history[0]["deep_raw"]
    assert not any(e.startswith("deep_") for e, _, _ in t.events)


@pytest.mark.parametrize("mode", ["think", "say+think"])
def test_think_has_no_surface_in_history_tools_or_memory(namespace, mode):
    t = Turn(namespace, mode=mode).finish()
    assert t.replies() == ["Full answer."]
    assert t.history[0]["surface_reply"] == ""
    assert t.tools[0][2] == "" and t.tools[0][3]["speech"] == "Full answer."
    assert next(parts for phase, _, parts in t.calls if phase == "deep")["blurted_reply"] is None
    assert t.ns["_voice_pending"] == {}
    # Real bridge archive: nothing may be remembered before the transport's acknowledgements.
    fixture = test_f_bridge.BridgeTests()
    fixture.setUp()
    try:
        fixture.install()
        fixture.g["_parsed_deep"] = lambda raw: (t.ns["parse_response"](raw)[0]["speech"], "")
        fixture.history.extend(t.history)
        fixture.g["f_archive_turn"]("text-1")
        assert fixture.g["F_MEMORY"].status()["events"] == 0
        fixture.g["_protect_latest"]()
        assert fixture.g["F_MEMORY"].status()["events"] == 0
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(test_f_bridge.threading, "Thread", test_f_bridge.InlineThread)
            fixture.socket.handlers["speech_done"]({"turn_id": "text-1"})
        assert fixture.g["F_MEMORY"].model.events[0]["assistant"] == "Full answer."
    finally:
        fixture.g["F_MEMORY"].close()
        fixture.tearDown()


def test_parallel_latency_is_deep_alone(namespace):
    t = Turn(namespace)
    assert [(p, ts) for p, ts, _ in t.calls] == [("deep", 0), ("surface", 0)]
    t.now = 2
    t.release["surface"].set()
    assert t.deep_cancel.verdict.wait(5)
    t.now = 5
    t.finish()
    assert [ts for e, _, ts in t.events if e == "deep_done"] == [5]  # not surface 2 + deep 5


@pytest.mark.parametrize("mode", ["think", "say"])
def test_fast_deep_is_held_until_verdict(namespace, mode):
    t = Turn(namespace, mode=mode, reply="Hey!")
    t.now = 1
    t.release["deep"].set()
    assert t.generated.wait(5)
    assert not any(e.startswith("deep_") for e, _, _ in t.events)
    assert not t.history[0]["deep_raw"] and not t.settled.is_set()
    t.now = 3
    t.finish()
    assert t.replies() == (["Full answer."] if mode == "think" else ["Hey!"])
    assert t.receipt == ("complete" if mode == "think" else "cancelled")


@pytest.mark.parametrize("options", [{"surface_error": True}, {"raw": "nonsense"}, {"raw": "mode: nonsense\nreply: filler"}])
def test_surface_failure_uses_deep(namespace, options):
    t = Turn(namespace, **options).finish()
    assert t.replies() == ["Full answer."]
    assert not any(e == "error" for e, _, _ in t.events)


def test_deep_failure_is_reported_only_after_think(namespace):
    t = Turn(namespace, deep_error=True)
    t.release["deep"].set()
    assert not any(e == "error" for e, _, _ in t.events)
    t.finish()
    assert t.replies() == [] and t.history == []
    assert [e for e, _, _ in t.events][-2:] == ["error", "end"]
    t = Turn(namespace, deep_error=True, mode="say", reply="Hey!").finish()
    assert t.replies() == ["Hey!"] and not any(e == "error" for e, _, _ in t.events)


@pytest.mark.parametrize("mode", ["think", "say"])
def test_ollama_is_sequential_and_one_reply(namespace, mode):
    t = Turn(namespace, mode=mode, reply="Hey!", local=True)
    assert not t.started["deep"].is_set()
    t.now = 2
    t.release["surface"].set()
    if mode == "think":
        assert t.started["deep"].wait(5)
        assert t.calls[-1][1] == 2
    t.finish()
    assert t.replies() == (["Full answer."] if mode == "think" else ["Hey!"])
    assert t.started["deep"].is_set() == (mode == "think")


def test_toggle_off_preserves_filler_and_sequential_deep(namespace):
    t = Turn(namespace, enabled=False)
    assert not t.started["deep"].is_set()
    t.finish()
    assert t.replies() == ["Let me think.", "Full answer."]
    assert t.calls[-1][2]["blurted_reply"] == "Let me think."


def test_quiet_not_for_me_and_empty_say_are_unchanged(namespace):
    for enabled in (False, True):
        t = Turn(namespace, enabled=enabled, raw="status: not-for-me\nmode: say\nreply:").finish()
        assert t.replies() == [] and not t.history[0]["deep_raw"]


def test_cancelling_turn_releases_buffer_without_recording_filler(namespace):
    t = Turn(namespace)
    t.cancel.set()
    t.finish()
    assert t.replies() == [] and t.history == [] and t.receipt == "cancelled"


@pytest.mark.parametrize("channel", ["", "discord", "telegram", "local"])
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_flag_is_scoped_to_text_channels(channel, voice, enabled):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "brain/webui.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ws_message")
    node.decorator_list = []
    calls = []
    ns = {"request": SimpleNamespace(sid="sid"), "_voice_stopped": lambda tid: None,
          "_start_message_gen": lambda *a, **kw: calls.append(kw),
          "DEFAULT_SURFACE_MODEL": "surface", "DEFAULT_DEEP_MODEL": "deep"}
    exec(compile(ast.Module([node], type_ignores=[]), "<ws_message>", "exec"), ns)
    ns["ws_message"]({"text": "Hi", "channel": channel, "voice": voice, "single_reply": enabled})
    assert calls[0]["single_reply"] == (enabled and channel in ("discord", "telegram"))


@pytest.mark.parametrize('accept', [False, True])
def test_real_openrouter_receipt_and_ledger_wait_for_verdict(accept):
    import test_f_roles
    from f_openrouter import drain_scope
    fixture = test_f_roles.RoleTests()
    fixture.setUp()
    try:
        fixture.install()
        cancel = ReplyCancel(threading.Event())
        response = fixture.data('hello', stream=True)
        fixture.http.responses.append(response)
        generated, finished, closed = threading.Event(), threading.Event(), threading.Event()
        original_close = response.close
        def close():
            original_close()
            closed.set()
        response.close = close
        errors = []
        def run():
            fixture.local.cancel = cancel
            try:
                with drain_scope(None):
                    for chunk in fixture.g['_pick_stream']({'user_content': 'Hi', '_f_phase': 'deep', '_f_turn_id': 'chosen-turn'},
                                                          fixture.g['AVAILABLE_MODELS']['or:deep']):
                        generated.set()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=run)
        worker.start()
        assert generated.wait(5)
        assert closed.wait(5)  # provider completed; accounting still waits for the verdict
        assert not finished.is_set()
        cancel.choose(accept)
        worker.join(5)
        assert not worker.is_alive() and not errors
        rows = fixture.meter.summary()['recent_requests']
        assert len(rows) == 1 and rows[0]['turn_id'] == 'chosen-turn'
        assert rows[0]['status'] == ('complete' if accept else 'cancelled')
        import json
        receipts = list((fixture.root / 'memory_f/openrouter_receipts').glob('*.json'))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text(encoding='utf-8'))
        assert receipt['status'] == rows[0]['status']
        assert receipt['raw_usage']['completion_tokens'] == 20
        assert response.closed
    finally:
        fixture.tearDown()


def test_worker_inherits_bridge_context_and_disables_drain():
    from f_openrouter import drain_scope, current_drainer
    from f_text_reply import TextReply
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'brain/f_bridge.py').read_text(encoding='utf-8'))
    install = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'install')
    node = next(n for n in install.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare_text_deep')
    local = threading.local()
    local.turn_id, local.memory, local.memory_surface = 'same-turn', 'deep memory', 'surface memory'
    local.cancel = threading.Event()
    observed = []
    def pick(parts, config):
        observed.append((dict(local.__dict__), current_drainer()))
        yield 'answer'
    ns = {'local': local, 'drain_scope': drain_scope, 'g': {'_pick_stream': pick}}
    exec(compile(ast.Module([node], type_ignores=[]), '<bridge scope>', 'exec'), ns)
    with drain_scope(object()):
        reply = TextReply(ns['prepare_text_deep'], {}, {}, local.cancel)
        reply.choose(True)
        assert list(reply.chunks()) == ['answer']
    reply.thread.join(5)
    inherited, drainer = observed[0]
    assert inherited['turn_id'] == 'same-turn' and inherited['memory'] == 'deep memory'
    assert inherited['memory_surface'] == 'surface memory' and inherited['cancel'] is reply.cancel
    assert drainer is None and local.cancel is not reply.cancel


def test_interrupt_keeps_no_unsent_deep_or_surface_text(namespace):
    t = Turn(namespace, interrupt_on_chunk=True).finish()
    assert t.history[0]['interrupted'] is True
    assert t.history[0]['surface_reply'] == '' and t.history[0]['deep_raw'] == ''
    assert t.replies() == [] and t.tools == []


def test_native_meter_marks_unselected_completed_request_cancelled():
    import test_f_bridge_usage
    fixture = test_f_bridge_usage.BridgeAccountingTests()
    fixture.setUp()
    try:
        cancel = ReplyCancel(threading.Event())
        settling = threading.Event()
        finish = cancel.finish_status
        def finish_status(status):
            settling.set()
            return finish(status)
        cancel.finish_status = finish_status
        response = test_f_bridge_usage.Response(events=[
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 100}}},
            {'type': 'content_block_delta', 'delta': {'text': 'unused'}},
            {'type': 'message_delta', 'usage': {'output_tokens': 20}},
            {'type': 'message_stop'}])
        fixture.http.responses.append(response)
        def run():
            fixture.namespace['local'].cancel = cancel
            list(fixture.namespace['stream']('anthropic', {'_f_phase': 'deep', '_f_turn_id': 'text-native'}, 'claude-sonnet-5'))
        worker = threading.Thread(target=run)
        worker.start()
        assert settling.wait(5)
        cancel.choose(False)
        worker.join(5)
        assert not worker.is_alive()
        receipt = fixture.meter.summary()['recent_requests'][0]
        assert receipt['status'] == 'cancelled' and receipt['turn_id'] == 'text-native'
        assert response.closed
    finally:
        fixture.tearDown()
