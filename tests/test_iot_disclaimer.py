"""The IoT safety statement (docs/IOT_DISCLAIMER.md) and its enforcement.

Nothing of the smart home may turn on, start or connect before the current version of the statement was accepted:
the gateway's config proxy (speech/api.py), the brain's routes and MCP hub (brain/f_tools.py), the launcher's start
plan (launch.py) and the bridge itself (servers/openamika-iot). A new version asks again. Offline: no MCP server,
MQTT broker, zigbee2mqtt or browser is started; the hub's MCP transport is faked, its IoT gate is the real one.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "brain", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import f_tools  # noqa: E402
import iot_disclaimer  # noqa: E402  (brain/iot_disclaimer.py: the version and the rules)
from test_wipe_proxy import SOURCE, FakeHTTP, HTTPException, Request, Response, Session  # noqa: E402

VERSION = iot_disclaimer.IOT_DISCLAIMER_VERSION
BRIDGE_DIR = ROOT / "servers" / "openamika-iot"


def load_by_path(name, path):
    """A fresh module from a file (the bridge package may also be installed elsewhere in this environment)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_disclaimer = load_by_path("bridge_disclaimer_under_test", BRIDGE_DIR / "openamika_iot" / "disclaimer.py")
launch = load_by_path("openamika_launch_iot", ROOT / "launch.py")


def tools_doc(iot_enabled=True, accepted=None, **servers):
    """A tools.json document: the openamika-iot bridge (+ extra servers), optionally with an acceptance record."""
    doc = {"enabled": True, "clerk_role": "surface", "limits": {}, "mcpServers": {
        "iot": {"command": "python", "args": ["-m", "openamika_iot"], "cwd": "../servers/openamika-iot",
                "env": {"AMIKA_MQTT_PORT": "1"}, "enabled": iot_enabled},
        "hermes": {"command": "python", "args": ["-m", "openamika_agents", "hermes"], "kind": "agent", "enabled": False},
        **servers}}
    if accepted:
        doc["iot_disclaimer"] = {"version": accepted, "accepted_at": "2026-11-03T20:00:00Z"}
    return doc


# ───────────────────────────── the statement itself ─────────────────────────────

def test_statement_versions_agree_everywhere():
    text = (ROOT / "docs" / "IOT_DISCLAIMER.md").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", VERSION)
    assert re.search(r"<!--\s*IOT_DISCLAIMER_VERSION:\s*" + re.escape(VERSION) + r"\s*-->", text)
    assert bridge_disclaimer.IOT_DISCLAIMER_VERSION == VERSION          # the bridge checks the same version
    assert bridge_disclaimer.ENV_VAR == iot_disclaimer.ENV_VAR
    statement = iot_disclaimer.statement()
    assert statement["version"] == VERSION
    for code in ("en", "ja", "zh"):
        blocks = statement["languages"][code]["blocks"]
        assert VERSION in json.dumps(blocks, ensure_ascii=False), code   # each language carries its version


def test_statement_has_english_japanese_and_chinese_with_the_same_shape():
    languages = iot_disclaimer.statement()["languages"]
    assert [languages[c]["label"] for c in ("en", "ja", "zh")] == ["English", "日本語", "简体中文"]
    shapes = {}
    for code, lang in languages.items():
        heads = [b for b in lang["blocks"] if b["type"] == "h"]
        lists = [len(b["items"]) for b in lang["blocks"] if b["type"] == "ul"]
        assert [h["text"][:2] for h in heads] == [f"{n}." for n in range(1, 9)], code
        shapes[code] = lists
    assert shapes["en"] == shapes["ja"] == shapes["zh"]                 # every list item is translated


def test_english_statement_says_everything_it_must():
    blocks = iot_disclaimer.statement()["languages"]["en"]["blocks"]
    text = " ".join(b.get("text", "") + " " + " ".join(b.get("items", [])) for b in blocks).lower()
    for phrase in ("large language models", "non-deterministic", "the same words can lead to a different",
                   "mishear", "misunderstand", "wrong device", "wrong time", "repeat an action, skip it",
                   "prompt injection", "models and the companies that provide them", "zigbee", "mqtt", "firmware",
                   "no guarantee that amika will not cause harm to your iot devices", "property", "your data",
                   "people or animals", "at your own risk", "you alone are responsible", "supervising amika",
                   "terms of the device makers", "laws, regulations", "mekahime inc.", "contributors",
                   "not responsible or liable for any outcome", "damage, loss or injury",
                   "maximum extent permitted by applicable law", '"as is"', "without warranty", "mit license",
                   "heaters and space heaters", "stoves", "ovens", "irons", "heating or cooking appliance",
                   "gas valves", "water valves", "pumps", "door locks", "garage doors", "gates", "alarms",
                   "security systems", "medical, life-support", "child- or pet-safety", "vehicles", "machinery",
                   "power tools", "high-power loads", "fire", "flooding", "injury", "let someone in",
                   "best-effort", "never a guarantee", "manual, physical control", "turn iot off at any time",
                   "if you do not agree, do not enable iot", "i have read and accept this"):
        assert phrase in text, phrase


def test_translations_carry_the_essentials():
    languages = iot_disclaimer.statement()["languages"]
    flat = {code: json.dumps(lang["blocks"], ensure_ascii=False) for code, lang in languages.items()}
    for phrase in ("非決定的", "プロンプトインジェクション", "保証はありません", "自己責任", "MekaHime Inc.", "責任を負いません",
                   "現状のまま", "暖房器具", "ガレージのドア", "医療機器", "手動で物理的に操作", "いつでもオフ",
                   "同意できない場合は、IoT を有効にしないでください", "英語版が優先"):
        assert phrase in flat["ja"], phrase
    for phrase in ("不确定性", "提示词注入", "不保证", "风险均由你自行承担", "MekaHime Inc.", "不承担任何责任", "按“原样”提供",
                   "取暖器", "车库门", "医疗", "手动、物理上的控制", "随时关闭 IoT", "如果你不同意，请不要开启 IoT",
                   "以英文版本为准"):
        assert phrase in flat["zh"], phrase


def test_a_statement_that_does_not_match_the_code_is_refused(tmp_path):
    good = (ROOT / "docs" / "IOT_DISCLAIMER.md").read_text(encoding="utf-8")
    wrong = tmp_path / "wrong.md"
    wrong.write_text(good.replace(f"IOT_DISCLAIMER_VERSION: {VERSION}", "IOT_DISCLAIMER_VERSION: 2000-01-01"), "utf-8")
    with pytest.raises(ValueError):
        iot_disclaimer.statement(wrong)
    partial = tmp_path / "partial.md"
    partial.write_text(good.split("<!-- iot-disclaimer:zh -->")[0], "utf-8")
    with pytest.raises(ValueError, match="zh"):
        iot_disclaimer.statement(partial)


# ───────────────────────────── what counts as IoT, and the record ─────────────────────────────

def test_smart_home_servers_are_recognised():
    smart = iot_disclaimer.is_smart_home
    assert smart("iot", {})
    assert smart("plugs", {"command": "python", "args": ["-m", "openamika_iot"]})
    assert smart("plugs", {"command": "openamika-iot"})
    assert smart("zigbee", {"kind": "iot", "url": "http://198.51.100.9:8010/mcp"})
    assert smart("home-assistant", {"url": "http://example.test/sse"})
    assert smart("hass", {})
    assert smart("house", {"url": "http://homeassistant.local:8123/mcp_server/sse"})
    assert smart("house", {"url": "http://192.0.2.20:8123/api/mcp"})
    assert not smart("hermes", {"command": "python", "args": ["-m", "openamika_agents", "hermes"], "kind": "agent"})
    assert not smart("chassis", {"url": "http://127.0.0.1:9000/mcp"})
    assert iot_disclaimer.turns_on_iot(tools_doc(iot_enabled=True))
    assert not iot_disclaimer.turns_on_iot(tools_doc(iot_enabled=False))
    assert f_tools.is_iot_server("iot", {}) and not f_tools.is_iot_server("home-assistant", {"url": "http://x:8123/"})


def test_only_the_current_version_counts_and_status_names_what_is_held():
    assert not iot_disclaimer.accepted({})
    assert not iot_disclaimer.accepted(tools_doc(accepted="2000-01-01"))
    assert iot_disclaimer.accepted(tools_doc(accepted=VERSION))
    rec = iot_disclaimer.record()
    assert rec["version"] == VERSION and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", rec["accepted_at"])
    held = iot_disclaimer.status(tools_doc(accepted="2000-01-01"))
    assert held == {"version": VERSION, "accepted": False, "accepted_version": "2000-01-01",
                    "accepted_at": "2026-11-03T20:00:00Z", "held": ["iot"], "doc": "docs/IOT_DISCLAIMER.md"}
    assert iot_disclaimer.status(tools_doc(accepted=VERSION))["held"] == []
    assert iot_disclaimer.status(tools_doc(iot_enabled=False))["held"] == []
    assert iot_disclaimer.required_detail()["code"] == "iot_disclaimer_required"


def test_unreadable_tools_json_counts_as_not_accepted(tmp_path):
    assert iot_disclaimer.read_tools(tmp_path / "missing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", "utf-8")
    assert iot_disclaimer.read_tools(broken) == {}
    assert not iot_disclaimer.accepted(iot_disclaimer.read_tools(broken))


def test_launcher_services_of_the_smart_home_are_recognised():
    service = iot_disclaimer.is_iot_service
    assert service("mqtt") and service("zigbee2mqtt") and service("Mosquitto")
    assert service("broker", [r"C:\Program Files\mosquitto\mosquitto.exe", "-c", "mosquitto.conf"])
    assert service("zigbee", ["node", "index.js"], r"C:\OpenAmika\IoT\zigbee2mqtt")
    assert not service("tts", ["python", "main.py"], "vendor/tts-engine")


# ───────────────────────────── the brain: routes and the MCP hub ─────────────────────────────

class GatedFakeHub(f_tools._Hub):
    """The real hub, its IoT gate included, with the MCP transport faked: nothing is spawned or dialled."""

    def __init__(self):
        super().__init__()
        self.spawned = []                  # (server, the accepted version the bridge would get in its environment)
        self.calls = []

    async def _connect_async(self, name, cfg):
        self.spawned.append((name, f_tools._server_env(name, cfg).get(iot_disclaimer.ENV_VAR)))
        from mcp_runtime import fingerprint
        self._sessions[name] = SimpleNamespace(identity=fingerprint(cfg), listed_at=time.monotonic())
        self.tools[name] = [{"name": t, "description": "", "schema": {}}
                            for t in ("list_devices", "get_state", "set_state", "health", "discovery", "permit_join")]
        self.status[name] = {"connected": True, "error": None, "at": 0}
        return self.tools[name]

    async def _close_async(self, name):
        self._sessions.pop(name, None)
        self.status[name] = {"connected": False, "error": self.status.get(name, {}).get("error"), "at": 0}

    async def _call_async(self, name, tool, arguments, timeout):
        if name not in self._sessions:
            raise RuntimeError(f"server {name!r} is not connected")
        self.calls.append((name, tool, dict(arguments or {})))
        replies = {"list_devices": {"Desk Lamp": {"state": "OFF", "nickname": "lamp"}},
                   "discovery": {"permit_join": False, "events": []},
                   "health": {"ok": True, "reason": "ok", "detail": "fake bridge"},
                   "set_state": {"ok": True, "feedback": "DONE"}}
        return json.dumps(replies.get(tool, {"ok": True})), False


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """The brain's tool routes on a Flask test app, tools.json in a temp folder, the gated fake hub."""
    from flask import Flask, jsonify, request
    monkeypatch.setattr(f_tools, "CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr(f_tools, "_cfg_sig", None)
    monkeypatch.setattr(f_tools, "_cfg_cache", None)
    monkeypatch.setattr(f_tools, "_ensure_health_thread", lambda: None)
    monkeypatch.setattr(f_tools, "_kick_health", lambda: None)
    monkeypatch.setattr(f_tools, "_g", {})
    monkeypatch.delenv(iot_disclaimer.ENV_VAR, raising=False)
    hub = GatedFakeHub()
    monkeypatch.setattr(f_tools, "HUB", hub)
    for store in (f_tools._HEALTH, f_tools._PENDING, f_tools._AUTOSTART, f_tools._connect_fail_until, f_tools._IOT_SNAPSHOT):
        store.clear()
    app = Flask("brain-under-test")
    f_tools.install_tools({"app": app, "request": request, "jsonify": jsonify})
    path = tmp_path / "tools.json"

    def write(doc):
        path.write_text(json.dumps(doc), "utf-8")
        f_tools._cfg_sig = None

    def post(route, action, body, **headers):
        return app.test_client().post(route, json=body, headers={"X-Build-F-Action": action, **headers})

    yield SimpleNamespace(client=app.test_client(), post=post, hub=hub, path=path, write=write,
                          disk=lambda: json.loads(path.read_text("utf-8")) if path.exists() else None)
    if hub.loop is not None:
        hub.loop.call_soon_threadsafe(hub.loop.stop)
    for store in (f_tools._HEALTH, f_tools._PENDING, f_tools._AUTOSTART, f_tools._connect_fail_until, f_tools._IOT_SNAPSHOT):
        store.clear()


def accept(brain, version=VERSION):
    return brain.post("/api/build_f/iot/disclaimer", "iot-disclaimer", {"accept": True, "version": version})


def test_brain_refuses_to_turn_iot_on_before_the_statement_is_accepted(brain):
    response = brain.post("/api/build_f/tools/config", "tools-config", {"config": tools_doc(iot_enabled=True)})
    assert response.status_code == 409
    assert response.get_json()["code"] == "iot_disclaimer_required"
    assert brain.disk() is None                                         # nothing written
    assert brain.hub.spawned == []                                      # nothing started


def test_brain_turns_iot_on_once_accepted_and_keeps_the_record(brain):
    response = accept(brain)
    assert response.status_code == 200 and response.get_json()["iot_disclaimer"]["accepted"] is True
    record = brain.disk()["iot_disclaimer"]
    assert record["version"] == VERSION and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["accepted_at"])
    response = brain.post("/api/build_f/tools/config", "tools-config", {"config": tools_doc(iot_enabled=True)})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["iot_disclaimer"]["accepted"] is True and payload["servers"]["iot"]["blocked"] is None
    assert payload["servers"]["iot"]["iot"] is True and payload["servers"]["hermes"]["iot"] is False
    disk = brain.disk()
    assert disk["mcpServers"]["iot"]["enabled"] is True and disk["iot_disclaimer"] == record


def test_acceptance_needs_the_ticked_box_the_current_version_and_the_page(brain):
    for body in ({"accept": False, "version": VERSION}, {"version": VERSION}, {"accept": "yes", "version": VERSION},
                 {"accept": True, "version": "2000-01-01"}, {"accept": True}):
        response = brain.post("/api/build_f/iot/disclaimer", "iot-disclaimer", body)
        assert response.status_code == 400, body
    assert brain.post("/api/build_f/iot/disclaimer", "iot-disclaimer", {"accept": True, "version": VERSION},
                      Origin="http://evil.test").status_code == 403
    assert brain.post("/api/build_f/iot/disclaimer", "tools-config", {"accept": True, "version": VERSION}).status_code == 403
    assert brain.disk() is None


def test_a_config_save_cannot_forge_or_drop_the_acceptance(brain):
    forged = tools_doc(iot_enabled=True, accepted=VERSION)               # the raw editor cannot accept on its own
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": forged}).status_code == 409
    assert accept(brain).status_code == 200
    record = brain.disk()["iot_disclaimer"]
    other = tools_doc(iot_enabled=True, accepted="1999-01-01")
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": other}).status_code == 200
    assert brain.disk()["iot_disclaimer"] == record
    stripped = tools_doc(iot_enabled=False)
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": stripped}).status_code == 200
    assert brain.disk()["iot_disclaimer"] == record                     # turning IoT off keeps the acceptance


def test_a_new_version_of_the_statement_asks_again(brain, monkeypatch):
    assert accept(brain).status_code == 200
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": tools_doc(iot_enabled=True)}).status_code == 200
    monkeypatch.setattr(iot_disclaimer, "IOT_DISCLAIMER_VERSION", "2099-01-01")
    status = brain.client.get("/api/build_f/tools").get_json()
    assert status["iot_disclaimer"]["accepted"] is False and status["iot_disclaimer"]["held"] == ["iot"]
    assert status["iot_disclaimer"]["accepted_version"] == VERSION and status["servers"]["iot"]["blocked"] == "iot_disclaimer"
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": tools_doc(iot_enabled=True)}).status_code == 409
    assert accept(brain, VERSION).status_code == 400                    # the old version is not enough any more
    assert accept(brain, "2099-01-01").status_code == 200
    assert brain.client.get("/api/build_f/tools").get_json()["iot_disclaimer"]["accepted"] is True


def test_an_install_that_already_had_iot_on_keeps_it_off_until_accepted(brain):
    brain.write(tools_doc(iot_enabled=True))                            # from before the statement existed
    panel = brain.client.get("/api/build_f/iot").get_json()
    assert panel["enabled"] is True and panel["connected"] is False and panel["blocked"] == "iot_disclaimer"
    assert panel["iot_disclaimer"]["held"] == ["iot"] and "safety statement" in panel["error"]
    status = brain.client.get("/api/build_f/tools").get_json()
    assert status["servers"]["iot"]["enabled"] is True and status["servers"]["iot"]["blocked"] == "iot_disclaimer"
    cfg = f_tools._config()
    assert f_tools._active_tools(cfg) == [] and f_tools.capability_block() is None   # she is not told about it
    for route, action, body in (("/api/build_f/iot/device", "iot-device", {"device": "lamp", "action": "on"}),
                                ("/api/build_f/iot/pairing", "iot-pairing", {"enable": True}),
                                ("/api/build_f/tools/test", "tools-test", {"server": "iot"})):
        response = brain.post(route, action, body)
        assert response.status_code == 409 and response.get_json()["code"] == "iot_disclaimer_required", route
    assert brain.hub.spawned == [] and brain.hub.calls == []            # no bridge started, nothing switched
    assert accept(brain).status_code == 200
    panel = brain.client.get("/api/build_f/iot").get_json()
    assert panel["connected"] is True and panel.get("blocked") is None and panel["devices"][0]["friendly_name"] == "Desk Lamp"
    assert brain.hub.spawned == [("iot", VERSION)]                     # the bridge gets the accepted version


def test_a_withdrawn_acceptance_shows_iot_off_and_drops_the_old_session(brain):
    brain.write(tools_doc(iot_enabled=True, accepted=VERSION))
    panel = brain.client.get("/api/build_f/iot").get_json()
    assert panel["connected"] is True and f_tools._HEALTH["iot"]["ok"] is True and "iot" in brain.hub._sessions
    brain.write(tools_doc(iot_enabled=True, accepted="2000-01-01"))     # changed by hand while the brain runs
    server = brain.client.get("/api/build_f/tools").get_json()["servers"]["iot"]
    assert server["blocked"] == "iot_disclaimer" and server["connected"] is False and server["health"] is None
    assert "safety statement" in server["error"]                       # no "online" from the old probe
    panel = brain.client.get("/api/build_f/iot").get_json()
    assert panel["blocked"] == "iot_disclaimer" and panel["devices"] == []
    assert "iot" not in brain.hub._sessions and "iot" not in f_tools._HEALTH and "iot" not in f_tools._IOT_SNAPSHOT


def test_home_assistant_and_kind_iot_servers_need_the_statement_too(brain):
    for name, entry in (("home-assistant", {"url": "http://homeassistant.local:8123/mcp_server/sse", "enabled": True}),
                        ("lights", {"url": "http://198.51.100.5:9000/mcp", "kind": "iot", "enabled": True})):
        doc = tools_doc(iot_enabled=False, **{name: entry})
        response = brain.post("/api/build_f/tools/config", "tools-config", {"config": doc})
        assert response.status_code == 409, name
    assert accept(brain).status_code == 200
    doc = tools_doc(iot_enabled=False, lights={"url": "http://198.51.100.5:9000/mcp", "kind": "iot", "enabled": True})
    assert brain.post("/api/build_f/tools/config", "tools-config", {"config": doc}).status_code == 200
    assert brain.disk()["mcpServers"]["lights"]["kind"] == "iot"          # the marker survives a save


def test_the_hub_refuses_every_way_to_a_held_server_and_drops_a_connected_one(brain):
    hub, scfg = brain.hub, tools_doc()["mcpServers"]["iot"]
    brain.write(tools_doc(iot_enabled=True))
    for attempt in (lambda: hub.connect("iot", scfg), lambda: hub.ensure_connected("iot", scfg),
                    lambda: hub.call("iot", "set_state", {"device": "lamp", "state": "on"}, 5)):
        with pytest.raises(f_tools.IotNotAccepted):
            attempt()
    assert hub.status["iot"]["blocked"] == "iot_disclaimer" and hub.spawned == [] and hub.calls == []
    assert f_tools.start_agent("iot", {**scfg, "autostart": "openamika-iot --http"}) is None
    brain.write(tools_doc(iot_enabled=True, accepted=VERSION))
    assert hub.ensure_connected("iot", scfg) and "iot" in hub._sessions
    brain.write(tools_doc(iot_enabled=True))                            # the acceptance taken back by hand
    with pytest.raises(f_tools.IotNotAccepted):
        hub.ensure_connected("iot", scfg)
    assert "iot" not in hub._sessions and not hub.status["iot"]["connected"]
    hermes = {"command": "python", "args": ["-m", "openamika_agents", "hermes"], "kind": "agent", "enabled": True}
    assert hub.ensure_connected("hermes", hermes)                       # other servers are not affected


def test_only_a_smart_home_server_gets_the_accepted_version(brain, monkeypatch):
    monkeypatch.setenv(iot_disclaimer.ENV_VAR, "inherited")
    hermes = {"command": "python", "env": {iot_disclaimer.ENV_VAR: "hand-written"}}
    iot = {"command": "python", "args": ["-m", "openamika_iot"], "env": {iot_disclaimer.ENV_VAR: "hand-written"}}
    assert iot_disclaimer.ENV_VAR not in f_tools._server_env("hermes", hermes)
    assert iot_disclaimer.ENV_VAR not in f_tools._server_env("iot", iot)  # not accepted: nothing to vouch for
    brain.write(tools_doc(accepted=VERSION))
    assert f_tools._server_env("iot", iot)[iot_disclaimer.ENV_VAR] == VERSION
    assert iot_disclaimer.ENV_VAR not in f_tools._server_env("hermes", hermes)


def test_an_approval_for_a_held_server_keeps_waiting(brain):
    brain.write(tools_doc(iot_enabled=True))
    f_tools._PENDING["p1"] = {"id": "p1", "ts": 1.0, "turn_id": "t1", "server": "iot", "tool": "set_state",
                              "arguments": {"device": "lamp", "state": "on"}}
    response = brain.post("/api/build_f/tools/approve", "tools-approve", {"id": "p1"})
    assert response.status_code == 409 and "p1" in f_tools._PENDING and brain.hub.calls == []


# ───────────────────────────── the gateway (speech/api.py) ─────────────────────────────

SPEECH_FUNCTIONS = {"_build_f_session_busy", "_build_f_control", "build_f_tools_config", "build_f_tools_test",
                    "build_f_tools_start", "build_f_iot_device", "build_f_iot_pairing", "build_f_iot_disclaimer",
                    "build_f_iot_disclaimer_accept", "_iot_refusal", "_iot_config_gate", "_iot_server_gate",
                    "_iot_use_gate", "_iot_accept_gate"}


@pytest.fixture
def gateway(tmp_path):
    """The gateway's real control and IoT functions, lifted out of speech/api.py (no torch, no models), with the
    rules module reading tools.json from a temp folder and a fake brain behind HANE_URL."""
    rules = load_by_path("gateway_iot_rules", ROOT / "brain" / "iot_disclaimer.py")
    rules.TOOLS_PATH = tmp_path / "tools.json"
    http = FakeHTTP()
    http.get = http.post
    ns = {"asyncio": asyncio, "json": json, "aiohttp": http, "Request": Request, "HTTPException": HTTPException,
          "_dialog_sessions": set(), "_wipe_in_progress": False, "HANE_URL": "http://127.0.0.1:5201", "IOT_DISCLAIMER": rules}
    nodes = []
    for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SPEECH_FUNCTIONS:
            node = copy.deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_TOOLS_FIELDS" for t in node.targets):
            nodes.append(copy.deepcopy(node))
    exec(compile(ast.Module(nodes, type_ignores=[]), "<gateway iot>", "exec"), ns)
    assert SPEECH_FUNCTIONS <= set(ns)
    path = tmp_path / "tools.json"
    return SimpleNamespace(ns=ns, http=http, rules=rules, path=path,
                           write=lambda doc: path.write_text(json.dumps(doc), "utf-8"))


def control(action, body):
    return Request(body=json.dumps(body).encode(), headers={"x-build-f-action": action})


def refused(gateway, fn, request):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(gateway.ns[fn](request))
    assert gateway.ns["_wipe_in_progress"] is False
    return caught.value


def test_gateway_refuses_a_config_that_turns_iot_on_without_acceptance(gateway):
    error = refused(gateway, "build_f_tools_config", control("tools-config", {"config": tools_doc(iot_enabled=True)}))
    assert error.status_code == 409
    assert error.detail["code"] == "iot_disclaimer_required" and error.detail["version"] == VERSION
    assert gateway.http.calls == []                                     # never forwarded to the brain
    gateway.write(tools_doc(accepted="2000-01-01"))                     # an older version is not enough
    assert refused(gateway, "build_f_tools_config", control("tools-config", {"config": tools_doc()})).status_code == 409
    assert gateway.http.calls == []


def test_gateway_forwards_it_once_accepted_and_lets_other_configs_through(gateway):
    gateway.http.response = Response(data={"ok": True, "enabled": True, "servers": {},
                                           "iot_disclaimer": {"accepted": True}, "secret": "x"})
    result = asyncio.run(gateway.ns["build_f_tools_config"](control("tools-config", {"config": tools_doc(iot_enabled=False)})))
    assert result["ok"] is True and "secret" not in result               # IoT off: no statement needed
    gateway.write(tools_doc(accepted=VERSION))
    result = asyncio.run(gateway.ns["build_f_tools_config"](control("tools-config", {"config": tools_doc(iot_enabled=True)})))
    assert result["iot_disclaimer"] == {"accepted": True}
    assert [url for url, _ in gateway.http.calls] == ["http://127.0.0.1:5201/api/build_f/tools/config"] * 2


def test_gateway_asks_again_for_a_new_version(gateway):
    gateway.write(tools_doc(accepted=VERSION))
    gateway.rules.IOT_DISCLAIMER_VERSION = "2099-01-01"
    error = refused(gateway, "build_f_tools_config", control("tools-config", {"config": tools_doc(iot_enabled=True)}))
    assert error.status_code == 409 and error.detail["version"] == "2099-01-01" and gateway.http.calls == []


def test_gateway_acceptance_needs_the_box_and_the_current_version(gateway):
    for body in ({"accept": False, "version": VERSION}, {"accept": True, "version": "2000-01-01"}, {"version": VERSION}):
        assert refused(gateway, "build_f_iot_disclaimer_accept", control("iot-disclaimer", body)).status_code == 400
    assert gateway.http.calls == []
    busy = Session()
    busy._state = "SPEAKING"
    gateway.ns["_dialog_sessions"].add(busy)                           # accepting may happen while she talks
    gateway.http.response = Response(data={"ok": True, "iot_disclaimer": {"accepted": True}})
    result = asyncio.run(gateway.ns["build_f_iot_disclaimer_accept"](control("iot-disclaimer", {"accept": True, "version": VERSION})))
    assert result == {"ok": True, "iot_disclaimer": {"accepted": True}}
    url, sent = gateway.http.calls[0]
    assert url == "http://127.0.0.1:5201/api/build_f/iot/disclaimer"
    assert sent["headers"] == {"X-Build-F-Action": "iot-disclaimer"} and sent["json"] == {"accept": True, "version": VERSION}


def test_gateway_serves_the_statement_and_its_acceptance(gateway):
    shown = asyncio.run(gateway.ns["build_f_iot_disclaimer"]())
    assert shown["ok"] is True and shown["version"] == VERSION and shown["accepted"] is False
    assert set(shown["statement"]["languages"]) == {"en", "ja", "zh"}
    gateway.write(tools_doc(accepted=VERSION))
    assert asyncio.run(gateway.ns["build_f_iot_disclaimer"]())["accepted"] is True


def test_gateway_keeps_the_panel_test_and_start_off_too(gateway):
    for fn, action, body in (("build_f_iot_device", "iot-device", {"device": "lamp", "action": "on"}),
                             ("build_f_iot_pairing", "iot-pairing", {"enable": True}),
                             ("build_f_tools_test", "tools-test", {"server": "iot"}),
                             ("build_f_tools_start", "tools-start", {"server": "iot"})):
        assert refused(gateway, fn, control(action, body)).status_code == 409, fn
    assert gateway.http.calls == []
    gateway.http.response = Response(data={"ok": True, "server": "hermes", "kind": "agent", "tools": [], "health": None})
    asyncio.run(gateway.ns["build_f_tools_test"](control("tools-test", {"server": "hermes"})))   # other servers: as before
    assert len(gateway.http.calls) == 1


def test_a_brain_refusal_with_a_code_reaches_the_page_as_a_structured_detail(gateway):
    gateway.write(tools_doc(accepted=VERSION))
    gateway.http.response = Response(status=409, data={"ok": False, "code": "iot_disclaimer_required", "error": "Accept it first."})
    error = refused(gateway, "build_f_tools_config", control("tools-config", {"config": tools_doc(iot_enabled=True)}))
    assert error.status_code == 409 and error.detail == {"code": "iot_disclaimer_required", "message": "Accept it first."}
    gateway.http.response = Response(status=409, data={"ok": False, "error": "busy"})
    assert refused(gateway, "build_f_tools_config", control("tools-config", {"config": tools_doc()})).detail == "busy"


# ───────────────────────────── the launcher ─────────────────────────────

def example_settings():
    cfg = json.loads((ROOT / "settings.example.json").read_text("utf-8"))
    for spec in cfg["services"].values():
        if isinstance(spec, dict):
            spec["enabled"] = True                                      # mosquitto and zigbee2mqtt switched on
    return cfg


def test_launcher_does_not_start_the_smart_home_without_acceptance(monkeypatch):
    cfg = example_settings()
    for tools in (None, tools_doc(), tools_doc(accepted="2000-01-01")):
        services, held, agents = launch.start_plan(cfg, tools)
        assert [s.name for s in services] == ["tts"]
        assert [s.name for s in held] == ["mqtt", "zigbee2mqtt"]
    services, held, agents = launch.start_plan(cfg, tools_doc(accepted=VERSION))
    assert [s.name for s in services] == ["tts", "mqtt", "zigbee2mqtt"] and held == []
    monkeypatch.setattr(launch.iot_rules(), "IOT_DISCLAIMER_VERSION", "2099-01-01")   # a new statement: held again
    services, held, agents = launch.start_plan(cfg, tools_doc(accepted=VERSION))
    assert [s.name for s in held] == ["mqtt", "zigbee2mqtt"]


def test_launcher_does_not_autostart_a_smart_home_server_without_acceptance():
    auto = {"iot": {**tools_doc()["mcpServers"]["iot"], "autostart": "python -m openamika_iot --http"}}
    _, _, agents = launch.start_plan({"services": {}}, {**tools_doc(), "mcpServers": auto})
    assert agents == []
    _, _, agents = launch.start_plan({"services": {}}, {**tools_doc(accepted=VERSION), "mcpServers": auto})
    assert [a.name for a in agents] == ["iot"]


def test_launcher_preflight_says_why_the_smart_home_is_off(monkeypatch):
    monkeypatch.setattr(launch, "listeners", lambda: {})
    monkeypatch.setattr(launch, "http_json", lambda *a, **k: None)
    monkeypatch.setattr(launch, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(launch, "serial_dongle", lambda: ("", "no dongle in the test"))
    cfg, tools = example_settings(), tools_doc(iot_enabled=True)
    services, held, agents = launch.start_plan(cfg, tools)
    report = launch.preflight(cfg, None, None, tools, None, services, agents, [], held_iot=held)
    check = next(c for c in report.checks if c["name"] == launch.IOT_STATEMENT_CHECK)
    assert check["level"] == "warn" and VERSION in check["detail"]
    assert "mqtt, zigbee2mqtt, iot" in check["detail"] and "Tools page" in check["detail"]
    assert not any(c["name"] in ("smart home broker", "Zigbee dongle", "smart home") for c in report.checks)
    report.iot_accepted(VERSION, ["mqtt", "zigbee2mqtt"])
    assert check["level"] == "ok" and "started mqtt, zigbee2mqtt" in check["detail"]


# ───────────────────────────── the bridge ─────────────────────────────

def test_bridge_refusal_rules():
    refusal, env = bridge_disclaimer.refusal, bridge_disclaimer.ENV_VAR
    message = refusal(None, {})
    assert "did not start" in message and VERSION in message and "docs/IOT_DISCLAIMER.md" in message
    assert "not the current one" in refusal("2000-01-01", {})
    assert refusal(None, {env: VERSION}) is None and refusal(VERSION, {}) is None
    assert refusal("2000-01-01", {env: VERSION}) is not None             # an explicit wrong flag is not overruled


def test_bridge_will_not_start_without_acceptance(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != bridge_disclaimer.ENV_VAR and not k.startswith("PYTHON")}
    env.update({"PYTHONPATH": str(BRIDGE_DIR), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                "AMIKA_MQTT_HOST": "127.0.0.1", "AMIKA_MQTT_PORT": "1",   # even a wrong import could reach no broker
                "AMIKA_IOT_DATA_DIR": str(tmp_path)})
    for extra, expected in (([], "has not been accepted"), (["--accept-iot-disclaimer", "2000-01-01"], "not the current one")):
        done = subprocess.run([sys.executable, "-B", "-m", "openamika_iot", *extra], cwd=BRIDGE_DIR, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert done.returncode == 2, done.stderr
        assert "openamika-iot did not start" in done.stderr and expected in done.stderr and VERSION in done.stderr


# ───────────────────────────── the Tools page ─────────────────────────────

class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.by_id[attrs["id"]] = (tag, attrs)


def test_tools_page_has_the_consent_dialog_unticked_and_disabled():
    html = (ROOT / "ui" / "tools.html").read_text("utf-8")
    page = _Elements()
    page.feed(html)
    assert page.by_id["iotConsent"][0] == "dialog"
    tag, attrs = page.by_id["iotConsentCheck"]
    assert tag == "input" and attrs["type"] == "checkbox" and "checked" not in attrs
    tag, attrs = page.by_id["iotConsentEnable"]
    assert tag == "button" and "disabled" in attrs
    assert "I have read and accept this" in html and "Enable IoT" in html and "Cancel" in html
    script = (ROOT / "ui" / "tools.js").read_text("utf-8")
    assert "/api/build_f/iot/disclaimer" in script and "iot_disclaimer_required" in script
