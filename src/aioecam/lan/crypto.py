"""Ayla LAN-mode session crypto, both roles.

After the key exchange both sides derive the same four keys from the LAN key,
the two nonces and the two timestamps. ``app`` keys protect app -> machine
messages, ``dev`` keys the other direction. A role only decides which set is
used for sending, so the fake device in the tests shares this code.

Rules that cost time to find (do not "simplify"):

- The LAN key is used as the ASCII bytes of its base64 string, never decoded.
- Each direction is ONE AES-256-CBC stream chained across all messages for
  the lifetime of the session; a lost message desynchronises it for good.
- Plaintext is zero padded to a multiple of 16 with at least one zero byte.
- The HMAC-SHA256 signature covers the plaintext, not the ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from aioecam.exceptions import EcamError

Role = Literal["app", "device"]


class SessionError(EcamError):
    """A session message could not be authenticated or decoded."""


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _derive(lan_key: bytes, seed: bytes) -> bytes:
    return _hmac(lan_key, _hmac(lan_key, seed) + seed)


class _Direction:
    def __init__(self, lan_key: bytes, seed: bytes) -> None:
        self.sign_key = _derive(lan_key, seed + b"0")
        crypto_key = _derive(lan_key, seed + b"1")
        iv = _derive(lan_key, seed + b"2")[:16]
        cipher = Cipher(algorithms.AES(crypto_key), modes.CBC(iv))
        # Never finalized: update() keeps the CBC chain across messages.
        self.encryptor = cipher.encryptor()
        self.decryptor = cipher.decryptor()


class LanSession:
    def __init__(
        self,
        lan_key: str,
        *,
        random_1: str,
        random_2: str,
        time_1: str,
        time_2: str,
        role: Role = "app",
    ) -> None:
        key = lan_key.encode("ascii")
        app = _Direction(key, f"{random_1}{random_2}{time_1}{time_2}".encode())
        dev = _Direction(key, f"{random_2}{random_1}{time_2}{time_1}".encode())
        self._out, self._in = (app, dev) if role == "app" else (dev, app)
        self.role: Role = role
        self._seq = 0

    def seal(self, data: dict[str, Any]) -> dict[str, str]:
        """Encrypt and sign one message. ``seq_no`` is added here."""
        # seq_no as a string, like the official app and cremalink.
        plain = json.dumps({"seq_no": str(self._seq), "data": data}, separators=(",", ":"))
        self._seq += 1
        raw = plain.encode()
        padded = raw + b"\x00" * (16 - len(raw) % 16)
        enc = self._out.encryptor.update(padded)
        sign = _hmac(self._out.sign_key, raw)
        return {
            "enc": base64.b64encode(enc).decode("ascii"),
            "sign": base64.b64encode(sign).decode("ascii"),
        }

    def open(self, message: dict[str, Any]) -> dict[str, Any]:
        """Decrypt and authenticate one message; returns the full ``{seq_no, data}`` body."""
        try:
            enc = base64.b64decode(message["enc"], validate=True)
            sign = base64.b64decode(message.get("sign", ""), validate=True)
        except (KeyError, TypeError, binascii.Error) as exc:
            raise SessionError(f"malformed session message: {exc}") from exc
        if not enc or len(enc) % 16:
            raise SessionError(f"ciphertext length {len(enc)} is not a multiple of 16")
        # Decrypt even if the signature is bad: skipping it would desync the CBC stream.
        plain = self._in.decryptor.update(enc).rstrip(b"\x00")
        if not hmac.compare_digest(_hmac(self._in.sign_key, plain), sign):
            raise SessionError("signature mismatch")
        try:
            body = json.loads(plain)
        except ValueError as exc:
            raise SessionError("decrypted message is not JSON") from exc
        if not isinstance(body, dict):
            raise SessionError("decrypted message is not an object")
        return body
