import asyncio
import ast
import base64
import io
import logging
import threading
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from flask import Flask, jsonify, request
import pytest

import f_tools
from mcp_access_log import OAuthAccessFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vision"))
from image_analysis import install_image_analysis


def test_on_demand_analysis_is_bounded_and_does_not_touch_camera_state():
    from PIL import Image
    raw = io.BytesIO(); Image.new("RGB", (12, 12), "blue").save(raw, "PNG")
    model = SimpleNamespace(is_loaded=True, key="fixture", inference_lock=threading.RLock(),
                            preprocess=lambda image, prompt: (image.size, prompt), generate=lambda inputs: "A blue square")
    app = FastAPI(); install_image_analysis(app, model)
    with TestClient(app) as client:
        body = {"image": base64.b64encode(raw.getvalue()).decode(), "mime_type": "image/png", "prompt": "Describe"}
        assert client.post("/api/analyze", json=body).status_code == 403
        headers = {"X-OpenAmika-Action": "analyze-image"}
        result = client.post("/api/analyze", json=body, headers=headers)
        assert result.json()["description"] == "A blue square"
        assert client.post("/api/analyze", json={**body, "mime_type": "image/jpeg"}, headers=headers).status_code == 400
        model.is_loaded = False
        assert client.post("/api/analyze", json=body, headers=headers).status_code == 503
        assert client.post("/api/analyze", json=body, headers={**headers, "Origin": "https://other.example"}).status_code == 403


def test_callback_access_logs_redact_codes():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/1.1" %s',
        ("127.0.0.1", "GET", "/api/build_f/tools/oauth/callback?code=secret&state=private", 200), None)
    OAuthAccessFilter().filter(record)
    assert "secret" not in record.getMessage() and "private" not in record.getMessage()
    assert "[redacted]" in record.getMessage()


def test_redacted_header_roundtrip_and_changed_url_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(f_tools, "CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr(f_tools, "_cfg_sig", None)
    monkeypatch.setattr(f_tools, "_cfg_cache", None)
    monkeypatch.setattr(f_tools, "_g", {})
    monkeypatch.setattr(f_tools, "HUB", f_tools._Hub())
    monkeypatch.setattr(f_tools, "_ensure_health_thread", lambda: None)
    monkeypatch.setattr(f_tools, "_kick_health", lambda: None)
    f_tools._save_config({"enabled": False, "mcpServers": {"remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer private-token"}}}})
    app = Flask("config-test")
    f_tools.install_tools({"app": app, "request": request, "jsonify": jsonify})
    client = app.test_client()
    public = client.get("/api/build_f/tools").json
    assert "private-token" not in str(public)
    headers = {"X-Build-F-Action": "tools-config"}
    public["config"]["mcpServers"]["remote"]["persona_note"] = "Updated note"
    assert client.post("/api/build_f/tools/config", json={"config": public["config"]}, headers=headers).status_code == 200
    assert f_tools._config()["mcpServers"]["remote"]["headers"]["Authorization"] == "Bearer private-token"
    public["config"]["mcpServers"]["remote"]["url"] = "https://another.example/mcp"
    assert client.post("/api/build_f/tools/config", json={"config": public["config"]}, headers=headers).status_code == 400
    assert client.post("/api/build_f/tools/connect", json={"server": "remote"}, headers={"Origin": "https://bad.example", "X-Build-F-Action": "tools-connect"}).status_code == 403


def test_gateway_callback_recovery_and_bounded_attachments():
    # Exercise the actual route bodies without loading speech models/devices.
    from test_wipe_proxy import FakeHTTP, Response
    http = FakeHTTP()
    http.get = http.post
    tree = ast.parse((Path(__file__).resolve().parents[1] / "speech/api.py").read_text(encoding="utf-8"))
    names = {"build_f_tools_oauth_callback", "build_f_tools_attachment", "build_f_tools_status"}
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    ns = {"asyncio": asyncio, "aiohttp": http, "Request": Request, "HTTPException": HTTPException,
          "HTMLResponse": HTMLResponse, "HANE_URL": "http://127.0.0.1:5201", "_TOOLS_FIELDS": ("ok",)}
    exec(compile(ast.Module(nodes, type_ignores=[]), "<MCP gateway routes>", "exec"), ns)

    async def scenario():
        request = SimpleNamespace(query_params={"state": "test-state", "code": "private-code"})
        result = await ns["build_f_tools_oauth_callback"](request)
        assert result.status_code == 200 and b"private-code" not in result.body
        assert result.headers["cache-control"] == "no-store"
        assert http.calls[-1][1]["headers"] == {"X-Build-F-Action": "tools-oauth-callback"}
        assert http.calls[-1][1]["allow_redirects"] is False
        http.response = http.ClientError("offline")
        result = await ns["build_f_tools_oauth_callback"](request)
        assert result.status_code == 400 and b"connect again" in result.body
        before = len(http.calls)
        request.query_params = {"code": "private-code"}
        assert (await ns["build_f_tools_oauth_callback"](request)).status_code == 400
        assert len(http.calls) == before
        request.headers = {"origin": "https://foreign.example"}
        request.url = SimpleNamespace(scheme="http", netloc="localhost")
        with pytest.raises(HTTPException) as caught:
            await ns["build_f_tools_status"](request)
        assert caught.value.status_code == 403 and len(http.calls) == before

        class Chunks:
            async def iter_chunked(self, size):
                yield b"a" * 65536
                yield self.tail
        chunks = Chunks(); chunks.tail = b"image"
        http.response = Response()
        http.response.headers = {"Content-Type": "image/png"}
        http.response.content = chunks
        image = await ns["build_f_tools_attachment"]("a" * 32)
        assert image.status_code == 200 and image.headers["x-content-type-options"] == "nosniff"
        chunks.tail = b"a" * (5 * 1024 * 1024)
        with pytest.raises(HTTPException) as caught:
            await ns["build_f_tools_attachment"]("a" * 32)
        assert caught.value.status_code == 413
        chunks.tail = b"image"
        http.response.headers = {"Content-Type": "text/html"}
        with pytest.raises(HTTPException) as caught:
            await ns["build_f_tools_attachment"]("a" * 32)
        assert caught.value.status_code == 415
        with pytest.raises(HTTPException) as caught:
            await ns["build_f_tools_attachment"]("../private")
        assert caught.value.status_code == 404
    asyncio.run(scenario())
