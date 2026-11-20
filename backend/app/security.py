"""Password hashing and signed tokens using only the standard library."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.settings import settings

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 64}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), **_SCRYPT)
    return hmac.compare_digest(candidate, _unb64(digest))


def create_token(purpose: str, subject: str, ttl_seconds: int, **claims: str) -> str:
    payload = {"pur": purpose, "sub": subject, "exp": int(time.time()) + ttl_seconds, **claims}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body)}"


def read_token(token: str, purpose: str) -> dict[str, object] | None:
    try:
        body, signature = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("pur") != purpose or int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def _sign(body: str) -> str:
    return _b64(hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest())


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
