"""Password hashing, JWT handling and opaque key generation."""
import hashlib
import secrets
from datetime import timedelta

import bcrypt
import jwt
from flask import current_app

from ..models import utcnow

ALGORITHM = "HS256"


# --- Passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    rounds = current_app.config.get("BCRYPT_ROUNDS", 12)  # tests lower this to stay fast
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# --- JWT -------------------------------------------------------------------
def _encode(payload: dict, ttl: timedelta) -> str:
    now = utcnow()
    payload = {**payload, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, current_app.config["JWT_SECRET"], algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict:
    """Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError."""
    payload = jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=[ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("unexpected token type")
    return payload


def create_access_token(admin) -> tuple[str, int]:
    minutes = current_app.config["ACCESS_TOKEN_MINUTES"]
    token = _encode(
        {"type": "access", "sub": str(admin.id), "role": admin.role, "company_id": admin.company_id},
        timedelta(minutes=minutes),
    )
    return token, minutes * 60


def create_image_token(violation_id: int) -> tuple[str, int]:
    """Short-lived token that authorizes reading one violation image (local stand-in for an S3 presigned URL)."""
    ttl = current_app.config["IMAGE_URL_TTL_SECONDS"]
    return _encode({"type": "image", "vid": violation_id}, timedelta(seconds=ttl)), ttl


# --- Opaque secrets (refresh tokens, device API keys) ------------------------
def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, sha256_hex(raw)


def new_device_api_key() -> tuple[str, str]:
    raw = "pgk_" + secrets.token_urlsafe(32)
    return raw, sha256_hex(raw)
