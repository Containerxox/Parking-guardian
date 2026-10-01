"""System audit log: who changed which administrative resource."""
from flask import g

from ..extensions import db
from ..models import AuditLog


def record(action: str, resource_type: str, resource_id=None, before=None, after=None, company_id=None) -> None:
    """Add an audit entry to the current transaction. The caller commits."""
    actor = getattr(g, "admin", None)
    db.session.add(AuditLog(
        actor_id=actor.id if actor else None,
        company_id=company_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        before_value=before,
        after_value=after,
    ))
