"""Login, token refresh (rotation) and logout."""
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select

from ..auth import require_auth
from ..auth.security import create_access_token, new_refresh_token, sha256_hex, verify_password
from ..errors import ApiError, bad_request, forbidden, unauthorized
from ..extensions import db
from ..models import Admin, RefreshToken, Role, Status, as_utc, utcnow
from ..serializers import admin_to_dict
from .utils import json_body

bp = Blueprint("auth", __name__)

COOKIE_PATH = "/api/v1/auth"


def _ensure_usable(admin: Admin) -> None:
    if admin.status != Status.ACTIVE:
        raise forbidden("비활성화된 계정입니다.")
    if admin.role == Role.COMPANY_ADMIN and admin.company.status != Status.ACTIVE:
        raise forbidden("비활성화된 회사의 계정입니다.")


def _issue_session(admin: Admin):
    """Create a new refresh token row, return the access token and set the refresh cookie."""
    config = current_app.config
    raw, token_hash = new_refresh_token()
    days = config["REFRESH_TOKEN_DAYS"]
    db.session.add(RefreshToken(admin_id=admin.id, token_hash=token_hash, expires_at=utcnow() + timedelta(days=days)))
    db.session.commit()

    access_token, expires_in = create_access_token(admin)
    response = jsonify({"access_token": access_token, "expires_in": expires_in, "admin": admin_to_dict(admin)})
    response.set_cookie(
        config["REFRESH_COOKIE_NAME"], raw, max_age=days * 86400, httponly=True,
        secure=config["COOKIE_SECURE"], samesite=config["COOKIE_SAMESITE"], path=COOKIE_PATH,
    )
    return response


def _current_refresh_token() -> RefreshToken | None:
    raw = request.cookies.get(current_app.config["REFRESH_COOKIE_NAME"])
    if not raw:
        return None
    return db.session.scalar(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw)))


@bp.post("/auth/login")
def login():
    data = json_body()
    email = str(data.get("email") or "").strip().lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        raise bad_request("이메일과 비밀번호를 입력해 주세요.")

    admin = db.session.scalar(select(Admin).where(Admin.email == email))
    if admin is None or not verify_password(password, admin.password_hash):
        raise ApiError(401, "invalid_credentials", "이메일 또는 비밀번호가 일치하지 않습니다.")
    _ensure_usable(admin)
    return _issue_session(admin)


@bp.post("/auth/refresh")
def refresh():
    token = _current_refresh_token()
    if token is None or token.revoked_at is not None or as_utc(token.expires_at) <= utcnow():
        raise unauthorized("다시 로그인해 주세요.")
    admin = token.admin
    _ensure_usable(admin)
    token.revoked_at = utcnow()  # rotation: each refresh token is usable once
    return _issue_session(admin)


@bp.post("/auth/logout")
def logout():
    token = _current_refresh_token()
    if token is not None and token.revoked_at is None:
        token.revoked_at = utcnow()
        db.session.commit()
    response = jsonify({"ok": True})
    response.delete_cookie(current_app.config["REFRESH_COOKIE_NAME"], path=COOKIE_PATH)
    return response


@bp.get("/auth/me")
@require_auth()
def me():
    return jsonify({"admin": admin_to_dict(g.admin)})
