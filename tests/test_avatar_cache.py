"""Exercise the actual avatar routes without importing the GPU/speech stack."""
import ast
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from avatar_cache import asset_response, model_response, versioned_reference

ROOT = Path(__file__).resolve().parents[1]


def avatar_app(models_dir):
    source = (ROOT / "speech/api.py").read_text(encoding="utf-8")
    wanted = {"_avatar_dir", "_avatar_document", "avatar_file", "_avatar_motion_entries", "avatar_motions",
              "_revalidate_page_assets"}
    namespace = dict(Path=Path, json=json, asyncio=asyncio, Request=Request, HTTPException=HTTPException,
                     logger=logging.getLogger(__name__), AVATAR_MODELS_DIR=models_dir, quote=quote,
                     asset_response=asset_response, model_response=model_response,
                     versioned_reference=versioned_reference)
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            exec(ast.get_source_segment(source, node), namespace)

    async def file(request):
        return await namespace["avatar_file"](**request.path_params, request=request)

    async def motions(request):
        from starlette.responses import JSONResponse
        return JSONResponse(await namespace["avatar_motions"](**request.path_params))

    return Starlette(routes=[Route("/avatar_models/{model_id}/{file_path:path}", file),
                             Route("/api/avatar/models/{model_id}/motions", motions)],
                     middleware=[Middleware(BaseHTTPMiddleware, dispatch=namespace["_revalidate_page_assets"])])


@pytest.fixture
def assets(tmp_path):
    model = tmp_path / "Model"
    model.mkdir()
    for name, content in {"body.moc3": b"model", "texture.png": b"texture", "idle.motion3.json": b"{}",
                          "smile.exp3.json": b"{}", "sound.wav": b"sound"}.items():
        (model / name).write_bytes(content)
    (model / "avatar.model3.json").write_text(json.dumps({"FileReferences": {
        "Moc": "body.moc3", "Textures": ["texture.png?quality=full#image"],
        "Motions": {"Idle": [{"File": "idle.motion3.json", "Sound": "sound.wav"}]}}}), encoding="utf-8")
    with TestClient(avatar_app(tmp_path)) as client:
        yield model, client


def manifest(client, headers=None):
    return client.get("/avatar_models/Model/avatar.model3.json", headers=headers)


def test_unchanged_assets_revalidate_without_sending_the_file(assets):
    _, client = assets
    url = "/avatar_models/Model/body.moc3"
    first = client.get(url)
    assert first.status_code == 200 and first.content == b"model"
    assert first.headers["cache-control"] == "private, no-cache"
    for headers in [{"If-None-Match": first.headers["etag"]},
                    {"If-None-Match": '"old", W/' + first.headers["etag"]},
                    {"If-None-Match": "*"}, {"If-Modified-Since": first.headers["last-modified"]}]:
        response = client.get(url, headers=headers)
        assert response.status_code == 304 and response.content == b""
        assert response.headers["etag"] == first.headers["etag"]
        assert "content-length" not in response.headers
    # A nonmatching ETag must win over a matching modification date.
    assert client.get(url, headers={"If-None-Match": '"old"',
                                   "If-Modified-Since": first.headers["last-modified"]}).status_code == 200
    assert client.get(url, headers={"If-Modified-Since": "invalid"}).status_code == 200


def test_versioned_assets_are_immutable_and_only_changed_files_get_new_urls(assets):
    model, client = assets
    first = manifest(client)
    refs = first.json()["FileReferences"]
    texture_url = "/avatar_models/Model/" + refs["Textures"][0]
    moc_url = "/avatar_models/Model/" + refs["Moc"]
    for url in (texture_url, moc_url):
        assert client.get(url).headers["cache-control"] == "private, max-age=31536000, immutable"
    assert "quality=full" in texture_url and texture_url.endswith("#image")
    # A timestamp-only change does not force a new download.
    stat = (model / "texture.png").stat()
    os.utime(model / "texture.png", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert manifest(client).headers["etag"] == first.headers["etag"]
    # Edit bytes at the same size, within the same second: nanosecond metadata invalidates the hash cache.
    (model / "texture.png").write_bytes(b"changed")
    os.utime(model / "texture.png", ns=(stat.st_atime_ns, stat.st_mtime_ns + 20_000_000))
    changed = manifest(client, {"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200
    changed_refs = changed.json()["FileReferences"]
    assert changed_refs["Textures"][0] != refs["Textures"][0]
    assert changed_refs["Moc"] == refs["Moc"]
    assert client.get("/avatar_models/Model/" + changed_refs["Textures"][0]).content == b"changed"
    assert client.get(texture_url).headers["cache-control"] == "private, no-cache"
    assert client.get("/avatar_models/Model/body.moc3?v=arbitrary").headers["cache-control"] == "private, no-cache"


def test_manifest_and_motion_registry_stay_current(assets):
    model, client = assets
    first = manifest(client)
    unchanged = manifest(client, {"If-None-Match": first.headers["etag"]})
    assert unchanged.status_code == 304 and not unchanged.content
    assert first.headers["cache-control"] == "private, no-cache"
    refs = first.json()["FileReferences"]
    for reference in [refs["Motions"]["Idle"][0]["File"], refs["Motions"]["Idle"][0]["Sound"],
                      refs["Expressions"][0]["File"]]:
        assert "?v=" in reference
    registry = client.get("/api/avatar/models/Model/motions")
    assert registry.headers["cache-control"] == "private, no-cache"
    assert all("?v=" in entry["path"] for entry in registry.json())
    (model / "new.exp3.json").write_text("{}")
    assert manifest(client, {"If-None-Match": first.headers["etag"]}).status_code == 200


def test_head_ranges_missing_files_and_path_boundaries(assets):
    model, client = assets
    url = "/avatar_models/Model/body.moc3"
    head = client.head(url)
    assert head.status_code == 200 and not head.content and head.headers["content-length"] == "5"
    partial = client.get(url, headers={"Range": "bytes=1-3", "If-Range": head.headers["etag"]})
    assert partial.status_code == 206 and partial.content == b"ode"
    assert client.get(url, headers={"Range": "bytes=1-3", "If-Range": '"old"'}).content == b"model"
    missing = client.get("/avatar_models/Model/missing.png")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    (model / "missing.png").write_bytes(b"restored")
    assert client.get("/avatar_models/Model/missing.png").content == b"restored"
    (model.parent / "private.txt").write_text("private")
    assert client.get("/avatar_models/Model/%2e%2e/private.txt").status_code == 403
    assert versioned_reference("../private.txt", model, model) == "../private.txt"
    assert versioned_reference("https://example.com/image.png", model, model) == "https://example.com/image.png"


def test_injected_motions_and_bad_model_json_keep_existing_fallbacks(assets):
    model, client = assets
    (model / "avatar.model3.json").write_text("{}")
    assert "?v=" in manifest(client).json()["FileReferences"]["Motions"]["Idle"][0]["File"]
    (model / "avatar.model3.json").write_text("invalid")
    response = manifest(client)
    assert response.status_code == 200 and response.text == "invalid"
    assert "immutable" not in response.headers["cache-control"]
