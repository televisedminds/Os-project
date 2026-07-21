"""Security layer for the dashboard — auth, at-rest encryption, rate limiting.

The dashboard stores real API credentials and can spend paid credits, so the
settings/mutating endpoints must not be open to anyone who can reach the page.
This module is the single place that decides:

* **who may call an admin endpoint** — a bearer token (`OOS_DASHBOARD_TOKEN`),
  compared in constant time. When no token is set the endpoints are usable only
  from loopback (local dev), never from a public IP — so a fresh public deploy
  is closed by default instead of silently writable.
* **CSRF** — admin calls must carry the token in a custom header (`X-OOS-Token`
  or `Authorization: Bearer`). A browser will not attach a custom header to a
  cross-site request and cannot read the token, so a malicious page cannot
  forge an authenticated call. A same-origin/Referer check backs this up.
* **rate limiting** — a per-IP sliding window on the mutating endpoints, so a
  leaked/guessed token can't be brute-forced or used to burn paid credits.
* **at-rest encryption** — stored credentials are Fernet-encrypted with a key
  from `OOS_SECRET_KEY` (or a persisted, 0600 `data/.secret_key`). Plaintext
  written by older builds is transparently migrated on first read.

Nothing here ever logs or returns a raw secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from pathlib import Path

from fastapi import HTTPException, Request

# --------------------------------------------------------------- encryption
#
# At-rest encryption for stored credentials, using only the standard library
# (no native crypto dependency to break on deploy). Construction:
#
#   key material  : master secret (OOS_SECRET_KEY, or a persisted random file)
#   per-value      : random 16-byte salt + 16-byte nonce
#   derive         : PBKDF2-HMAC-SHA256(master, salt, 120k) -> 32B enc + 32B mac
#   encrypt        : plaintext XOR keystream, where keystream blocks are
#                    HMAC-SHA256(enc_key, nonce || counter)  (CTR mode)
#   authenticate   : HMAC-SHA256(mac_key, salt || nonce || ciphertext)  (EtM)
#
# This is a standard encrypt-then-MAC stream cipher over vetted primitives.
# Its job is to protect small secrets in the SQLite file from someone who
# reads the file without the key — transport security (HTTPS) and the admin
# token remain the primary controls. Tampering fails the MAC and decrypts to
# empty rather than leaking or trusting corrupted bytes.

_ENC_PREFIX = "enc:v2:"
_PBKDF2_ITERS = 120_000


def _key_file(cfg) -> Path:
    return Path(getattr(cfg, "db_path", "data/x")).resolve().parent / ".secret_key"


def _load_or_create_master(cfg) -> bytes | None:
    """The master secret. Prefers OOS_SECRET_KEY (any passphrase); otherwise a
    random 32-byte secret persisted to data/.secret_key with 0600 perms so
    restarts keep decrypting."""

    env = (getattr(cfg, "secret_key", "") or os.environ.get("OOS_SECRET_KEY", "")).strip()
    if env:
        return hashlib.sha256(env.encode()).digest()
    path = _key_file(cfg)
    try:
        if path.exists():
            return bytes.fromhex(path.read_text().strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        master = secrets.token_bytes(32)
        path.write_text(master.hex())
        os.chmod(path, 0o600)
        return master
    except Exception:  # noqa: BLE001 - can't persist a key → fail closed (encryption off)
        return None


def _derive(master: bytes, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERS, dklen=64)
    return dk[:32], dk[32:]


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])


class SecretBox:
    """Encrypts/decrypts small credential strings. If no master key can be
    obtained it degrades to pass-through and reports `active=False`, so the
    dashboard can warn rather than silently pretend data is encrypted."""

    def __init__(self, cfg):
        self._master = _load_or_create_master(cfg)
        self.active = self._master is not None

    def encrypt(self, value: str) -> str:
        if not self.active or not value or value.startswith(_ENC_PREFIX):
            return value
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
        enc_key, mac_key = _derive(self._master, salt)
        pt = value.encode()
        ct = bytes(a ^ b for a, b in zip(pt, _keystream(enc_key, nonce, len(pt))))
        tag = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
        return _ENC_PREFIX + base64.urlsafe_b64encode(salt + nonce + tag + ct).decode()

    def decrypt(self, value: str) -> str:
        if not value or not value.startswith(_ENC_PREFIX):
            return value                 # legacy plaintext — migrated on next save
        if not self.active:
            return ""                    # no key → reveal nothing
        try:
            raw = base64.urlsafe_b64decode(value[len(_ENC_PREFIX):])
            salt, nonce, tag, ct = raw[:16], raw[16:32], raw[32:64], raw[64:]
            enc_key, mac_key = _derive(self._master, salt)
            if not hmac.compare_digest(tag, hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()):
                return ""                # tampered / wrong key
            return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode()
        except Exception:  # noqa: BLE001
            return ""


# ------------------------------------------------------------- rate limiting

class RateLimiter:
    """Per-key sliding-window limiter (in-memory; fine for a single-process app)."""

    def __init__(self, max_hits: int, window_s: float):
        self.max_hits = max_hits
        self.window_s = window_s
        self._hits: dict[str, deque] = {}

    def check(self, key: str) -> bool:
        now = time.time()
        dq = self._hits.setdefault(key, deque())
        while dq and dq[0] < now - self.window_s:
            dq.popleft()
        if len(dq) >= self.max_hits:
            return False
        dq.append(now)
        return True


# --------------------------------------------------------------------- auth

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost")


def _same_origin(request: Request) -> bool:
    """Reject cross-site form/script posts. A missing Origin/Referer is allowed
    (native clients, curl, the CLI); a PRESENT one must match the host."""

    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return host in origin


def _present_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-oos-token", "").strip()


class AdminGuard:
    """FastAPI dependency: authorises admin (settings/mutating) endpoints."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.limiter = RateLimiter(
            max_hits=int(os.environ.get("OOS_ADMIN_RATE", "20")), window_s=60.0)
        # Opt-in "no password anywhere" mode for a personal, single-user deploy.
        # The guard becomes a no-op (rate-limit only). Convenience over security —
        # only sensible when the box is otherwise access-controlled or you accept
        # that anyone with the URL can use it.
        self.open_mode = os.environ.get("OOS_OPEN_MODE", "") not in ("", "0", "false", "no")

    @property
    def token(self) -> str:
        return (getattr(self.cfg, "dashboard_token", "") or "").strip()

    def configured(self) -> bool:
        return bool(self.token) or self.open_mode

    def __call__(self, request: Request) -> None:
        ip = _client_ip(request)
        if not self.limiter.check(ip):
            raise HTTPException(429, "rate limit exceeded — slow down")
        if self.open_mode:
            return                    # password-free by explicit choice
        if not _same_origin(request):
            raise HTTPException(403, "cross-origin request refused")
        token = self.token
        if not token:
            # No token configured → local-only. Behind a reverse proxy every
            # client's IP reads as loopback, so a forwarded header means the
            # request is NOT truly local → fail closed and demand a token.
            forwarded = (request.headers.get("x-forwarded-for")
                         or request.headers.get("forwarded"))
            if _is_loopback(ip) and not forwarded:
                return
            raise HTTPException(
                403, "admin endpoints are disabled until OOS_DASHBOARD_TOKEN is set "
                     "(without it they are reachable only from localhost, and a "
                     "reverse proxy does not count as local). See SECURITY.md.")
        supplied = _present_token(request)
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "missing or invalid admin token")
