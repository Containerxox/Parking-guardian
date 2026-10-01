"""Small request helpers shared by the API modules."""
from flask import jsonify, request
from sqlalchemy import func, select

from ..errors import bad_request, not_found
from ..extensions import db
from ..models import Status


def json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise bad_request("JSON 본문이 필요합니다.")
    return data


def required_text(data: dict, key: str, label: str, max_length: int = 255) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"{label}은(는) 필수입니다.")
    value = value.strip()
    if len(value) > max_length:
        raise bad_request(f"{label}은(는) {max_length}자 이하여야 합니다.")
    return value


def optional_text(data: dict, key: str, max_length: int = 255) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request(f"{key} 형식이 올바르지 않습니다.")
    value = value.strip()
    if len(value) > max_length:
        raise bad_request(f"{key}은(는) {max_length}자 이하여야 합니다.")
    return value or None


def status_value(data: dict) -> str | None:
    value = data.get("status")
    if value is None:
        return None
    if value not in Status.ALL:
        raise bad_request("status는 ACTIVE 또는 INACTIVE여야 합니다.")
    return value


def int_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        raise bad_request(f"{name} 형식이 올바르지 않습니다.")


def get_or_404(model, object_id: int):
    obj = db.session.get(model, object_id)
    if obj is None:
        raise not_found()
    return obj


def list_response(items: list, **extra):
    return jsonify({"items": items, "total": len(items), **extra})


def paginated_response(stmt, serializer, default_size: int = 20, **extra):
    page = max(1, int_arg("page") or 1)
    size = min(100, max(1, int_arg("size") or default_size))
    total = db.session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.session.scalars(stmt.limit(size).offset((page - 1) * size)).all()
    return jsonify({"items": [serializer(r) for r in rows], "total": total, "page": page, "size": size, **extra})
