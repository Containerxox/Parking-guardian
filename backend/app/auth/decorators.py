"""Authentication decorators and tenant-scope helpers.

Tenant isolation rules (see docs/design/API.md):
- A COMPANY_ADMIN's company always comes from the verified token, never from the request.
- Naming another company explicitly (query/body/path) is a 403.
- Loading another company's resource by id is a 404, so its existence is not revealed.
"""
from functools import wraps

import jwt
from flask import current_app, g, request
from sqlalchemy import select

from ..errors import ApiError, forbidden, not_found, unauthorized
from ..extensions import db
from ..models import Admin, Machine, Role, Status, utcnow
from .security import decode_token, sha256_hex


def require_auth(*roles: str):
    """Require a valid access token. With roles given, also require one of those roles."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise unauthorized()
            try:
                payload = decode_token(header[7:].strip(), "access")
            except jwt.ExpiredSignatureError:
                raise unauthorized("인증이 만료되었습니다.", code="token_expired")
            except jwt.InvalidTokenError:
                raise unauthorized("유효하지 않은 인증 정보입니다.")

            admin = db.session.get(Admin, int(payload["sub"]))
            if admin is None or admin.status != Status.ACTIVE:
                raise unauthorized("사용할 수 없는 계정입니다.")
            if admin.role == Role.COMPANY_ADMIN and admin.company.status != Status.ACTIVE:
                raise forbidden("비활성화된 회사의 계정입니다.")
            if roles and admin.role not in roles:
                raise forbidden()

            g.admin = admin
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_device(view):
    """Authenticate a machine by its API key (X-Device-Key)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        api_key = request.headers.get("X-Device-Key", "").strip()
        if not api_key:
            raise unauthorized("장비 인증 키가 필요합니다.")
        machine = db.session.scalar(select(Machine).where(Machine.api_key_hash == sha256_hex(api_key)))
        if machine is None:
            raise unauthorized("유효하지 않은 장비 인증 키입니다.")
        lot = machine.parking_lot
        if machine.status != Status.ACTIVE or lot.status != Status.ACTIVE or lot.company.status != Status.ACTIVE:
            raise forbidden("비활성화된 장비, 주차장 또는 회사입니다.")
        g.machine = machine
        return view(*args, **kwargs)

    return wrapper


# --- Tenant scope ------------------------------------------------------------
def scoped_company_id(requested=None) -> int | None:
    """Company filter to apply to a list query.

    COMPANY_ADMIN: always their own company (403 if they asked for another one).
    SUPER_ADMIN: the requested company, or None meaning "all companies".
    """
    admin: Admin = g.admin
    requested_id = _to_int(requested)
    if admin.role == Role.COMPANY_ADMIN:
        if requested_id is not None and requested_id != admin.company_id:
            raise forbidden("다른 회사의 데이터에는 접근할 수 없습니다.")
        return admin.company_id
    return requested_id


def ensure_company_access(company_id: int | None) -> None:
    """Call after loading a resource by id. Hides other companies' resources behind a 404."""
    admin: Admin = g.admin
    if admin.role == Role.COMPANY_ADMIN and company_id != admin.company_id:
        raise not_found()


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "validation_error", "company_id 형식이 올바르지 않습니다.")


def touch_heartbeat(machine: Machine) -> None:
    machine.last_heartbeat_at = utcnow()


def machine_online(machine: Machine) -> bool:
    from ..models import as_utc

    last = as_utc(machine.last_heartbeat_at)
    if last is None:
        return False
    return (utcnow() - last).total_seconds() <= current_app.config["MACHINE_ONLINE_SECONDS"]
