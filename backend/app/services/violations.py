"""Violation lifecycle. Every status change goes through this module.

    DETECTED -> NOTIFIED -> IN_PROGRESS -> RESOLVED
    DETECTED / NOTIFIED may also go straight to IN_PROGRESS or RESOLVED (a late notification must
    not block an admin who is already on site).
    RESOLVED -> IN_PROGRESS only by reopen (SUPER_ADMIN).

Each change is a conditional UPDATE (WHERE status = <the status we read>), so when two admins click
at the same time exactly one wins and the other gets a 409. The history row is written in the same
transaction.
"""
from sqlalchemy import update

from ..errors import conflict
from ..extensions import db
from ..models import HistoryAction, Violation, ViolationHistory, ViolationStatus, utcnow

S = ViolationStatus

STATUS_LABELS = {S.DETECTED: "탐지됨", S.NOTIFIED: "알림 전송됨", S.IN_PROGRESS: "현장 대응 중", S.RESOLVED: "처리 완료"}


def add_history(violation_id: int, action: str, message: str | None = None, admin_id: int | None = None,
                previous_status: str | None = None, new_status: str | None = None) -> None:
    db.session.add(ViolationHistory(
        violation_id=violation_id, action=action, admin_id=admin_id,
        previous_status=previous_status, new_status=new_status, message=message,
    ))


def create_violation(machine, zone, detected_at) -> Violation:
    """Create the event for a suspected illegal parking. The caller commits."""
    lot = machine.parking_lot
    violation = Violation(
        company_id=lot.company_id,
        parking_lot_id=lot.id,
        machine_id=machine.id,
        parking_zone_id=zone.id if zone else None,
        detected_at=detected_at,
        status=S.DETECTED,
    )
    db.session.add(violation)
    db.session.flush()
    add_history(violation.id, HistoryAction.DETECTED, "AI가 불법주차 의심 차량을 탐지했습니다.",
                new_status=S.DETECTED)
    return violation


def _transition(violation: Violation, allowed_from: tuple, new_status: str, action: str, message: str | None,
                admin_id: int | None, extra_values: dict | None = None) -> Violation:
    previous = violation.status
    if previous not in allowed_from:
        raise conflict(
            f"현재 상태({STATUS_LABELS.get(previous, previous)})에서는 이 작업을 할 수 없습니다.",
            code="invalid_transition",
        )

    values = {"status": new_status, "updated_at": utcnow(), **(extra_values or {})}
    result = db.session.execute(
        update(Violation)
        .where(Violation.id == violation.id, Violation.status == previous)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise conflict("다른 관리자가 먼저 상태를 변경했습니다. 목록을 새로 고쳐 주세요.", code="invalid_transition")

    add_history(violation.id, action, message, admin_id=admin_id, previous_status=previous, new_status=new_status)
    db.session.commit()
    db.session.refresh(violation)
    return violation


def mark_notified(violation: Violation, recipient_count: int) -> Violation:
    return _transition(
        violation, (S.DETECTED,), S.NOTIFIED, HistoryAction.NOTIFIED,
        f"담당 관리자 {recipient_count}명에게 알림을 전송했습니다.", admin_id=None,
    )


def acknowledge(violation: Violation, admin) -> Violation:
    return _transition(
        violation, (S.DETECTED, S.NOTIFIED), S.IN_PROGRESS, HistoryAction.ACKNOWLEDGED,
        f"{admin.name}님이 확인하고 현장 대응을 시작했습니다.", admin_id=admin.id,
    )


def resolve(violation: Violation, admin, note: str | None) -> Violation:
    message = f"{admin.name}님이 처리 완료했습니다."
    if note:
        message += f" 처리 내용: {note}"
    return _transition(
        violation, (S.DETECTED, S.NOTIFIED, S.IN_PROGRESS), S.RESOLVED, HistoryAction.RESOLVED, message,
        admin_id=admin.id,
        extra_values={"resolved_at": utcnow(), "resolved_by": admin.id, "resolution_note": note or None},
    )


def reopen(violation: Violation, admin, message: str) -> Violation:
    return _transition(
        violation, (S.RESOLVED,), S.IN_PROGRESS, HistoryAction.REOPENED,
        f"{admin.name}님이 처리 완료를 되돌렸습니다. 사유: {message}", admin_id=admin.id,
        extra_values={"resolved_at": None, "resolved_by": None, "resolution_note": None},
    )
