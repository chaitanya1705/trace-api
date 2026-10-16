"""Credential vault primitives (A11): AES-256-GCM, random 96-bit nonce, key version selects the key.

The master key lives only in backend environment variables. Ciphertext and nonce are stored in Postgres; the key is
never stored beside them. AAD binds a ciphertext to its connection, version and kind, so a ciphertext copied to
another row fails authentication. The API never returns plaintext; callers get masked metadata only.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(RuntimeError):
    pass


def _b64(v: str) -> bytes:
    return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@dataclass(frozen=True)
class Sealed:
    key_version: int
    nonce: bytes
    ciphertext: bytes


def _aad(connection_id: str, version: int, kind: str) -> bytes:
    return f"trace-vault|{connection_id}|{version}|{kind}".encode()


class Vault:
    def __init__(self, current_version: int, keys: dict[int, bytes]):
        if current_version not in keys:
            raise VaultError("current key version is not in the keyring")
        if any(len(k) != 32 for k in keys.values()):
            raise VaultError("keys must be 32 bytes")
        self._current = current_version
        self._keys = keys

    @classmethod
    def from_settings(cls, settings) -> Vault:
        if not settings.trace_secret_key:
            raise VaultError("TRACE_SECRET_KEY is not configured")
        keys = {settings.trace_secret_key_version: _b64(settings.trace_secret_key)}
        if settings.trace_secret_keys_previous:
            for part in settings.trace_secret_keys_previous.split(","):
                ver, _, val = part.partition(":")
                keys[int(ver)] = _b64(val)
        return cls(settings.trace_secret_key_version, keys)

    @property
    def current_version(self) -> int:
        return self._current

    def seal(self, plaintext: str, *, connection_id: str, version: int, kind: str) -> Sealed:
        nonce = os.urandom(12)
        ct = AESGCM(self._keys[self._current]).encrypt(
            nonce, plaintext.encode(), _aad(connection_id, version, kind)
        )
        return Sealed(self._current, nonce, ct)

    def open(self, sealed: Sealed, *, connection_id: str, version: int, kind: str) -> str:
        key = self._keys.get(sealed.key_version)
        if key is None:
            raise VaultError(f"no key for version {sealed.key_version}")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, _aad(connection_id, version, kind)).decode()
        except InvalidTag:
            raise VaultError("ciphertext failed authentication") from None


def mask(secret: str) -> str:
    """Display hint only: last 4 characters, never enough to reconstruct."""
    return "…" + secret[-4:] if len(secret) >= 12 else "…"
