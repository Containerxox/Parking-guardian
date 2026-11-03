"""Phase 2 judgement worker (ADR 0019 decisions 5-8, design 6.5, 6.8, 6.11, 6.13; decisions of 2026-11-02).

For one queue message:

  1. Message without a readable job id, or naming no job -> isolate: never delete, make it visible again at once
     so SQS redrive (maxReceiveCount) moves it to the DLQ. Only ids and the reason are logged.
  2. Claim: conditional UPDATE (pending_publish | queued | processing with an expired lease) -> processing,
     claim_attempt + 1 (fencing token), lease. Nothing claimed: if the job is finished, finish leftover effects
     and delete the message; if another worker holds a valid lease, leave the message alone.
  3. Read the inbox photo. Missing -> rejected. Inference runs OUTSIDE any database transaction.
     InvalidImageError -> rejected. Any other failure -> release the lease, keep the message (redelivery,
     maxReceiveCount 3, then DLQ).
  4. One transaction: lock the machine row, lock the job row and re-check the claim (status processing and the
     same claim_attempt; otherwise another worker took over and this result is dropped), order guard, verdict
     (record_verdict(commit=False)), result row with the job's trace and violation links, job done/stale,
     confirm command. Commit, or roll back everything.
  5. Effects (only for a job that created a violation): evidence photo inbox -> violations/ key, notification.
     A failed part stays 'pending' (last_error names it); a redelivery or the relay (after 5 min) redoes only
     that part; 'failed' after 3 failed attempts. Idempotent, never touches the episode.
  6. Delete the message. A failed delete is only logged: the redelivered message finds the job finished.

Life cycle (P4/P5, 2026-11-03): serve() loads the models and runs both once BEFORE its first receive (a failed load raises and
consumes nothing). request_stop() (SIGTERM/SIGINT via cli.install_stop_handlers) means: no new work. The message being judged is
finished through step 6; a message that the long poll still returns after the stop is given back at once (visibility 0, it costs one
receive of maxReceiveCount 3) without a claim. If the grace period runs out first (SIGKILL), steps 2-4 make it safe: the lease (55 s)
ends, the message returns after the 60 s visibility timeout, another worker claims, claim_attempt fencing drops the dead one's result.

Order guard (design 6.5; the worker owns machines.current_session_id / last_applied_seq, decision 2026-11-02):
  - no current session                                   -> apply, switch to the job's session
  - job's session is current and seq > last_applied_seq  -> apply
  - job's session first seen by the server LATER than the current one (first_received_at, then session id)
                                                          -> apply, switch
  - otherwise (older session, or seq not higher)          -> stale: the verdict is stored, nothing else changes
Delivery order is not trusted; only first_received_at and seq decide.
"""
import logging
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, or_, select, update

from .. import adapters
from ..adapters.inference import InvalidImageError
from ..adapters.queue import QueueError, QueueMessage
from ..adapters.storage import build_image_key, sniff_image_type
from ..extensions import db
from ..models import (CommandStatus, CommandType, DetectionJob, DetectionResult, DeviceCommand, DeviceEvent,
                      DeviceSession, EffectsStatus, HistoryAction, ImageStatus, JobStatus, Machine, SessionStatus,
                      Verdict, Violation, as_utc, utcnow)
from . import detections
from . import violations as violation_service

log = logging.getLogger(__name__)

CONFIRM_DELAY = timedelta(seconds=60)        # design 6.9: due_at = received_at + 60 s
CONFIRM_VALIDITY = timedelta(minutes=9)      # design 6.10: expires_at = due_at + 9 min
EFFECTS_MAX_ATTEMPTS = 3                     # design 6.11 "repeated failure"; decision 2026-11-02
EFFECT_PARTS = ("image", "notify")
_EFFECTS_ERROR = re.compile(r"^effects: attempt (\d+) failed \(([a-z,]+)\)")


def parse_effects_error(last_error: str | None) -> tuple[int, tuple[str, ...]]:
    """(failed attempts so far, parts still to do) from job.last_error; a fresh job does every part."""
    match = _EFFECTS_ERROR.match(last_error or "")
    if not match:
        return 0, EFFECT_PARTS
    parts = tuple(p for p in match.group(2).split(",") if p in EFFECT_PARTS)
    return int(match.group(1)), parts or EFFECT_PARTS


@dataclass
class Applied:
    status: str              # done | stale
    created: bool


def _session_order(session: DeviceSession):
    return (as_utc(session.first_received_at), str(session.session_id))


def _guard(machine: Machine, job: DetectionJob) -> tuple[bool, bool]:
    """(apply, switch_session) for this job, from the machine's current session and last applied seq."""
    if machine.current_session_id is None:
        return True, True
    if job.session_id == machine.current_session_id:
        return job.seq > (machine.last_applied_seq or 0), False
    current = db.session.get(DeviceSession, machine.current_session_id)
    job_session = db.session.get(DeviceSession, job.session_id)
    if current is None or _session_order(job_session) > _session_order(current):
        return True, True
    return False, False


def _link_episode_results(result: DetectionResult, outcome, machine_id: int) -> None:
    """Option (나), decision 2026-11-02: a result names the episode its verdict was applied to. When this verdict
    confirmed a new episode, the sightings counted towards it are the immediately preceding applied results of the
    machine, all violation verdicts (any other applied verdict resets the count). Link exactly those, newest
    first, and stop at the first one that is not such a sighting. Session membership is not the criterion."""
    violation = outcome.violation
    if violation is None:
        return
    result.violation_id = violation.id
    if not outcome.created:
        return
    earlier = max(0, (violation.detection_count or 1) - 1)
    if not earlier:
        return
    previous = db.session.scalars(select(DetectionResult).where(
        DetectionResult.machine_id == machine_id, DetectionResult.applied.is_(True),
        DetectionResult.id != result.id).order_by(DetectionResult.received_at.desc(), DetectionResult.id.desc())
        .limit(earlier)).all()
    for row in previous:
        if row.verdict != Verdict.VIOLATION or row.violation_id is not None:
            break
        row.violation_id = violation.id


def _finish_job(job: DetectionJob, result: DetectionResult, applied: bool, outcome) -> None:
    """Last writes of the apply transaction (separate so tests can fail the transaction here)."""
    job.result_id = result.id
    job.status = JobStatus.DONE if applied else JobStatus.STALE
    job.lease_until = None
    job.last_error = None
    if applied and outcome is not None and outcome.created:
        job.violation_id = outcome.violation.id
        job.effects_status = EffectsStatus.PENDING


def _maybe_confirm_command(job: DetectionJob, outcome, now: datetime) -> None:
    """Design 6.8/6.10: a pending verdict asks the device for a confirm photo (as the Phase 1 device does after
    any violation_pending answer while occupied). Only for an active session: an ended (left) or superseded
    session's car is gone or the device has moved on. The active-command unique index absorbs a duplicate."""
    if outcome is None or outcome.result != detections.RESULT_PENDING or job.event == DeviceEvent.LEFT:
        return
    session = db.session.get(DeviceSession, job.session_id)
    if session is None or session.status != SessionStatus.ACTIVE:
        return
    due = as_utc(job.received_at) + CONFIRM_DELAY
    try:
        with db.session.begin_nested():
            db.session.add(DeviceCommand(machine_id=job.machine_id, session_id=job.session_id,
                                         type=CommandType.CAPTURE_CONFIRM, due_at=due, expires_at=due + CONFIRM_VALIDITY,
                                         status=CommandStatus.PENDING, source_job_id=job.id))
    except Exception as exc:   # IntegrityError: an active confirm already exists for this session
        log.info("confirm command not created for job %s (%s)", job.id, type(exc).__name__)


STOPPING = "stopping"   # run_once: the stop was requested before it looked at the queue
RETURNED = "returned"   # run_once: a message arrived after the stop and was given back untouched


class JudgementWorker:
    def __init__(self, worker_id: str | None = None, clock=utcnow, lease_seconds: int = 55, stop: threading.Event | None = None):
        # lease shorter than the 60 s visibility timeout: when the message reappears the lease has expired
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.clock = clock
        self.lease = timedelta(seconds=lease_seconds)
        self.stop = stop if stop is not None else threading.Event()

    # --- life cycle ------------------------------------------------------------------------------------

    def request_stop(self) -> None:
        """No new work from now on. Safe to call from a signal handler (it only sets an event)."""
        self.stop.set()

    @property
    def stopping(self) -> bool:
        return self.stop.is_set()

    def prepare(self) -> dict:
        """Load the models and run them once (YoloInference.preload) before anything is received. Adapters with nothing to load
        (mock, http, test doubles) have no preload and are simply ready. A failure raises: the caller must not start consuming."""
        preload = getattr(adapters.inference(), "preload", None)
        return preload() if callable(preload) else {}

    def serve(self, until_empty: bool = False, max_messages: int = 0) -> int:
        """prepare(), then receive and process messages until the queue is empty (until_empty), max_messages were handled, or a stop was
        requested. Returns how many messages were processed (a message given back after the stop does not count)."""
        handled = 0
        if self.stopping:
            return handled
        report = self.prepare()
        if report:
            log.info("judge-worker %s ready: %s", self.worker_id, " ".join(f"{k}={v}" for k, v in report.items()))
        while not self.stopping and (not max_messages or handled < max_messages):
            outcome = self.run_once()      # long poll (20 s on SQS) when idle
            if outcome is None:
                if until_empty:
                    break
                continue
            if outcome in (STOPPING, RETURNED):
                break
            handled += 1
        return handled

    def _give_back(self, message: QueueMessage) -> None:
        """A message that arrived after the stop: make it visible again now so another worker takes it without waiting 60 s."""
        try:
            adapters.queue().extend_visibility(message.receipt_handle, 0)
        except QueueError as exc:      # it then comes back after the visibility timeout; nothing was claimed, nothing is lost
            log.warning("could not give message %s back: %s", message.message_id, exc.code)

    # --- loop ------------------------------------------------------------------------------------------

    def run_once(self) -> str | None:
        """Receive and process at most one message. None when the queue had nothing."""
        if self.stopping:
            return STOPPING
        messages = adapters.queue().receive()
        if not messages:
            return None
        if self.stopping:
            self._give_back(messages[0])
            return RETURNED
        return self.process(messages[0])

    def process(self, message: QueueMessage) -> str:
        """Returns what happened: done | stale | already | busy | rejected | retry | isolated | lost_claim."""
        if message.job_id is None:
            return self._isolate(message, "unreadable message body")
        job = db.session.get(DetectionJob, message.job_id)
        if job is None:
            return self._isolate(message, "no such job")
        job_id = job.id
        db.session.rollback()   # end the read transaction before anything slow

        attempt = self._claim(job_id)
        if attempt is None:
            return self._not_claimed(job_id, message)

        try:
            image = adapters.storage().read(job.s3_key)
        except FileNotFoundError:
            self._reject(job_id, attempt, "missing_image")
            self._delete(message)
            return "rejected"
        except Exception as exc:
            self._release(job_id, attempt, f"storage: {type(exc).__name__}")
            return "retry"

        try:   # inference outside any transaction
            result = adapters.inference().analyze(image, f"{job.event_id}")
        except InvalidImageError:
            self._reject(job_id, attempt, "invalid_image")
            self._delete(message)
            return "rejected"
        except Exception as exc:
            log.warning("inference failed for job %s: %s", job_id, type(exc).__name__)
            self._release(job_id, attempt, f"inference: {type(exc).__name__}")
            return "retry"

        try:
            applied = self._apply(job_id, attempt, result)
        except Exception:
            db.session.rollback()
            log.exception("applying job %s failed; rolled back", job_id)
            self._release(job_id, attempt, "apply failed")
            return "retry"
        if applied is None:
            return "lost_claim"   # another worker owns the job now; it will delete the message

        if applied.created:
            try:
                self._run_effects(job_id)
            except Exception:
                db.session.rollback()
                log.exception("effects of job %s interrupted; they run again on redelivery", job_id)
                return "retry"
        self._delete(message)
        return applied.status

    # --- steps -----------------------------------------------------------------------------------------

    def _claim(self, job_id: uuid.UUID) -> int | None:
        now = self.clock()
        attempt = db.session.execute(
            update(DetectionJob).where(
                DetectionJob.id == job_id,
                or_(DetectionJob.status.in_([JobStatus.PENDING_PUBLISH, JobStatus.QUEUED]),
                    and_(DetectionJob.status == JobStatus.PROCESSING,
                         or_(DetectionJob.lease_until.is_(None), DetectionJob.lease_until < now))))
            .values(status=JobStatus.PROCESSING, claim_attempt=DetectionJob.claim_attempt + 1,
                    lease_until=now + self.lease, worker_id=self.worker_id, updated_at=now)
            .returning(DetectionJob.claim_attempt)
            .execution_options(synchronize_session=False)).scalar()
        db.session.commit()
        return attempt

    def _not_claimed(self, job_id: uuid.UUID, message: QueueMessage) -> str:
        db.session.expire_all()
        job = db.session.get(DetectionJob, job_id)
        if job.status in (JobStatus.DONE, JobStatus.STALE, JobStatus.REJECTED):
            if job.status == JobStatus.DONE and job.effects_status == EffectsStatus.PENDING:
                try:
                    self._run_effects(job_id)
                except Exception:
                    db.session.rollback()
                    log.exception("effects of job %s interrupted again", job_id)
                    return "retry"
            self._delete(message)
            return "already"
        db.session.rollback()
        log.info("job %s is %s (worker %s holds it); leaving the message", job_id, job.status, job.worker_id)
        return "busy"

    def _owned(self, job_id: uuid.UUID, attempt: int):
        return and_(DetectionJob.id == job_id, DetectionJob.status == JobStatus.PROCESSING,
                    DetectionJob.claim_attempt == attempt)

    def _reject(self, job_id: uuid.UUID, attempt: int, reason: str) -> None:
        db.session.execute(update(DetectionJob).where(self._owned(job_id, attempt)).values(
            status=JobStatus.REJECTED, last_error=reason, lease_until=None, updated_at=self.clock())
            .execution_options(synchronize_session=False))
        db.session.commit()

    def _release(self, job_id: uuid.UUID, attempt: int, reason: str) -> None:
        """Give the job back for redelivery: the lease ends now, the message stays in the queue."""
        try:
            now = self.clock()
            db.session.execute(update(DetectionJob).where(self._owned(job_id, attempt)).values(
                lease_until=now, last_error=reason, updated_at=now).execution_options(synchronize_session=False))
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("could not release job %s; its lease will expire", job_id)

    def _apply(self, job_id: uuid.UUID, attempt: int, inference_result) -> Applied | None:
        job_row = db.session.get(DetectionJob, job_id)
        machine = db.session.scalar(select(Machine).where(Machine.id == job_row.machine_id)
                                    .with_for_update(of=Machine))
        job = db.session.scalar(select(DetectionJob).where(DetectionJob.id == job_id)
                                .with_for_update().execution_options(populate_existing=True))
        if job.status != JobStatus.PROCESSING or job.claim_attempt != attempt:
            db.session.rollback()   # fencing: another worker re-claimed the job after our lease expired
            log.info("job %s was re-claimed (attempt %s is stale); dropping this result", job_id, attempt)
            return None

        apply, switch = _guard(machine, job)
        outcome = None
        if apply:
            if switch:
                machine.current_session_id = job.session_id
            machine.last_applied_seq = job.seq
            outcome = detections.record_verdict(
                machine.id, inference_result.verdict, as_utc(job.detected_at) or as_utc(job.received_at),
                event=job.event, observed_at=job.received_at, previous_signal_at=job.prev_signal_at, commit=False)

        result = DetectionResult(
            job_id=job.id, verdict=inference_result.verdict, window=inference_result.window,
            stickers=inference_result.stickers, elapsed_ms=inference_result.elapsed_ms,
            model_version=getattr(inference_result, "backend", None), applied=apply,
            machine_id=job.machine_id, event_id=job.event_id, session_id=job.session_id, seq=job.seq,
            event=job.event, received_at=job.received_at)
        db.session.add(result)
        db.session.flush()
        if apply:
            _link_episode_results(result, outcome, job.machine_id)
            _maybe_confirm_command(job, outcome, self.clock())
        _finish_job(job, result, apply, outcome)
        db.session.commit()
        return Applied(status=JobStatus.DONE if apply else JobStatus.STALE,
                       created=bool(apply and outcome is not None and outcome.created))

    def _run_effects(self, job_id: uuid.UUID) -> None:
        """Evidence photo + notification for a job that created a violation. Same steps and failure records as
        the Phase 1 endpoint (device.py 3-4). Never creates or changes the violation's episode state.

        Retry (design 6.7 job 3, 6.11; decision 2026-11-02): a failed part leaves effects_status 'pending' and
        job.last_error names the attempt and the parts still missing, e.g. "effects: attempt 1 failed (image)".
        The next run (redelivery, or the relay after 5 minutes) redoes only those parts. After
        EFFECTS_MAX_ATTEMPTS failed attempts: 'failed'. Repeating a part is safe (same evidence key,
        notifications deduplicated by violation/admin/channel)."""
        job = db.session.get(DetectionJob, job_id)
        if job is None or job.effects_status != EffectsStatus.PENDING:
            return
        previous_attempts, parts = parse_effects_error(job.last_error)
        violation_id = job.violation_id
        failed_parts = []

        violation = db.session.get(Violation, violation_id)
        if "image" in parts and violation.image_status != ImageStatus.UPLOADED:
            try:
                image = adapters.storage().read(job.s3_key)
                extension, _ = sniff_image_type(image)
                key = build_image_key(violation.company_id, violation.parking_lot_id, violation.id,
                                      as_utc(violation.detected_at), extension)
                adapters.storage().save(key, image)
                violation.image_key = key
                violation.image_status = ImageStatus.UPLOADED
                violation.image_expires_at = utcnow() + timedelta(days=current_app.config["IMAGE_RETENTION_DAYS"])
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception("evidence copy failed (violation=%s)", violation_id)
                failed_parts.append("image")
                violation = db.session.get(Violation, violation_id)
                violation.image_status = ImageStatus.FAILED
                violation_service.add_history(violation_id, HistoryAction.IMAGE_UPLOAD_FAILED,
                                              "차량 사진 저장에 실패했습니다. 사건 기록은 유지됩니다.")
                db.session.commit()

        if "notify" in parts:
            try:
                adapters.publisher().publish_violation_detected(violation_id)
            except Exception:
                db.session.rollback()
                log.exception("notification publish failed (violation=%s)", violation_id)
                failed_parts.append("notify")
                violation_service.add_history(violation_id, HistoryAction.NOTIFY_FAILED,
                                              "알림 전송에 실패했습니다. 사건 기록은 유지됩니다.")
                db.session.commit()

        attempt = previous_attempts + 1
        if not failed_parts:
            values = {"effects_status": EffectsStatus.DONE, "last_error": None}
        elif attempt >= EFFECTS_MAX_ATTEMPTS:
            values = {"effects_status": EffectsStatus.FAILED,
                      "last_error": f"effects: attempt {attempt} failed ({','.join(failed_parts)}); gave up"}
        else:
            values = {"effects_status": EffectsStatus.PENDING,
                      "last_error": f"effects: attempt {attempt} failed ({','.join(failed_parts)})"}
        db.session.execute(update(DetectionJob).where(
            DetectionJob.id == job_id, DetectionJob.effects_status == EffectsStatus.PENDING).values(
            updated_at=self.clock(), **values).execution_options(synchronize_session=False))
        db.session.commit()

    def _isolate(self, message: QueueMessage, reason: str) -> str:
        """Decision 2026-11-02: never judge or delete it; make it visible again now so the queue's redrive policy
        moves it to the DLQ after maxReceiveCount receipts. Only identifiers are logged, never the body."""
        log.warning("isolating unusable queue message: reason=%s message_id=%s group=%s receive_count=%s",
                    reason, message.message_id, message.group_id, message.receive_count)
        db.session.rollback()
        try:
            adapters.queue().extend_visibility(message.receipt_handle, 0)
        except QueueError as exc:
            log.warning("could not reset visibility of message %s: %s", message.message_id, exc.code)
        return "isolated"

    def _delete(self, message: QueueMessage) -> None:
        try:
            adapters.queue().delete(message.receipt_handle)
        except QueueError as exc:
            log.info("delete of message %s failed (%s); a redelivery will find the job finished",
                     message.message_id, exc.code)
