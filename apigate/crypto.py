"""Secrets handling.

* Provider keys are stored AES-256-GCM encrypted, bound (AAD) to their row id,
  under a master key that never lives in the database.
* Gateway API keys and session tokens are stored only as HMAC-SHA256 digests.
* Passwords use scrypt (stdlib) with per-user salt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API_KEY_PREFIX = "sk-gate-"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Vault:
    def __init__(self, master: bytes):
        if len(master) != 32:
            raise ValueError("master key must be 32 bytes")
        self._enc = HKDF(hashes.SHA256(), 32, None, b"apigate/enc/v1").derive(master)
        self._mac = HKDF(hashes.SHA256(), 32, None, b"apigate/mac/v1").derive(master)

    # -- loading ---------------------------------------------------------
    @staticmethod
    def generate_master() -> str:
        return _b64e(secrets.token_bytes(32))

    @classmethod
    def load(cls, master_b64: str, data_dir: Path) -> "Vault":
        """Env/setting wins; otherwise a key file inside data_dir (created 0600 on first run)."""
        master_b64 = (master_b64 or os.environ.get("APIGATE_MASTER_KEY", "")).strip()
        if not master_b64:
            kf = data_dir / "master.key"
            if kf.exists():
                master_b64 = kf.read_text().strip()
            else:
                data_dir.mkdir(parents=True, exist_ok=True)
                master_b64 = cls.generate_master()
                fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(master_b64)
        try:
            return cls(_b64d(master_b64))
        except Exception as e:  # noqa: BLE001
            raise ValueError("invalid master key (expected base64url of 32 bytes)") from e

    # -- encryption ------------------------------------------------------
    def encrypt(self, plaintext: str, aad: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._enc).encrypt(nonce, plaintext.encode(), aad.encode())
        return b"v1" + nonce + ct

    def decrypt(self, blob: bytes, aad: str) -> str:
        if blob[:2] != b"v1":
            raise ValueError("unknown ciphertext version")
        return AESGCM(self._enc).decrypt(blob[2:14], blob[14:], aad.encode()).decode()

    # -- digests ---------------------------------------------------------
    def digest(self, value: str) -> str:
        return hmac.new(self._mac, value.encode(), hashlib.sha256).hexdigest()


# ---- tokens ---------------------------------------------------------------
def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def looks_like_api_key(s: str) -> bool:
    return s.startswith(API_KEY_PREFIX) and 20 < len(s) < 128


# ---- passwords --------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=96 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(stored: str, password: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=_b64d(salt), n=int(n), r=int(r), p=int(p), dklen=32, maxmem=96 * 1024 * 1024)
        return hmac.compare_digest(calc, _b64d(dk))
    except Exception:  # noqa: BLE001
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def verify_dummy(password: str) -> None:
    """Burn the same CPU as a real check (user-enumeration timing defence)."""
    verify_password(_DUMMY_HASH, password)


# ---- redaction ----------------------------------------------------------------
_KEY_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{16,})"
)


class RedactingFilter(logging.Filter):
    """Scrubs well-known key shapes and any registered exact secret from log records."""

    def __init__(self) -> None:
        super().__init__()
        self._exact: set[str] = set()

    def register(self, secret: str) -> None:
        if len(secret) >= 8:
            self._exact.add(secret)

    def forget(self, secret: str) -> None:
        self._exact.discard(secret)

    def scrub(self, text: str) -> str:
        for s in self._exact:
            if s in text:
                text = text.replace(s, "[redacted]")
        return _KEY_PATTERNS.sub("[redacted]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.scrub(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True


redactor = RedactingFilter()


def scrub(text: str) -> str:
    return redactor.scrub(text)
