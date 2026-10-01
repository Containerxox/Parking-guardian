"""Violation events, their history, notifications and the system audit log."""
from ..extensions import db
from .base import Channel, ImageStatus, NotificationStatus, ViolationStatus, in_list, utcnow


class Violation(db.Model):
    """One suspected illegal-parking event. Never deleted: handling changes its status."""

    __tablename__ = "violations"

    id = db.Column(db.Integer, primary_key=True)
    # company_id / parking_lot_id are a snapshot of where the event happened. They make tenant
    # filtering a single-column condition and stay correct even if the machine is moved later.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False)
    parking_lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id", ondelete="RESTRICT"), nullable=False)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False, index=True)
    parking_zone_id = db.Column(db.Integer, db.ForeignKey("parking_zones.id", ondelete="SET NULL"), nullable=True)

    detected_at = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=ViolationStatus.DETECTED)

    # One parked vehicle = one violation ("episode"). A device uploads a photo every minute, so the
    # same vehicle is seen many times: later sightings update these fields instead of creating rows.
    # ended_at IS NULL means the vehicle is still there. It is independent of `status`: an admin may
    # resolve the violation while the vehicle is still parked, and that must not start a new one.
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=True)
    detection_count = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)
    end_reason = db.Column(db.String(30), nullable=True)

    image_key = db.Column(db.String(500), nullable=True)
    image_status = db.Column(db.String(20), nullable=False, default=ImageStatus.PENDING)
    image_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)
    resolution_note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    company = db.relationship("Company")
    parking_lot = db.relationship("ParkingLot")
    machine = db.relationship("Machine")
    parking_zone = db.relationship("ParkingZone")
    resolver = db.relationship("Admin", foreign_keys=[resolved_by])
    history = db.relationship("ViolationHistory", back_populates="violation",
                              order_by="ViolationHistory.created_at, ViolationHistory.id")

    __table_args__ = (
        db.CheckConstraint(in_list("status", ViolationStatus.ALL), name="ck_violations_status"),
        db.CheckConstraint(in_list("image_status", ImageStatus.ALL), name="ck_violations_image_status"),
        db.Index("ix_violations_company_status_detected", "company_id", "status", "detected_at"),
        db.Index("ix_violations_lot_detected", "parking_lot_id", "detected_at"),
        # At most one ongoing episode per machine. This is what stops duplicates when the same photo
        # is retried or two uploads race, regardless of what the application code does.
        db.Index("uq_violations_active_per_machine", "machine_id", unique=True,
                 sqlite_where=db.text("ended_at IS NULL"), postgresql_where=db.text("ended_at IS NULL")),
    )


class ViolationHistory(db.Model):
    """Append-only timeline of what happened to a violation."""

    __tablename__ = "violation_history"

    id = db.Column(db.Integer, primary_key=True)
    violation_id = db.Column(db.Integer, db.ForeignKey("violations.id", ondelete="RESTRICT"),
                             nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)  # NULL = system
    previous_status = db.Column(db.String(20), nullable=True)
    new_status = db.Column(db.String(20), nullable=True)
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    violation = db.relationship("Violation", back_populates="history")
    admin = db.relationship("Admin")


class Notification(db.Model):
    """One notification to one admin over one channel for one violation."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    violation_id = db.Column(db.Integer, db.ForeignKey("violations.id", ondelete="RESTRICT"), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=NotificationStatus.PENDING)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    violation = db.relationship("Violation")
    admin = db.relationship("Admin")

    __table_args__ = (
        db.CheckConstraint(in_list("channel", Channel.ALL), name="ck_notifications_channel"),
        db.CheckConstraint(in_list("status", NotificationStatus.ALL), name="ck_notifications_status"),
        # A queue may deliver the same event twice. This makes notification creation idempotent.
        db.UniqueConstraint("violation_id", "admin_id", "channel", name="uq_notifications_target"),
        db.Index("ix_notifications_admin_status", "admin_id", "channel", "status"),
    )


class AuditLog(db.Model):
    """Who changed which administrative resource, with before/after values."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)
    company_id = db.Column(db.Integer, nullable=True, index=True)
    action = db.Column(db.String(60), nullable=False)
    resource_type = db.Column(db.String(40), nullable=False)
    resource_id = db.Column(db.String(40), nullable=True)
    before_value = db.Column(db.JSON, nullable=True)
    after_value = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    actor = db.relationship("Admin")
