"""Phase 2 photo ingest (ADR 0019; design 6.2 state path vs judgement path, 6.4-6.5, 6.7, 6.10, 6.11, 6.13).

One photo becomes one detection job. The AI verdict is NOT computed here; a worker does that later from the
queue. What happens here, in order:

  1. Duplicate check: the same (machine, event_id) already has a job -> return it (200), write nothing.
  2. Inbox photo: key = machine + event_id, written create-only; a retry finds its own bytes, a different
     photo under the same event_id is a 409 (decision 2026-11-02). Nothing is ever replaced.
  3. One transaction, holding the machine row lock (FOR UPDATE):
       - snapshot the machine's last signal into job.prev_signal_at, then refresh the signal (SIGNAL_LOST
         decision 2026-11-02; the worker compares received_at with the snapshot)
       - state path (design 6.2, 6.10): register a session seen for the first time (older active sessions ->
         superseded, their commands cancelled); `left` ends its session and cancels its commands; a confirm
         photo fulfils its command. The order guard (machines.current_session_id / last_applied_seq) is NOT
         touched: the worker owns it (decision 2026-11-02)
       - the job row, status pending_publish
  4. After commit, one immediate send to the queue, unless an older job of the same machine is still
     pending_publish (design 6.5: keep per-machine order). Success -> queued. Failure -> stays
     pending_publish with a backoff; the outbox relay (step 6) sends it later. Either way the caller answers 202.

Failures before the commit raise IngestError (503 to the device, which retries with the same event_id).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .. import adapters
from ..adapters.queue import QueueError
from ..extensions import db
from ..models import (CommandStatus, CommandType, DetectionJob, DeviceCommand, DeviceEvent, DeviceSession, JobStatus,
                      Machine, SessionStatus, as_utc, utcnow)

log = logging.getLogger(__name__)

FIRST_PUBLISH_BACKOFF = timedelta(seconds=1)   # design 6.7: 1, 2, 4 ... 60 s; the relay continues the series


class IngestError(Exception):
    """Something the device should retry (503) or fix (409)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


COMMAND_ALREADY_CONFIRMED = "command_already_confirmed"


@dataclass
class IngestResult:
    job_id: uuid.UUID
    event_id: uuid.UUID
    status: str
    created: bool   # False = duplicate of an earlier request
    # Set only when the duplicate is of the COMMAND (a different event_id for a command that already has its
    # confirm job): the answer is that job, nothing new was stored.
    reason: str | None = None
    command_id: uuid.UUID | None = None
    existing_event_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PhotoEvent:
    machine_id: int
    event: str
    event_id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    detected_at: datetime | None    # device clock, display/audit only
    command_id: uuid.UUID | None


def inbox_key(machine_id: int, event_id: uuid.UUID) -> str:
    """inbox/m-<machine>/<event_id> (decision 2026-11-02, replaces the dated key of design 6.11). No date and
    no extension: every retry of one event names the same object whatever the clocks, the day or the file type.
    The content type travels with the object; the 7-day lifecycle uses object age, not the key."""
    return f"inbox/m-{machine_id}/{event_id}"


def _store_inbox_photo(key: str, image_bytes: bytes) -> None:
    """Create the inbox object, or accept the one already there only if it holds exactly these bytes (a retry
    after a failed commit). A different photo under the same event id is refused; nothing is replaced."""
    storage = adapters.storage()
    try:
        if storage.save_new(key, image_bytes):
            return
        if storage.read(key) == image_bytes:
            return
    except Exception:
        log.exception("inbox save failed (key=%s)", key)
        raise IngestError(503, "storage_unavailable", "사진을 저장하지 못했습니다. 잠시 후 다시 보내 주세요.")
    raise IngestError(409, "event_id_conflict", "같은 event_id 로 다른 사진이 이미 저장되어 있습니다.")


def _later(stored: datetime | None, value: datetime) -> datetime:
    stored = as_utc(stored)
    return value if stored is None or value > stored else stored


def _find_job(machine_id: int, event_id: uuid.UUID) -> DetectionJob | None:
    return db.session.scalar(select(DetectionJob).where(DetectionJob.machine_id == machine_id,
                                                        DetectionJob.event_id == event_id))


@dataclass(frozen=True)
class CommandClaim:
    """What the photo's command_id amounts to.

    absent   no command_id: an ordinary photo, nothing to check.
    valid    a capture_confirm command of THIS machine and of the photo's session, on a `confirm` photo.
             `link` is stored on the job; `owner` is the job that already holds the command (a duplicate), if any.
    invalid  anything else. The request is refused with 409 command_conflict before anything is written."""

    kind: str
    link: uuid.UUID | None = None
    owner: DetectionJob | None = None
    reason: str = ""


def _classify_command(photo: PhotoEvent) -> CommandClaim:
    """Decision 2026-11-02: an explicit command_id must name a valid confirm command of this machine and session;
    otherwise it is a device bug and the request is refused (409, not retryable), not silently ignored.

    Validity ignores the command's STATUS (cancelled and expired commands are protected too). An existing job with
    this command_id proves the claim was valid when that job was taken, so the protection does not depend on the
    command row still existing (commands are deleted 30 days after creation, jobs 30 days after they finished)."""
    if photo.command_id is None:
        return CommandClaim("absent")
    if photo.event != DeviceEvent.CONFIRM:
        log.warning("%s photo names a command; only a confirm photo may (machine=%s command=%s)",
                    photo.event, photo.machine_id, photo.command_id)
        return CommandClaim("invalid", reason="command_id 는 confirm 사진에만 붙일 수 있습니다.")
    owner = db.session.scalar(select(DetectionJob).where(DetectionJob.machine_id == photo.machine_id,
                                                         DetectionJob.command_id == photo.command_id))
    if owner is not None:
        if owner.session_id == photo.session_id:
            return CommandClaim("valid", photo.command_id, owner)
        log.warning("command %s is held by a job of another session (machine=%s)", photo.command_id, photo.machine_id)
        return CommandClaim("invalid", reason="이 command_id 는 다른 세션의 confirm 사진이 이미 사용했습니다.")
    command = db.session.get(DeviceCommand, photo.command_id)
    if (command is None or command.machine_id != photo.machine_id or command.session_id != photo.session_id
            or command.type != CommandType.CAPTURE_CONFIRM):
        log.warning("confirm photo names a command that is not a confirm command of this machine/session "
                    "(machine=%s command=%s)", photo.machine_id, photo.command_id)
        return CommandClaim("invalid", reason="command_id 가 이 장비·이 세션의 confirm 명령이 아닙니다.")
    return CommandClaim("valid", photo.command_id)


def _refuse(claim: CommandClaim) -> IngestError:
    return IngestError(409, "command_conflict", claim.reason)


def _command_duplicate(owner: DetectionJob, photo: PhotoEvent) -> IngestResult:
    """The command already has its confirm job: answer with it, store nothing. 200 means 'the server already holds a
    photo for this command, sending again changes nothing'; `status` says what became of that photo."""
    log.info("confirm for command %s is a duplicate of job %s (%s); not stored (machine=%s)",
             photo.command_id, owner.id, owner.status, photo.machine_id)
    return IngestResult(owner.id, photo.event_id, owner.status, created=False, reason=COMMAND_ALREADY_CONFIRMED,
                        command_id=photo.command_id, existing_event_id=owner.event_id)


def _as_duplicate(job: DetectionJob, photo: PhotoEvent, image_bytes: bytes) -> IngestResult:
    if (job.session_id, job.seq, job.event) != (photo.session_id, photo.seq, photo.event):
        raise IngestError(409, "event_id_conflict",
                          "같은 event_id 로 다른 사진 정보(session_id, seq, event)가 이미 접수되었습니다.")
    claim = _classify_command(photo)
    if claim.kind == "invalid":
        raise _refuse(claim)
    if claim.link != job.command_id:
        raise IngestError(409, "event_id_conflict", "같은 event_id 로 다른 command_id 가 이미 접수되었습니다.")
    try:
        stored = adapters.storage().read(job.s3_key)
    except Exception:   # expired by the inbox lifecycle, or storage down: the job exists, so it is a duplicate
        log.info("duplicate of job %s accepted without comparing bytes (inbox photo unavailable)", job.id)
        stored = image_bytes
    if stored != image_bytes:
        raise IngestError(409, "event_id_conflict", "같은 event_id 로 다른 사진이 이미 접수되었습니다.")
    return IngestResult(job.id, job.event_id, job.status, created=False)


def _commit_ingest() -> None:
    """Separate function so tests can make the commit fail."""
    db.session.commit()


def _cancel_active_commands(machine_id: int, session_ids) -> None:
    db.session.execute(update(DeviceCommand).where(
        DeviceCommand.machine_id == machine_id, DeviceCommand.session_id.in_(session_ids),
        DeviceCommand.status.in_(CommandStatus.ACTIVE)).values(status=CommandStatus.CANCELLED, updated_at=utcnow()))


def _apply_state_path(machine: Machine, photo: PhotoEvent, received_at: datetime) -> None:
    """Design 6.2 / 6.10: what the device reports takes effect now, independent of the AI backlog."""
    session = db.session.get(DeviceSession, photo.session_id)
    if session is not None and session.machine_id != machine.id:
        raise IngestError(409, "session_conflict", "다른 장비의 session_id 입니다.")

    if session is None:
        # First time the server sees this session (any event may be the first: the device can give up on
        # `arrived`). Sessions are ordered by when the server first saw them (design 6.4).
        older = db.session.scalars(select(DeviceSession.session_id).where(
            DeviceSession.machine_id == machine.id, DeviceSession.status == SessionStatus.ACTIVE)).all()
        if older:
            db.session.execute(update(DeviceSession).where(DeviceSession.session_id.in_(older))
                               .values(status=SessionStatus.SUPERSEDED, updated_at=utcnow()))
            _cancel_active_commands(machine.id, older)
        session = DeviceSession(session_id=photo.session_id, machine_id=machine.id, first_received_at=received_at)
        db.session.add(session)
        db.session.flush()
        # machines.current_session_id / last_applied_seq are NOT touched here (decision 2026-11-02): the worker
        # moves them as it applies jobs. Switching here, at receipt, would make every still-queued job of the
        # previous session stale and lose its verdicts.

    if photo.event == DeviceEvent.LEFT:
        if session.left_reported_at is None:
            session.left_reported_at = received_at
        if session.status == SessionStatus.ACTIVE:
            session.status = SessionStatus.ENDED
        _cancel_active_commands(machine.id, [session.session_id])

    if photo.event == DeviceEvent.CONFIRM and photo.command_id is not None:
        fulfilled = db.session.execute(update(DeviceCommand).where(
            DeviceCommand.command_id == photo.command_id, DeviceCommand.machine_id == machine.id,
            DeviceCommand.session_id == session.session_id, DeviceCommand.status.in_(CommandStatus.ACTIVE))
            .values(status=CommandStatus.FULFILLED, updated_at=utcnow())).rowcount
        if not fulfilled:
            log.info("confirm photo names a command that is not active for this machine/session "
                     "(machine=%s command=%s)", machine.id, photo.command_id)


def ingest_photo(photo: PhotoEvent, image_bytes: bytes, received_at: datetime) -> IngestResult:
    # 1. duplicate (cheap check before touching storage; the unique constraint is the real guard)
    existing = _find_job(photo.machine_id, photo.event_id)
    if existing is not None:
        return _as_duplicate(existing, photo, image_bytes)

    # 1b. the command_id: refuse an invalid one (409) or, if the command already has its confirm job, answer with that
    # job (200). Both before anything is stored.
    claim = _classify_command(photo)
    if claim.kind == "invalid":
        raise _refuse(claim)
    if claim.owner is not None:
        return _command_duplicate(claim.owner, photo)

    # 2. inbox photo (create only; a retry finds its own bytes there)
    key = inbox_key(photo.machine_id, photo.event_id)
    _store_inbox_photo(key, image_bytes)

    # 3. state path + job row, one transaction under the machine row lock
    try:
        machine = db.session.scalar(select(Machine).where(Machine.id == photo.machine_id)
                                    .with_for_update(of=Machine))
        # Authoritative re-check under the lock: a concurrent confirm for the same command may have committed
        # between 1b and here. (The unique index below is the last defence.) The inbox object written above stays
        # behind and is removed by the 7-day lifecycle, like after a failed commit.
        claim = _classify_command(photo)
        if claim.kind == "invalid":
            raise _refuse(claim)                 # the IngestError handler below rolls back and re-raises
        if claim.owner is not None:
            result = _command_duplicate(claim.owner, photo)
            db.session.rollback()
            return result
        signals = [t for t in (as_utc(machine.last_detection_at), as_utc(machine.last_heartbeat_at)) if t]
        previous_signal = max(signals) if signals else None
        _apply_state_path(machine, photo, received_at)
        job = DetectionJob(machine_id=machine.id, event_id=photo.event_id, session_id=photo.session_id,
                           seq=photo.seq, event=photo.event, detected_at=photo.detected_at,
                           received_at=received_at, s3_key=key, status=JobStatus.PENDING_PUBLISH,
                           next_publish_at=received_at, prev_signal_at=previous_signal, command_id=claim.link)
        db.session.add(job)
        machine.last_detection_at = _later(machine.last_detection_at, received_at)
        machine.last_heartbeat_at = _later(machine.last_heartbeat_at, received_at)
        _commit_ingest()
        job_id = job.id
    except IngestError:
        db.session.rollback()
        raise
    except IntegrityError:
        # Lost a race (design 6.13): the other request's job is the answer. Same event first, then same command.
        db.session.rollback()
        existing = _find_job(photo.machine_id, photo.event_id)
        if existing is not None:
            return _as_duplicate(existing, photo, image_bytes)
        claim = _classify_command(photo)
        if claim.kind == "valid" and claim.owner is not None:
            return _command_duplicate(claim.owner, photo)
        log.exception("ingest integrity error (machine=%s event=%s)", photo.machine_id, photo.event_id)
        raise IngestError(503, "database_unavailable", "접수를 기록하지 못했습니다. 잠시 후 다시 보내 주세요.")
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("ingest transaction failed (machine=%s event=%s)", photo.machine_id, photo.event_id)
        raise IngestError(503, "database_unavailable", "접수를 기록하지 못했습니다. 잠시 후 다시 보내 주세요.")

    # 4. immediate send (an optimisation; the relay is the guarantee)
    status = _publish_now(job_id, photo.machine_id)
    return IngestResult(job_id, photo.event_id, status, created=True)


def _older_unpublished(job_id: uuid.UUID, machine_id: int) -> bool:
    return db.session.scalar(select(exists().where(
        DetectionJob.machine_id == machine_id, DetectionJob.id != job_id,
        DetectionJob.status == JobStatus.PENDING_PUBLISH)))


def _lock_unless_taken(job_id: uuid.UUID) -> bool:
    """Lock the job row if it is still pending_publish and nobody holds it. False = the relay (or a worker) has it
    or has moved it on.

    The ingest's bookkeeping after its immediate send is an optimisation, the relay is the guarantee (design 6.7).
    The relay locks a pending_publish job FOR UPDATE for the whole time it sends with the normal, slower client;
    waiting for that lock held the request ~37 s in a queue outage (9단계 follow-up, pg_blocking_pids confirmed).
    A locked row is the relay's: it records its own outcome, and either way the job stays pending_publish until then.
    SKIP LOCKED is ignored by SQLite (single writer), where there is nothing to wait for."""
    return db.session.scalar(select(DetectionJob.id).where(
        DetectionJob.id == job_id, DetectionJob.status == JobStatus.PENDING_PUBLISH)
        .with_for_update(skip_locked=True)) is not None


def _mark_queued(job_id: uuid.UUID, now: datetime) -> None:
    # Conditional: a worker may already have claimed the job (claim accepts pending_publish, design 6.8), and a row
    # the relay holds is left to the relay (it marks the job queued after its own send; the message is deduplicated).
    if not _lock_unless_taken(job_id):
        db.session.rollback()
        return
    db.session.execute(update(DetectionJob).where(
        DetectionJob.id == job_id, DetectionJob.status == JobStatus.PENDING_PUBLISH).values(
        status=JobStatus.QUEUED, published_at=now, publish_attempts=DetectionJob.publish_attempts + 1,
        last_error=None, updated_at=now))
    db.session.commit()


def _record_send_failure(job_id: uuid.UUID, error: QueueError, now: datetime) -> None:
    if not _lock_unless_taken(job_id):
        db.session.rollback()   # the relay holds the row (it records its own result) or the job moved on
        return
    db.session.execute(update(DetectionJob).where(
        DetectionJob.id == job_id, DetectionJob.status == JobStatus.PENDING_PUBLISH).values(
        publish_attempts=DetectionJob.publish_attempts + 1, next_publish_at=now + FIRST_PUBLISH_BACKOFF,
        last_error=f"{error.operation}: {error.code}", updated_at=now))
    db.session.commit()


def _publish_now(job_id: uuid.UUID, machine_id: int) -> str:
    queue = adapters.queue()
    try:
        if _older_unpublished(job_id, machine_id):
            return JobStatus.PENDING_PUBLISH   # the relay sends this machine's jobs in order
        try:
            queue.send(job_id, machine_id, fast=True)   # intake only: fail fast, the relay retries
        except QueueError as exc:
            log.warning("queue send failed, relay will retry (job=%s code=%s retryable=%s)",
                        job_id, exc.code, exc.retryable)
            _record_send_failure(job_id, exc, utcnow())
            return JobStatus.PENDING_PUBLISH
        _mark_queued(job_id, utcnow())
        return JobStatus.QUEUED
    except SQLAlchemyError:
        # The job is committed; only its bookkeeping failed. It stays pending_publish (and may already be in
        # the queue: the relay's resend is deduplicated by job id, design 6.11).
        db.session.rollback()
        log.exception("updating publish state failed (job=%s)", job_id)
        return JobStatus.PENDING_PUBLISH
