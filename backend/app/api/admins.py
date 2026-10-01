"""Admin accounts and their parking-lot assignments."""
from flask import Blueprint, jsonify, request
from sqlalchemy import select

from ..auth import ensure_company_access, require_auth, scoped_company_id
from ..auth.security import hash_password
from ..errors import bad_request, conflict
from ..extensions import db
from ..models import Admin, AdminParkingLot, Company, ParkingLot, Role
from ..serializers import admin_to_dict
from ..services import audit
from .utils import get_or_404, json_body, list_response, optional_text, required_text, status_value

bp = Blueprint("admins", __name__)

MIN_PASSWORD_LENGTH = 8


def _password(data: dict, required: bool) -> str | None:
    password = data.get("password")
    if password is None and not required:
        return None
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise bad_request(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    return password


@bp.get("/admins")
@require_auth()
def list_admins():
    company_id = scoped_company_id(request.args.get("company_id"))
    stmt = select(Admin).where(Admin.role == Role.COMPANY_ADMIN).order_by(Admin.id)
    if company_id is not None:
        stmt = stmt.where(Admin.company_id == company_id)
    return list_response([admin_to_dict(a) for a in db.session.scalars(stmt)])


@bp.post("/admins")
@require_auth(Role.SUPER_ADMIN)
def create_admin():
    data = json_body()
    email = required_text(data, "email", "이메일").lower()
    name = required_text(data, "name", "이름", 100)
    password = _password(data, required=True)
    company_id = data.get("company_id")
    if not isinstance(company_id, int):
        raise bad_request("company_id는 필수입니다.")
    if "@" not in email:
        raise bad_request("이메일 형식이 올바르지 않습니다.")

    company = get_or_404(Company, company_id)
    if db.session.scalar(select(Admin).where(Admin.email == email)):
        raise conflict("이미 사용 중인 이메일입니다.")

    admin = Admin(email=email, name=name, password_hash=hash_password(password), role=Role.COMPANY_ADMIN,
                  company_id=company.id)
    db.session.add(admin)
    db.session.flush()
    audit.record("ADMIN_CREATED", "admin", admin.id, after={"email": email, "name": name}, company_id=company.id)
    db.session.commit()
    return jsonify(admin_to_dict(admin)), 201


@bp.patch("/admins/<int:admin_id>")
@require_auth(Role.SUPER_ADMIN)
def update_admin(admin_id: int):
    admin = get_or_404(Admin, admin_id)
    if admin.role != Role.COMPANY_ADMIN:
        raise bad_request("회사 관리자 계정만 수정할 수 있습니다.")
    data = json_body()
    before = {"name": admin.name, "status": admin.status}

    name = optional_text(data, "name", 100)
    if name:
        admin.name = name
    status = status_value(data)
    if status:
        admin.status = status
    password = _password(data, required=False)
    if password:
        admin.password_hash = hash_password(password)

    after = {"name": admin.name, "status": admin.status}
    if after != before:
        action = "ADMIN_STATUS_CHANGED" if before["status"] != after["status"] else "ADMIN_UPDATED"
        audit.record(action, "admin", admin.id, before=before, after=after, company_id=admin.company_id)
    if password:
        audit.record("ADMIN_PASSWORD_RESET", "admin", admin.id, company_id=admin.company_id)
    db.session.commit()
    return jsonify(admin_to_dict(admin))


@bp.put("/admins/<int:admin_id>/parking-lots")
@require_auth(Role.SUPER_ADMIN)
def replace_assignments(admin_id: int):
    """Replace the full set of parking lots this admin is responsible for.

    SUPER_ADMIN only: an admin must not be able to remove themselves (or a colleague) from
    notifications.
    """
    admin = get_or_404(Admin, admin_id)
    ensure_company_access(admin.company_id)
    if admin.role != Role.COMPANY_ADMIN:
        raise bad_request("회사 관리자에게만 담당 주차장을 배정할 수 있습니다.")

    lot_ids = json_body().get("parking_lot_ids")
    if not isinstance(lot_ids, list) or not all(isinstance(i, int) for i in lot_ids):
        raise bad_request("parking_lot_ids는 숫자 배열이어야 합니다.")
    lot_ids = sorted(set(lot_ids))

    lots = db.session.scalars(select(ParkingLot).where(ParkingLot.id.in_(lot_ids))).all() if lot_ids else []
    # Application-level check. The composite foreign keys enforce the same rule in the database.
    if len(lots) != len(lot_ids) or any(lot.company_id != admin.company_id for lot in lots):
        raise bad_request("같은 회사의 주차장만 배정할 수 있습니다.")

    before = sorted(a.parking_lot_id for a in admin.assignments)
    current = {a.parking_lot_id: a for a in admin.assignments}
    for lot_id, assignment in current.items():
        if lot_id not in lot_ids:
            admin.assignments.remove(assignment)
    for lot_id in lot_ids:
        if lot_id not in current:
            admin.assignments.append(AdminParkingLot(parking_lot_id=lot_id, company_id=admin.company_id))

    if before != lot_ids:
        audit.record("ADMIN_ASSIGNMENT_CHANGED", "admin", admin.id, before={"parking_lot_ids": before},
                     after={"parking_lot_ids": lot_ids}, company_id=admin.company_id)
    db.session.commit()
    db.session.refresh(admin)
    return jsonify(admin_to_dict(admin))
