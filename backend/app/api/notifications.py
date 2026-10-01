"""In-app notifications for the logged-in admin, and the audit log for SUPER_ADMIN."""
from flask import Blueprint, g, jsonify, request
from sqlalchemy import func, select, update

from ..auth import require_auth
from ..errors import not_found
from ..extensions import db
from ..models import AuditLog, Channel, Notification, NotificationStatus, Role, utcnow
from ..serializers import audit_to_dict, notification_to_dict
from .utils import int_arg, paginated_response

bp = Blueprint("notifications", __name__)


def _mine():
    return (Notification.admin_id == g.admin.id, Notification.channel == Channel.IN_APP)


@bp.get("/notifications")
@require_auth()
def list_notifications():
    stmt = select(Notification).where(*_mine()).order_by(Notification.created_at.desc(), Notification.id.desc())
    if request.args.get("unread", "").lower() in ("1", "true", "yes"):
        stmt = stmt.where(Notification.status != NotificationStatus.READ)
    unread_count = db.session.scalar(
        select(func.count()).select_from(Notification)
        .where(*_mine(), Notification.status != NotificationStatus.READ)
    ) or 0
    return paginated_response(stmt, notification_to_dict, unread_count=unread_count)


@bp.post("/notifications/<int:notification_id>/read")
@require_auth()
def read_notification(notification_id: int):
    notification = db.session.scalar(select(Notification).where(Notification.id == notification_id, *_mine()))
    if notification is None:
        raise not_found()
    if notification.status != NotificationStatus.READ:
        notification.status = NotificationStatus.READ
        notification.read_at = utcnow()
        db.session.commit()
    return jsonify(notification_to_dict(notification))


@bp.post("/notifications/read-all")
@require_auth()
def read_all_notifications():
    result = db.session.execute(
        update(Notification)
        .where(*_mine(), Notification.status != NotificationStatus.READ)
        .values(status=NotificationStatus.READ, read_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return jsonify({"updated": result.rowcount})


@bp.get("/audit-logs")
@require_auth(Role.SUPER_ADMIN)
def list_audit_logs():
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    company_id = int_arg("company_id")
    if company_id is not None:
        stmt = stmt.where(AuditLog.company_id == company_id)
    return paginated_response(stmt, audit_to_dict, default_size=50)
