"""Violation queries, handling actions and image access."""
import jwt
from flask import Blueprint, Response, g, jsonify, request, url_for
from sqlalchemy import select

from .. import adapters
from ..adapters.storage import sniff_image_type
from ..auth import ensure_company_access, require_auth, scoped_company_id
from ..auth.security import create_image_token, decode_token
from ..errors import bad_request, not_found
from ..extensions import db
from ..models import ImageStatus, Role, Violation, ViolationStatus
from ..serializers import history_to_dict, violation_to_dict
from ..services import violations as violation_service
from .utils import get_or_404, int_arg, json_body, optional_text, paginated_response

bp = Blueprint("violations", __name__)


def _load_violation(violation_id: int) -> Violation:
    violation = get_or_404(Violation, violation_id)
    ensure_company_access(violation.company_id)
    return violation


@bp.get("/violations")
@require_auth()
def list_violations():
    company_id = scoped_company_id(request.args.get("company_id"))
    stmt = select(Violation).order_by(Violation.detected_at.desc(), Violation.id.desc())
    if company_id is not None:
        stmt = stmt.where(Violation.company_id == company_id)

    status = request.args.get("status")
    if status == "open":
        stmt = stmt.where(Violation.status.in_(ViolationStatus.OPEN))
    elif status == "resolved":
        stmt = stmt.where(Violation.status == ViolationStatus.RESOLVED)
    elif status in ViolationStatus.ALL:
        stmt = stmt.where(Violation.status == status)
    elif status:
        raise bad_request("status 값이 올바르지 않습니다.")

    lot_id = int_arg("parking_lot_id")
    if lot_id is not None:
        stmt = stmt.where(Violation.parking_lot_id == lot_id)

    # assigned=true narrows the list to the parking lots this admin is responsible for.
    # It is a convenience filter, not a permission: company-wide viewing stays available.
    if g.admin.role == Role.COMPANY_ADMIN and request.args.get("assigned", "").lower() in ("1", "true", "yes"):
        stmt = stmt.where(Violation.parking_lot_id.in_([a.parking_lot_id for a in g.admin.assignments]))
    return paginated_response(stmt, violation_to_dict)


@bp.get("/violations/<int:violation_id>")
@require_auth()
def get_violation(violation_id: int):
    violation = _load_violation(violation_id)
    body = violation_to_dict(violation)
    body["history"] = [history_to_dict(h) for h in violation.history]
    return jsonify(body)


@bp.post("/violations/<int:violation_id>/acknowledge")
@require_auth()
def acknowledge_violation(violation_id: int):
    violation = violation_service.acknowledge(_load_violation(violation_id), g.admin)
    return jsonify(violation_to_dict(violation))


@bp.post("/violations/<int:violation_id>/resolve")
@require_auth()
def resolve_violation(violation_id: int):
    data = request.get_json(silent=True) or {}
    note = optional_text(data, "resolution_note", 1000) if isinstance(data, dict) else None
    violation = violation_service.resolve(_load_violation(violation_id), g.admin, note)
    return jsonify(violation_to_dict(violation))


@bp.post("/violations/<int:violation_id>/reopen")
@require_auth(Role.SUPER_ADMIN)
def reopen_violation(violation_id: int):
    message = optional_text(json_body(), "message", 1000)
    if not message:
        raise bad_request("되돌리는 사유를 입력해 주세요.")
    violation = violation_service.reopen(_load_violation(violation_id), g.admin, message)
    return jsonify(violation_to_dict(violation))


@bp.get("/violations/<int:violation_id>/image-url")
@require_auth()
def violation_image_url(violation_id: int):
    """Return a short-lived URL for the image. With S3 this becomes a presigned GET URL."""
    violation = _load_violation(violation_id)
    if not violation.image_key or violation.image_status != ImageStatus.UPLOADED:
        raise not_found("이미지가 없습니다.")
    token, ttl = create_image_token(violation.id)
    return jsonify({"url": url_for("violations.serve_image", token=token, _external=True), "expires_in": ttl})


@bp.get("/images/<token>")
def serve_image(token: str):
    """No login header here: <img> tags cannot send one. The signed token is the authorization."""
    try:
        payload = decode_token(token, "image")
    except jwt.InvalidTokenError:  # includes expiry
        raise not_found("이미지 주소가 만료되었거나 올바르지 않습니다.")
    violation = db.session.get(Violation, payload.get("vid"))
    if violation is None or not violation.image_key:
        raise not_found("이미지가 없습니다.")
    try:
        data = adapters.storage().read(violation.image_key)
    except (FileNotFoundError, ValueError):
        raise not_found("이미지가 없습니다.")
    response = Response(data, mimetype=sniff_image_type(data)[1])
    response.headers["Cache-Control"] = "private, max-age=60"
    return response
