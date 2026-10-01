"""Company (tenant) management and the dashboard summary."""
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify
from sqlalchemy import func, or_, select

from ..auth import require_auth, scoped_company_id
from ..errors import conflict, forbidden
from ..extensions import db
from ..models import (Admin, Company, HistoryAction, Machine, Notification, NotificationStatus, ParkingLot, Role,
                      Status, Violation, ViolationHistory, ViolationStatus, utcnow)
from ..serializers import company_to_dict
from ..services import audit
from .utils import get_or_404, json_body, list_response, optional_text, required_text, status_value

bp = Blueprint("companies", __name__)


@bp.get("/companies")
@require_auth(Role.SUPER_ADMIN)
def list_companies():
    companies = db.session.scalars(select(Company).order_by(Company.id)).all()
    return list_response([company_to_dict(c) for c in companies])


@bp.post("/companies")
@require_auth(Role.SUPER_ADMIN)
def create_company():
    name = required_text(json_body(), "name", "회사 이름", 100)
    if db.session.scalar(select(Company).where(Company.name == name)):
        raise conflict("이미 등록된 회사 이름입니다.")
    company = Company(name=name)
    db.session.add(company)
    db.session.flush()
    audit.record("COMPANY_CREATED", "company", company.id, after={"name": name}, company_id=company.id)
    db.session.commit()
    return jsonify(company_to_dict(company)), 201


@bp.get("/companies/<int:company_id>")
@require_auth()
def get_company(company_id: int):
    # The company is named explicitly in the path, so another company's id is a 403.
    if g.admin.role == Role.COMPANY_ADMIN and g.admin.company_id != company_id:
        raise forbidden("다른 회사의 데이터에는 접근할 수 없습니다.")
    return jsonify(company_to_dict(get_or_404(Company, company_id)))


@bp.patch("/companies/<int:company_id>")
@require_auth(Role.SUPER_ADMIN)
def update_company(company_id: int):
    company = get_or_404(Company, company_id)
    data = json_body()
    before = {"name": company.name, "status": company.status}

    name = optional_text(data, "name", 100)
    if name and name != company.name:
        if db.session.scalar(select(Company).where(Company.name == name, Company.id != company.id)):
            raise conflict("이미 등록된 회사 이름입니다.")
        company.name = name
    status = status_value(data)
    if status:
        company.status = status

    after = {"name": company.name, "status": company.status}
    if after != before:
        action = "COMPANY_STATUS_CHANGED" if before["status"] != after["status"] else "COMPANY_UPDATED"
        audit.record(action, "company", company.id, before=before, after=after, company_id=company.id)
    db.session.commit()
    return jsonify(company_to_dict(company))


@bp.get("/dashboard/summary")
@require_auth()
def dashboard_summary():
    company_id = scoped_company_id()

    def count(model, *conditions):
        return db.session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0

    def scoped(column):
        return (column == company_id,) if company_id is not None else ()

    machines_stmt = select(func.count()).select_from(Machine).join(ParkingLot, Machine.parking_lot_id == ParkingLot.id)
    if company_id is not None:
        machines_stmt = machines_stmt.where(ParkingLot.company_id == company_id)

    # --- Operations indicators: each one means "someone has to look at this" ---
    now = utcnow()
    open_violation = (Violation.status.in_(ViolationStatus.OPEN), *scoped(Violation.company_id))

    # 1. Open violations nobody finished within STALE_VIOLATION_HOURS
    stale_before = now - timedelta(hours=current_app.config["STALE_VIOLATION_HOURS"])
    stale_violations = count(Violation, *open_violation, Violation.detected_at < stale_before)

    # 2. Machines in service that stopped sending signals (uploads and heartbeats both count as a signal)
    online_after = now - timedelta(seconds=current_app.config["MACHINE_ONLINE_SECONDS"])
    offline_stmt = (
        select(func.count()).select_from(Machine).join(ParkingLot, Machine.parking_lot_id == ParkingLot.id)
        .where(Machine.status == Status.ACTIVE,
               or_(Machine.last_heartbeat_at.is_(None), Machine.last_heartbeat_at < online_after))
    )
    if company_id is not None:
        offline_stmt = offline_stmt.where(ParkingLot.company_id == company_id)

    # 3. Open violations whose notification did not reach everyone it should have:
    #    a delivery failed, or the event could not be published / the lot has no assigned admin
    #    (both recorded as NOTIFY_FAILED in the violation history).
    failed_delivery = select(Notification.violation_id).where(Notification.status == NotificationStatus.FAILED)
    failed_publish = select(ViolationHistory.violation_id).where(ViolationHistory.action == HistoryAction.NOTIFY_FAILED)
    failed_notifications = count(
        Violation, *open_violation,
        or_(Violation.id.in_(failed_delivery),
            (Violation.status == ViolationStatus.DETECTED) & Violation.id.in_(failed_publish)),
    )

    return jsonify({
        "stale_violations": stale_violations,
        "stale_violation_hours": current_app.config["STALE_VIOLATION_HOURS"],
        "offline_machines": db.session.scalar(offline_stmt) or 0,
        "failed_notifications": failed_notifications,
        "companies": 1 if company_id is not None else count(Company),
        "admins": count(Admin, Admin.role == Role.COMPANY_ADMIN, *scoped(Admin.company_id)),
        "parking_lots": count(ParkingLot, *scoped(ParkingLot.company_id)),
        "machines": db.session.scalar(machines_stmt) or 0,
        "open_violations": count(Violation, Violation.status.in_(ViolationStatus.OPEN), *scoped(Violation.company_id)),
        "resolved_violations": count(Violation, Violation.status == ViolationStatus.RESOLVED,
                                     *scoped(Violation.company_id)),
    })
