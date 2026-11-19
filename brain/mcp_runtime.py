"""SDK v2 connection owner, OAuth interaction, and bounded tool discovery.

Each connection opens and closes all SDK contexts in one asyncio task. Calls are
queued to that task: no cross-task AnyIO scope exits and no replay of writes.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import time
from urllib.parse import parse_qs, urlsplit

from mcp_credentials import CredentialStore


class AuthRequired(RuntimeError):
    pass


class OAuthLogFilter(logging.Filter):
    def filter(self, record):
        if record.levelno >= logging.WARNING:
            record.msg = "MCP OAuth operation failed; check connection status."
            record.args = ()
            record.exc_info = record.exc_text = None
        return True


def fingerprint(cfg):
    return hashlib.sha256(json.dumps({k: v for k, v in cfg.items()
                                    if k not in ("tools", "persona_note", "enabled")}, sort_keys=True).encode()).hexdigest()


def safe_error(error):
    """Public diagnostics never include response bodies, query strings, or tokens."""
    if isinstance(error, BaseExceptionGroup):
        return safe_error(error.exceptions[0])
    if isinstance(error, AuthRequired):
        return "Sign in to authorize this server."
    response = getattr(error, "response", None)
    if response is not None:
        if response.status_code in (401, 403):
            return "Sign in to authorize this server (HTTP %s)." % response.status_code
        return "MCP server returned HTTP %s." % response.status_code
    if isinstance(error, (TimeoutError, asyncio.CancelledError)):
        return "The connection timed out or was cancelled."
    if type(error).__module__.startswith("mcp.client.auth"):
        return "OAuth authorization failed. Check client registration and sign in again."
    return "MCP connection failed (%s). Check the server URL, transport, and credentials." % type(error).__name__


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Use an HTTP(S) server URL without embedded credentials or a fragment.")
    return parsed


def origin(url):
    p = validate_url(url)
    return p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)


class Connection:
    def __init__(self, name, cfg, root, status, *, interactive=False, store=None):
        self.name, self.cfg, self.status = name, cfg, status
        self.identity = fingerprint(cfg)
        self.interactive = interactive
        self.storage = store or CredentialStore(root, name, cfg)
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.callback = asyncio.get_running_loop().create_future()
        self.authorization = None
        self.provider = None
        self.client = None
        self.tools = []
        self.instructions = ""     # the server's own usage notes from the handshake (untrusted, shown to the clerk)
        self.listed_at = 0
        self.task = None
        self.last_http_status = None
        self.auth_needed = False

    def update(self, state, **kwargs):
        self.status[self.name] = {"state": state, "connected": state == "connected", "error": None,
                                  "at": time.time(), **kwargs}

    async def guard_request(self, request):
        """An explicitly chosen LAN endpoint is allowed; discovered private destinations are not."""
        url = str(request.url)
        p = validate_url(url)
        if origin(url) == origin(self.cfg["url"]):
            return
        allowed = (self.cfg.get("auth") or {}).get("allowed_origins", [])
        if origin(url) in [origin(u) for u in allowed]:
            return
        if p.scheme != "https":
            raise ValueError("Discovered OAuth endpoints must use HTTPS.")
        addresses = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Discovered OAuth endpoint resolves to a private address; configure its origin explicitly.")

    async def note_response(self, response):
        self.last_http_status = response.status_code

    async def redirect(self, url):
        if not self.interactive:
            self.auth_needed = True
            raise AuthRequired()
        if self.callback.done():
            self.callback = asyncio.get_running_loop().create_future()
        # This URL is displayed only following an explicit Connect action, never launched by a poller.
        p = validate_url(url)
        await self.guard_request(type("Request", (), {"url": url})())
        values = parse_qs(p.query)
        self.authorization = {"url": url, "issuer": self.provider.context.auth_server_url or f"{p.scheme}://{p.netloc}",
                              "scope": values.get("scope", [""])[0], "state": values.get("state", [""])[0]}
        metadata = self.provider.context.oauth_metadata
        if metadata:
            saved = metadata.model_dump(mode="json")
            prm = self.provider.context.protected_resource_metadata
            if prm:
                saved["_protected_resource"] = prm.model_dump(mode="json")
            self.storage.remember_provider(saved)
        self.update("authorizing", authorization={k: v for k, v in self.authorization.items() if k != "state"})

    async def receive_callback(self):
        return await asyncio.wait_for(asyncio.shield(self.callback), 300)

    def accept_callback(self, params):
        from mcp.client.auth import AuthorizationCodeResult
        if not self.authorization or self.callback.done():
            return False
        state = params.get("state", "")
        if not state or not secrets.compare_digest(state, self.authorization["state"]):
            return False
        if params.get("error"):
            self.callback.set_exception(AuthRequired("Authorization declined."))
        else:
            self.callback.set_result(AuthorizationCodeResult(code=params.get("code", ""), state=state, iss=params.get("iss")))
        return True

    async def auth(self):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata, OAuthClientInformationFull
        cfg = self.cfg.get("auth") or {}
        auth_logger = logging.getLogger("mcp.client.auth.oauth2")
        if not any(isinstance(f, OAuthLogFilter) for f in auth_logger.filters):
            auth_logger.addFilter(OAuthLogFilter())
        mode = cfg.get("mode", "auto")
        if mode == "none" or mode == "static" or (self.cfg.get("headers") and mode == "auto"):
            return None
        # No dynamic registration during unattended discovery, even on an auth-required server.
        if not self.interactive and await self.storage.get_tokens() is None:
            return None
        redirect_uri = cfg.get("redirect_uri")
        if not redirect_uri:
            import os
            redirect_uri = os.getenv("AMIKA_CHAT_URL", "http://localhost:53000").rstrip("/") + "/api/build_f/tools/oauth/callback"
        p = validate_url(redirect_uri)
        if p.scheme != "https" and p.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("OAuth callback requires HTTPS or a loopback address.")
        if cfg.get("client_id") and await self.storage.get_client_info() is None:
            await self.storage.set_client_info(OAuthClientInformationFull(
                client_id=cfg["client_id"], redirect_uris=[redirect_uri], token_endpoint_auth_method="none",
                issuer=cfg.get("issuer")))
        metadata = OAuthClientMetadata(client_name="OpenAmika", redirect_uris=[redirect_uri],
                                       token_endpoint_auth_method="none", scope=cfg.get("scope"))
        class PersistentOAuth(OAuthClientProvider):
            async def _initialize(provider):
                await super()._initialize()
                from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
                saved = self.storage.provider()
                if saved:
                    provider.context.oauth_metadata = OAuthMetadata.model_validate(saved)
                    provider.context.auth_server_url = str(provider.context.oauth_metadata.issuer)
                    if saved.get("_protected_resource"):
                        provider.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(saved["_protected_resource"])
                token = provider.context.current_tokens
                if token and token.expires_in is not None:
                    provider.context.token_expiry_time = time.time() + token.expires_in - 5

        self.provider = PersistentOAuth(self.cfg["url"], metadata, self.storage,
                                            redirect_handler=self.redirect, callback_handler=self.receive_callback,
                                            client_metadata_url=cfg.get("client_metadata_url"))
        return self.provider

    async def discover(self):
        found, cursors, cursor = [], set(), None
        for _ in range(100):
            listing = await self.client.list_tools(cursor=cursor, cache_mode="use")
            for tool in listing.tools:
                found.append({"name": tool.name, "description": (tool.description or "")[:2000],
                              "schema": tool.input_schema, "output_schema": tool.output_schema,
                              "annotations": tool.annotations.model_dump(mode="json") if tool.annotations else None})
            cursor = listing.next_cursor
            if not cursor:
                self.tools, self.listed_at = found, time.monotonic()
                return found
            if cursor in cursors or len(found) > 10000:
                raise ValueError("Invalid or excessive tool pagination.")
            cursors.add(cursor)
        raise ValueError("Too many tool pages.")

    async def run(self):
        from mcp import Client, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx2
        self.update("connecting")
        try:
            async with AsyncExitStack() as stack:
                if self.cfg.get("url"):
                    validate_url(self.cfg["url"])
                    auth = await self.auth()
                    headers = self.cfg.get("headers") or {}
                    if self.cfg.get("transport") == "sse":
                        def factory(**kwargs):
                            return httpx2.AsyncClient(**kwargs, event_hooks={"request": [self.guard_request], "response": [self.note_response]})
                        transport = sse_client(self.cfg["url"], headers=headers, auth=auth, httpx_client_factory=factory)
                    else:
                        http = await stack.enter_async_context(httpx2.AsyncClient(
                            headers=headers, auth=auth, timeout=httpx2.Timeout(20, read=60),
                            event_hooks={"request": [self.guard_request], "response": [self.note_response]}, follow_redirects=False))
                        transport = streamable_http_client(self.cfg["url"], http_client=http)
                else:
                    transport = StdioServerParameters(command=self.cfg["command"], args=self.cfg.get("args", []),
                                                       env=self.cfg.get("env"), cwd=self.cfg.get("cwd"))
                client = Client(transport, mode="legacy" if self.cfg.get("transport") == "sse" else "auto")
                # Person-waiting OAuth happens inside SDK connect; bounded by receive_callback.
                async with asyncio.timeout(330 if self.interactive else 25):
                    self.client = await stack.enter_async_context(client)
                    self.instructions = str(client.instructions or "")[:2000]
                    await self.discover()
                self.update("connected", protocol_version=client.protocol_version)
                self.ready.set_result(self.tools)
                while True:
                    method, payload, timeout, result = await self.queue.get()
                    if result.cancelled():
                        continue
                    try:
                        if method == "list":
                            answer = await asyncio.wait_for(self.discover(), timeout)
                        else:
                            answer = await asyncio.wait_for(client.call_tool(payload[0], payload[1]), timeout)
                        if not result.done():
                            result.set_result(answer)
                        if method == "call":
                            self.update("connected", protocol_version=client.protocol_version)
                    except Exception as error:
                        if self.auth_needed or self.last_http_status in (401, 403):
                            self.update("auth_required", error="Sign in to authorize this server.")
                        if not result.done():
                            if method == "call" and isinstance(error, (TimeoutError, httpx2.TransportError)):
                                result.set_exception(RuntimeError("Tool outcome unknown: response lost or timed out. The action was not retried."))
                            else:
                                result.set_exception(RuntimeError(safe_error(error)))
        except BaseException as error:
            message = safe_error(error)
            if self.auth_needed:
                message = "Sign in to authorize this server."
            elif self.last_http_status in (401, 403):
                message = "Sign in to authorize this server (HTTP %s)." % self.last_http_status
            state = "auth_required" if "Sign in" in message or isinstance(error, AuthRequired) else "error"
            self.update(state, error=message)
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(message))
            # Consume background readiness failures; awaiters still receive the exception.
            if self.ready.done() and not self.ready.cancelled():
                self.ready.exception()
        finally:
            self.authorization = None
            while not self.queue.empty():
                *_, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError("MCP connection closed. Tool outcome may be unknown; not retried."))

    async def request(self, method, payload=None, timeout=20):
        if self.task.done():
            raise RuntimeError("MCP connection closed. Reconnect before calling tools.")
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((method, payload, timeout, result))
        try:
            return await asyncio.wait_for(result, timeout + 2)
        except TimeoutError:
            # Terminate the owner so a timed-out queued operation cannot execute later.
            await self.close()
            raise RuntimeError("Tool outcome unknown: operation timed out. The action was not retried.") from None

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task
        if not self.callback.done():
            self.callback.cancel()
        self.update("disconnected")

    async def revoke(self):
        """Local removal always happens, even when the provider cannot be reached."""
        import httpx2
        warning = None
        try:
            metadata = self.storage.provider()
            token = await self.storage.get_tokens()
            client = await self.storage.get_client_info()
            endpoint = metadata.get("revocation_endpoint")
            if token and endpoint and client:
                async with httpx2.AsyncClient(timeout=10, event_hooks={"request": [self.guard_request]}) as http:
                    data = {"token": token.refresh_token or token.access_token, "client_id": client.client_id}
                    headers = {}
                    if client.client_secret and client.token_endpoint_auth_method == "client_secret_basic":
                        import base64
                        from urllib.parse import quote
                        encoded = base64.b64encode((quote(client.client_id, safe="") + ":" + quote(client.client_secret, safe="")).encode()).decode()
                        headers["Authorization"] = "Basic " + encoded
                    elif client.client_secret:
                        data["client_secret"] = client.client_secret
                    response = await http.post(endpoint, data=data, headers=headers)
                    response.raise_for_status()
            elif token:
                warning = "Local credentials removed; this provider did not advertise token revocation."
        except Exception:
            warning = "Local credentials removed; remote revocation could not be confirmed."
        finally:
            self.storage.clear()
        return warning
