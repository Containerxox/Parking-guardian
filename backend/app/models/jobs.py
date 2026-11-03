"""Phase 2 asynchronous judgement tables (ADR 0019, design doc 6.4-6.13).

    device_sessions    one parked vehicle as the device sees it: arrived ... left
    detection_jobs     one photo to judge; the outbox row (the database is the source of truth, SQS only carries it)
    detection_results  the verdict of one job, kept even when the order guard says "stale"
    device_commands    a server -> device request carried by the heartbeat response (capture_confirm)

Only the schema lives here. Ingest, worker, relay and heartbeat commands are later steps.
"""
import uuid

from ..extensions import db
from .base import (CommandStatus, CommandType, DeviceEvent, EffectsStatus, JobStatus, SessionStatus, Verdict,
                   in_list, utcnow)


class DeviceSession(db.Model):
    """From a device's `arrived` to its `left`. The device creates session_id; the server orders sessions by
    first_received_at (server clock), never by the device clock."""

    __tablename__ = "device_sessions"

    session_id = db.Column(db.Uuid, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False)
    first_received_at = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=SessionStatus.ACTIVE)
    left_reported_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    # machines.current_session_id points back here, so the join column must be named.
    machine = db.relationship("Machine", foreign_keys=[machine_id])

    __table_args__ = (
        db.CheckConstraint(in_list("status", SessionStatus.ALL), name="ck_device_sessions_status"),
        db.Index("ix_device_sessions_machine_first_received", "machine_id", "first_received_at"),
    )


class DetectionJob(db.Model):
    """One photo waiting for, or done with, AI judgement. Columns and states: design 6.7."""

    __tablename__ = "detection_jobs"

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)   # job_id; also the SQS deduplication id
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False)
    event_id = db.Column(db.Uuid, nullable=False)                    # made by the device; same on every retry
    # The state path registers the session before the job is written, so a job never names an unknown session.
    session_id = db.Column(db.Uuid, db.ForeignKey("device_sessions.session_id", ondelete="RESTRICT"),
                           nullable=False)
    seq = db.Column(db.Integer, nullable=False)                      # order inside the session, from 1
    event = db.Column(db.String(20), nullable=False)
    detected_at = db.Column(db.DateTime(timezone=True), nullable=True)   # device clock: display/audit only
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)  # server clock at ingest
    # The machine's last signal (photo or heartbeat) just BEFORE this photo, read under the machine row lock at
    # ingest, before ingest refreshes it. The worker's silence check (SIGNAL_LOST) compares received_at with
    # this value; reading the machine row later would always see this photo's own receipt (decision 2026-11-02).
    prev_signal_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # The confirm command this photo fulfils: set only when the claim was valid at ingest (the command exists, belongs
    # to this machine and to the photo's session, and the photo is a confirm). NULL otherwise. With the unique index
    # below it makes "one command = one confirm job" a database guarantee (decision 2026-11-02, option A). No foreign
    # key on purpose: commands are deleted after 30 days independently of jobs (design 6.12), and this link must keep
    # protecting the command for as long as the job row exists.
    command_id = db.Column(db.Uuid, nullable=True)
    s3_key = db.Column(db.String(500), nullable=False)               # inbox/ key derived from event_id

    status = db.Column(db.String(20), nullable=False, default=JobStatus.PENDING_PUBLISH)
    publish_attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_publish_at = db.Column(db.DateTime(timezone=True), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)

    claim_attempt = db.Column(db.Integer, nullable=False, default=0, server_default="0")   # fencing token
    lease_until = db.Column(db.DateTime(timezone=True), nullable=True)
    worker_id = db.Column(db.String(100), nullable=True)

    # detection_results.job_id points back here, so this foreign key is added after both tables exist.
    # RESTRICT: a result cannot be deleted while its job exists (a done job never silently loses its verdict).
    # Results outlive jobs (90 d vs 30 d), so cleanup deletes the job first and is never blocked by this.
    result_id = db.Column(db.Integer, db.ForeignKey("detection_results.id", ondelete="RESTRICT", use_alter=True,
                                                    name="fk_detection_jobs_result_id"), nullable=True)
    violation_id = db.Column(db.Integer, db.ForeignKey("violations.id", ondelete="RESTRICT"), nullable=True)
    effects_status = db.Column(db.String(20), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    machine = db.relationship("Machine")
    session = db.relationship("DeviceSession")
    result = db.relationship("DetectionResult", foreign_keys=[result_id], post_update=True)
    violation = db.relationship("Violation")

    __table_args__ = (
        # Device retries and lost 202 responses: the same event becomes the same job (design 6.11, 6.13).
        db.UniqueConstraint("machine_id", "event_id", name="uq_detection_jobs_machine_event"),
        db.CheckConstraint(in_list("status", JobStatus.ALL), name="ck_detection_jobs_status"),
        db.CheckConstraint(in_list("event", DeviceEvent.ALL), name="ck_detection_jobs_event"),
        db.CheckConstraint("seq >= 1", name="ck_detection_jobs_seq"),
        db.CheckConstraint(in_list("effects_status", EffectsStatus.ALL), name="ck_detection_jobs_effects_status"),
        # Design 6.7: effects_status exists only when the job is done and created a violation.
        db.CheckConstraint(
            f"effects_status IS NULL OR (status = '{JobStatus.DONE}' AND violation_id IS NOT NULL)",
            name="ck_detection_jobs_effects_only_for_new_violation"),
        # One command = at most one confirm job per machine. NULL (every job without a valid command) never collides.
        db.Index("uq_detection_jobs_machine_command", "machine_id", "command_id", unique=True,
                 sqlite_where=db.text("command_id IS NOT NULL"), postgresql_where=db.text("command_id IS NOT NULL")),
        # Relay: pending_publish jobs whose next_publish_at has come (design 6.7, job 1).
        db.Index("ix_detection_jobs_status_next_publish", "status", "next_publish_at"),
        # Relay sends each machine's jobs in seq order; ingest checks for an older unpublished job (6.5).
        db.Index("ix_detection_jobs_machine_session_seq", "machine_id", "session_id", "seq"),
    )


class DetectionResult(db.Model):
    """The verdict of one job. `applied` = it changed the episode state (False when the job was stale).

    Retention (design 6.12, decision 2026-11-02): jobs are deleted after 30 days, results kept 90 days.
    When the job goes, job_id becomes NULL, so the job's trace is copied here when the result is written
    (same transaction as the verdict). The result stays findable by machine, event and violation.
    """

    __tablename__ = "detection_results"

    id = db.Column(db.Integer, primary_key=True)
    # Redelivery cannot add a second result while the job exists (design 6.13). NULL after the job is deleted;
    # from then on UNIQUE(machine_id, event_id) below is what keeps one result per event.
    job_id = db.Column(db.Uuid, db.ForeignKey("detection_jobs.id", ondelete="SET NULL",
                                              name="fk_detection_results_job_id"), nullable=True, unique=True)

    # Trace copied from the job. session_id has no foreign key: sessions are deleted after 30 days.
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False)
    event_id = db.Column(db.Uuid, nullable=False)
    session_id = db.Column(db.Uuid, nullable=False)
    seq = db.Column(db.Integer, nullable=False)
    event = db.Column(db.String(20), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)
    # The episode this verdict changed. Only an applied verdict can name one (a stale result touched nothing).
    violation_id = db.Column(db.Integer, db.ForeignKey("violations.id", ondelete="RESTRICT"), nullable=True)

    verdict = db.Column(db.String(20), nullable=False)
    window = db.Column(db.JSON, nullable=True)        # windshield box; NULL for no_vehicle
    stickers = db.Column(db.JSON, nullable=False, default=list)
    elapsed_ms = db.Column(db.Integer, nullable=False)
    model_version = db.Column(db.String(100), nullable=True)
    applied = db.Column(db.Boolean, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    # passive_deletes: let the database apply SET NULL instead of the ORM loading and updating results.
    job = db.relationship("DetectionJob", foreign_keys=[job_id], passive_deletes=True)
    machine = db.relationship("Machine")
    violation = db.relationship("Violation")

    __table_args__ = (
        db.CheckConstraint(in_list("verdict", Verdict.ALL), name="ck_detection_results_verdict"),
        db.CheckConstraint(in_list("event", DeviceEvent.ALL), name="ck_detection_results_event"),
        db.CheckConstraint("seq >= 1", name="ck_detection_results_seq"),
        db.CheckConstraint("violation_id IS NULL OR applied", name="ck_detection_results_violation_only_if_applied"),
        # One result per device event, also after job_id became NULL.
        db.UniqueConstraint("machine_id", "event_id", name="uq_detection_results_machine_event"),
        # Audit lookups: one machine over time, one violation's verdicts.
        db.Index("ix_detection_results_machine_received", "machine_id", "received_at"),
        db.Index("ix_detection_results_violation", "violation_id"),
    )


class DeviceCommand(db.Model):
    """A request to the device, delivered in heartbeat responses until fulfilled (design 6.10)."""

    __tablename__ = "device_commands"

    command_id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False)
    session_id = db.Column(db.Uuid, db.ForeignKey("device_sessions.session_id", ondelete="RESTRICT"),
                           nullable=False)
    type = db.Column(db.String(30), nullable=False)
    due_at = db.Column(db.DateTime(timezone=True), nullable=False)       # received_at of the photo + 60 s
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)   # due_at + 9 min
    status = db.Column(db.String(20), nullable=False, default=CommandStatus.PENDING)
    source_job_id = db.Column(db.Uuid, db.ForeignKey("detection_jobs.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    machine = db.relationship("Machine")
    session = db.relationship("DeviceSession")
    source_job = db.relationship("DetectionJob")

    __table_args__ = (
        db.CheckConstraint(in_list("type", CommandType.ALL), name="ck_device_commands_type"),
        db.CheckConstraint(in_list("status", CommandStatus.ALL), name="ck_device_commands_status"),
        # A worker redelivery must not create a second active confirm for the same session (design 6.10, 6.13).
        db.Index("uq_device_commands_active", "machine_id", "session_id", "type", unique=True,
                 sqlite_where=db.text(in_list("status", CommandStatus.ACTIVE)),
                 postgresql_where=db.text(in_list("status", CommandStatus.ACTIVE))),
        # Heartbeat: active commands of one machine.
        db.Index("ix_device_commands_machine_status", "machine_id", "status"),
    )
