"""Per-server OAuth credentials. DPAPI on Windows; OS keyring elsewhere.

No credentials enter tools.json or public status. Expiry survives process restart.
Storage identity includes the workspace, server name, URL, and OAuth configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time


class CredentialStore:
    def __init__(self, root: Path, name: str, config: dict):
        identity = json.dumps([str(root.resolve()), name, config.get("url"), config.get("auth", {})], sort_keys=True)
        self.key = hashlib.sha256(identity.encode()).hexdigest()
        self.directory = root / ".mcp-credentials"
        self.path = self.directory / (self.key + ".bin")
        self.lock = threading.RLock()

    def _read(self):
        with self.lock:
            if os.name == "nt":
                if not self.path.exists():
                    return {}
                import win32crypt
                raw = win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)[1].decode()
            else:
                raw = self._keyring().get_password("OpenAmika MCP", self.key)
            return json.loads(raw) if raw else {}

    @staticmethod
    def _keyring():
        import keyring
        backend = keyring.get_keyring()
        # Never silently fall back to plaintext or a chained third-party backend.
        if not type(backend).__module__.startswith(("keyring.backends.macOS", "keyring.backends.SecretService")):
            raise RuntimeError("OAuth requires an unlocked operating-system credential store.")
        return backend

    def _write(self, doc):
        raw = json.dumps(doc)
        with self.lock:
            if os.name == "nt":
                import win32crypt
                self.directory.mkdir(parents=True, exist_ok=True)
                encrypted = win32crypt.CryptProtectData(raw.encode(), "OpenAmika MCP", None, None, None, 0)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_bytes(encrypted)
                os.replace(temporary, self.path)
            else:
                self._keyring().set_password("OpenAmika MCP", self.key, raw)

    def clear(self):
        with self.lock:
            if os.name == "nt":
                self.path.unlink(missing_ok=True)
            else:
                backend = self._keyring()
                if backend.get_password("OpenAmika MCP", self.key) is not None:
                    backend.delete_password("OpenAmika MCP", self.key)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        doc = self._read()
        token = doc.get("tokens")
        if not token:
            return None
        if doc.get("expires_at") is not None:
            token["expires_in"] = max(0, int(doc["expires_at"] - time.time()))
        return OAuthToken.model_validate(token)

    async def set_tokens(self, tokens):
        with self.lock:
            doc = self._read()
            doc["tokens"] = tokens.model_dump(mode="json")
            doc["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in is not None else None
            self._write(doc)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        data = self._read().get("client")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        with self.lock:
            doc = self._read()
            doc["client"] = client_info.model_dump(mode="json")
            self._write(doc)

    def remember_provider(self, metadata):
        with self.lock:
            doc = self._read()
            doc["provider"] = metadata
            self._write(doc)

    def provider(self):
        return self._read().get("provider", {})
