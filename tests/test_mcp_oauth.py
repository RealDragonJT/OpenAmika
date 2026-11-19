"""OAuth protocol fixtures exercise the SDK, not a fabricated authenticated session."""
import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from mcp_runtime import Connection, AuthRequired


class MemoryStore:
    def __init__(self):
        self.tokens = self.client = None
        self.metadata = {}
    async def get_tokens(self): return self.tokens
    async def set_tokens(self, value): self.tokens = value
    async def get_client_info(self): return self.client
    async def set_client_info(self, value): self.client = value
    def remember_provider(self, value): self.metadata = value
    def provider(self): return self.metadata
    def clear(self): self.tokens = self.client = None; self.metadata = {}


class OAuthFixture:
    def __init__(self):
        self.registered = self.exchanges = self.refreshes = 0
        self.redirect = None
        self.token = "access-one"
        self.force_scope = False
        self.reject_refresh = False

    async def handle(self, request):
        url = str(request.url)
        if url == "https://resource.example/mcp":
            if self.force_scope:
                self.force_scope = False
                return httpx2.Response(403, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="read write"'})
            if request.headers.get("authorization") == "Bearer " + self.token:
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://resource.example/.well-known/oauth-protected-resource/mcp", scope="read"'})
        if url == "https://resource.example/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={"resource": "https://resource.example/mcp", "authorization_servers": ["https://auth.example"], "scopes_supported": ["read", "write"]})
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": "https://auth.example", "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token", "registration_endpoint": "https://auth.example/register", "revocation_endpoint": "https://auth.example/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True})
        if url == "https://auth.example/register":
            self.registered += 1
            return httpx2.Response(201, json={**json.loads(request.content), "client_id": "test-client"})
        if url == "https://auth.example/token":
            values = parse_qs(request.content.decode())
            assert values["resource"] == ["https://resource.example/mcp"]
            if values["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                if self.reject_refresh:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                assert values["refresh_token"] == ["refresh-one"]
                self.token = "access-two"
            else:
                self.exchanges += 1
                verifier = values["code_verifier"][0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                assert self.redirect["code_challenge"] == [challenge]
                assert self.redirect["code_challenge_method"] == ["S256"]
                assert values["code"] == ["test-code"]
            return httpx2.Response(200, json={"access_token": self.token, "token_type": "Bearer", "expires_in": 60,
                                             "refresh_token": "refresh-two" if self.refreshes else "refresh-one", "scope": "read"})
        raise AssertionError("Unexpected OAuth request: " + url)


async def connection(tmp_path, *, store=None, interactive=True):
    store = store or MemoryStore()
    c = Connection("fixture", {"url": "https://resource.example/mcp"}, tmp_path, {}, interactive=interactive, store=store)
    # DNS is not part of these in-memory HTTP fixtures; runtime request-guard tests are separate.
    async def allow(_request): pass
    c.guard_request = allow
    return c


async def authorize(c, fixture, *, wrong_issuer=False):
    provider = await c.auth()
    original = c.redirect
    async def redirect(url):
        await original(url)
        fixture.redirect = parse_qs(urlsplit(url).query)
        assert not c.accept_callback({"code": "test-code", "state": "incorrect"})
        assert c.accept_callback({"code": "test-code", "state": fixture.redirect["state"][0],
                                  "iss": "https://wrong.example" if wrong_issuer else "https://auth.example"})
        assert not c.accept_callback({"code": "test-code", "state": fixture.redirect["state"][0]})
    provider.context.redirect_handler = redirect
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(fixture.handle), auth=provider) as http:
        response = await http.post("https://resource.example/mcp", headers={"MCP-Protocol-Version": "2026-07-28"}, json={})
        assert response.status_code == 200


def test_discovery_registration_pkce_and_restart_refresh(tmp_path):
    async def scenario():
        f = OAuthFixture(); c = await connection(tmp_path)
        await authorize(c, f)
        assert f.registered == 1 and f.exchanges == 1
        assert c.storage.metadata["issuer"] == "https://auth.example"
        c.storage.tokens.expires_in = 0
        restarted = await connection(tmp_path, store=c.storage, interactive=False)
        provider = await restarted.auth()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(f.handle), auth=provider) as http:
            responses = await asyncio.gather(*[http.post("https://resource.example/mcp", json={}, headers={"MCP-Protocol-Version": "2026-07-28"}) for _ in range(3)])
        assert all(r.status_code == 200 for r in responses)
        assert f.refreshes == 1 and f.registered == 1
        assert c.storage.tokens.refresh_token == "refresh-two"
    asyncio.run(scenario())


def test_wrong_issuer_never_exchanges_code(tmp_path):
    async def scenario():
        f = OAuthFixture(); c = await connection(tmp_path)
        with pytest.raises(Exception, match="issuer|iss"):
            await authorize(c, f, wrong_issuer=True)
        assert f.exchanges == 0 and c.storage.tokens is None
    asyncio.run(scenario())


def test_step_up_uses_a_new_callback_and_requested_scopes(tmp_path):
    async def scenario():
        f = OAuthFixture(); c = await connection(tmp_path)
        await authorize(c, f)
        old_state = c.authorization["state"]
        f.force_scope = True
        # Use the same provider, as a connected client does during a later tool call.
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(f.handle), auth=c.provider) as http:
            response = await http.post("https://resource.example/mcp", json={})
        assert response.status_code == 200
        assert c.authorization["state"] != old_state
        assert set(f.redirect["scope"][0].split()) == {"read", "write"}
        assert f.exchanges == 2
    asyncio.run(scenario())


def test_unattended_discovery_does_not_register_or_open_login(tmp_path):
    async def scenario():
        c = await connection(tmp_path, interactive=False)
        assert await c.auth() is None
        with pytest.raises(AuthRequired): await c.redirect("https://auth.example/authorize")
    asyncio.run(scenario())


def test_rejected_refresh_requires_sign_in_without_opening_browser(tmp_path):
    async def scenario():
        f = OAuthFixture(); c = await connection(tmp_path)
        await authorize(c, f)
        c.storage.tokens.expires_in = 0
        f.reject_refresh = True
        f.token = "revoked"
        restarted = await connection(tmp_path, store=c.storage, interactive=False)
        provider = await restarted.auth()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(f.handle), auth=provider) as http:
            with pytest.raises(AuthRequired):
                await http.post("https://resource.example/mcp", json={})
        assert f.exchanges == 1
    asyncio.run(scenario())


def test_discovery_cannot_redirect_to_private_or_insecure_origin(tmp_path):
    async def scenario():
        c = Connection("fixture", {"url": "http://127.0.0.1:8000/mcp"}, tmp_path, {}, store=MemoryStore())
        await c.guard_request(httpx2.Request("GET", "http://127.0.0.1:8000/metadata"))
        with pytest.raises(ValueError, match="HTTPS"):
            await c.guard_request(httpx2.Request("GET", "http://auth.example/metadata"))
        with pytest.raises(ValueError, match="private"):
            await c.guard_request(httpx2.Request("GET", "https://127.0.0.1/metadata"))
    asyncio.run(scenario())
