"""Actual SDK transports, isolated credentials and result handling."""
import asyncio
import base64
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

import f_tools
from mcp_credentials import CredentialStore
from mcp_results import ResultStore
from mcp_runtime import Connection

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(f_tools, "CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.delenv("AMIKA_VLM_URL", raising=False)
    cfg = {"enabled": True, "mcpServers": {}}
    monkeypatch.setattr(f_tools, "_config", lambda: cfg)
    h = f_tools._Hub()
    yield h, cfg
    for name in list(h._sessions):
        h.disconnect(name)
    if h.loop:
        h.loop.call_soon_threadsafe(h.loop.stop)
        h.thread.join(5)


def stdio_config(tmp_path):
    return {"enabled": True, "command": sys.executable,
            "args": [str(ROOT / "tests" / "_mcp_server.py"), "--counter", str(tmp_path / "counter")]}


def test_stdio_discovery_call_image_and_reconnect(hub, tmp_path):
    h, cfg = hub
    cfg["mcpServers"]["fixture"] = scfg = stdio_config(tmp_path)
    tools = h.connect("fixture", scfg)
    assert {t["name"] for t in tools} == {"get_value", "write_slowly", "get_image"}
    assert next(t for t in tools if t["name"] == "get_value")["schema"]["properties"]["value"]
    assert h.server_instructions("fixture") == "Call get_value before write_slowly."
    text, error = h.call("fixture", "get_value", {"value": "hello"}, 5)
    assert not error and "hello" in text
    text, error = h.call("fixture", "get_image", {}, 5)
    assert not error and "retained" in text
    images = h.results.public()[-1]["content"]
    assert h.results.attachment(images[0]["id"])[0].is_file()
    h.disconnect("fixture")
    assert h.server_instructions("fixture") == ""
    assert h.connect("fixture", scfg)
    assert h.status["fixture"]["protocol_version"] == "2026-07-28"


def test_ambiguous_write_timeout_is_not_repeated(hub, tmp_path):
    h, cfg = hub
    cfg["mcpServers"]["fixture"] = scfg = stdio_config(tmp_path)
    h.connect("fixture", scfg)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        h.call("fixture", "write_slowly", {}, .15)
    assert (tmp_path / "counter").read_text() == "1"


@pytest.mark.parametrize("transport,path", [("streamable-http", "/mcp"), ("sse", "/sse")])
def test_http_and_legacy_sse_roundtrip(hub, tmp_path, transport, path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "_mcp_server.py"), "--transport", transport, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                time.sleep(.05)
        h, cfg = hub
        cfg["mcpServers"]["fixture"] = scfg = {"enabled": True, "url": f"http://127.0.0.1:{port}{path}",
                                                "transport": "sse" if transport == "sse" else "http", "auth": {"mode": "none"}}
        assert len(h.connect("fixture", scfg)) == 3
        text, error = h.call("fixture", "get_value", {"value": "HTTP"}, 5)
        assert not error and "HTTP" in text
        h.disconnect("fixture")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_tool_listing_paginates_and_rejects_cursor_loops(tmp_path):
    from mcp.types import ListToolsResult, Tool
    async def scenario():
        c = Connection("fixture", {}, tmp_path, {})
        c.client = SimpleNamespace(list_tools=AsyncMock(side_effect=[
            ListToolsResult(tools=[Tool(name="one", input_schema={"type": "object"})], next_cursor="next"),
            ListToolsResult(tools=[Tool(name="two", input_schema={"type": "object"})])]))
        assert [t["name"] for t in await c.discover()] == ["one", "two"]
        c.client.list_tools = AsyncMock(return_value=ListToolsResult(tools=[], next_cursor="loop"))
        with pytest.raises(ValueError, match="pagination"):
            await c.discover()
    asyncio.run(scenario())


def test_discovery_keeps_long_tool_descriptions(tmp_path):
    # a real-world description can run past 1,000 characters with an important rule at the very end
    from mcp.types import ListToolsResult, Tool
    long = "Hold a slot for the user. " * 50 + "Reuse request_key for retries."
    async def scenario():
        c = Connection("fixture", {}, tmp_path, {})
        c.client = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(
            tools=[Tool(name="hold_slot", description=long, input_schema={"type": "object"})])))
        return await c.discover()
    assert asyncio.run(scenario())[0]["description"] == long


def test_without_an_os_credential_store_reads_are_empty_and_saving_refuses(tmp_path, monkeypatch):
    # a public server must still connect on a machine without a keyring; a sign-in must not fall back to plaintext
    import mcp_credentials
    from mcp.shared.auth import OAuthToken
    monkeypatch.setattr(mcp_credentials.os, "name", "posix")
    def no_store():
        raise RuntimeError("OAuth requires an unlocked operating-system credential store.")
    monkeypatch.setattr(mcp_credentials.CredentialStore, "_keyring", staticmethod(no_store))
    store = mcp_credentials.CredentialStore(tmp_path, "public", {"url": "https://example.com/mcp"})
    assert asyncio.run(store.get_tokens()) is None and store.provider() == {}
    with pytest.raises(RuntimeError, match="credential store"):
        asyncio.run(store.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))


def test_dpapi_persists_expiry_and_isolates_servers(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI check")
    from mcp.shared.auth import OAuthToken
    async def scenario():
        a = CredentialStore(tmp_path, "a", {"url": "https://one.example/mcp"})
        await a.set_tokens(OAuthToken(access_token="secret-token", refresh_token="secret-refresh", expires_in=1))
        assert b"secret-token" not in a.path.read_bytes()
        # A restarted store must not reset the original token lifetime.
        doc = a._read(); doc["expires_at"] = time.time() - 10; a._write(doc)
        reopened = CredentialStore(tmp_path, "a", {"url": "https://one.example/mcp"})
        assert (await reopened.get_tokens()).expires_in == 0
        assert await CredentialStore(tmp_path, "b", {"url": "https://one.example/mcp"}).get_tokens() is None
        assert await CredentialStore(tmp_path, "a", {"url": "https://two.example/mcp"}).get_tokens() is None
        reopened.clear()
        assert await a.get_tokens() is None
    asyncio.run(scenario())


def test_structured_only_and_error_results_are_preserved(tmp_path):
    store = ResultStore(tmp_path)
    text, error = store.process("s", "tool", {}, CallToolResult(content=[], structured_content={"ok": False}, is_error=True))
    assert error and json.loads(text) == {"ok": False}
    assert store.public()[0]["structured"] == {"ok": False}


def test_images_use_vision_or_survive_analysis_failure(tmp_path, monkeypatch):
    from PIL import Image
    from mcp_results import image_bytes
    stream = io.BytesIO(); Image.new("RGB", (10, 10), "red").save(stream, "PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    result = CallToolResult(content=[ImageContent(type="image", data=encoded, mime_type="image/png")])
    monkeypatch.setattr("mcp_results.analyze_local", lambda *args: "A red square")
    store = ResultStore(tmp_path)
    text, _ = store.process("s", "tool", {}, result, vision_url="http://localhost:1234")
    assert "A red square" in text and store.public()[0]["content"][0]["analysis_status"] == "complete"
    def failed(*args): raise TimeoutError()
    monkeypatch.setattr("mcp_results.analyze_local", failed)
    text, _ = store.process("s", "tool", {}, result, vision_url="http://localhost:1234")
    assert "Do not claim" in text
    assert store.attachment(store.public()[-1]["content"][0]["id"])
    with pytest.raises(ValueError): image_bytes(encoded, "image/jpeg")
    with pytest.raises(ValueError): image_bytes("!not-base64", "image/png")
    assert store.attachment("../../secret") is None
