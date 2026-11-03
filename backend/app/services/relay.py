"""Phase 2 outbox relay (ADR 0019 decision 3; design 6.5, 6.7, 6.11, 6.12; decisions of 2026-11-02).

The database is the source of truth; the queue only carries jobs. The relay repairs everything the fast path
(ingest's immediate send, the worker) may leave behind:

  publish_pending     pending_publish jobs whose next_publish_at has come. Per machine, strictly in ingest order:
                      the machine stops at its first job that is not due, is locked by another relay, or fails to
                      send. Failure: publish_attempts + 1, next_publish_at = now + 1, 2, 4 ... 60 s; never gives up.
                      Several relays: rows are taken with FOR UPDATE SKIP LOCKED (PostgreSQL), and a machine is
                      skipped when an older pending job of it is held by another relay, so order survives.
                      A job sent but not marked queued is sent again later: SQS drops it within 5 minutes
                      (deduplication id = job id), after that the worker's claim finds the job finished.
  sweep_dead_letters  a DLQ message naming a job that is still open -> job dead_lettered (+ reason), commit, then
                      delete the message. Finished / already dead job -> just delete. A job a worker is judging
                      right now -> hold the message, look again later. Unreadable message or unknown job -> KEEP
                      it in the DLQ (hold it out of sight for an hour) and log identifiers only (decision
                      2026-11-02). Delete failures are harmless: the next sweep sees the job already dead.
  retry_effects       done jobs whose evidence copy / notification is still pending 5 minutes after their last
                      attempt -> the worker's effects step, which redoes only the missing parts (never creates or
                      changes a violation). One relay wins each job (conditional UPDATE).
  clean_up            design 6.12 with the 2026-11-02 delete rules: commands 30 d -> finished jobs 30 d (never
                      dead_lettered, in flight, or with effects pending; results keep their trace) -> sessions
                      30 d with no jobs/commands left and not a machine's current session -> results 90 d.
  recover_stalled     (2026-11-03) a job only moves when an SQS message reaches a worker. If that message is gone
                      (4-day retention, a DLQ message that expired before the sweep, a worker that died and left no
                      message behind) the job would stay open forever. Recovery puts such a job back to
                      pending_publish so the normal publish path sends it again. See recover_stalled().
"""
import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, delete, exists, func, or_, select, update

from .. import adapters
from ..adapters.queue import QueueError, QueueMessage
from ..extensions import db
from ..models import (DetectionJob, DetectionResult, DeviceCommand, DeviceSession, EffectsStatus, JobStatus, Machine,
                      as_utc, utcnow)
from .worker import JudgementWorker

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60
PENDING_ALERT_AGE = timedelta(minutes=5)          # design 6.7: alert when pending_publish is older than 5 min
EFFECTS_RETRY_AFTER = timedelta(minutes=5)        # design 6.7 job 3
DEDUPLICATION_WINDOW = timedelta(minutes=5)       # SQS FIFO; redrive waits it out
REDRIVE_MARGIN = timedelta(seconds=60)             # extra wait behind the window (see redrive)
STALLED_AFTER = timedelta(days=1)                 # recovery threshold (see recover_stalled); far below the 4-day queue
                                                  # retention and the 7-day inbox retention, far above any normal run
MIN_STALLED_AFTER = DEDUPLICATION_WINDOW + timedelta(minutes=5)   # a requeue inside the dedup window would be dropped
OPEN_STATUSES = (JobStatus.PENDING_PUBLISH, JobStatus.QUEUED, JobStatus.PROCESSING)
HOLD_UNUSABLE_SECONDS = 3600                      # unreadable / unknown-job DLQ messages
HOLD_BUSY_SECONDS = 60                            # DLQ message of a job a worker is judging right now
JOB_RETENTION = timedelta(days=30)
SESSION_RETENTION = timedelta(days=30)
COMMAND_RETENTION = timedelta(days=30)
RESULT_RETENTION = timedelta(days=90)
FINISHED = (JobStatus.DONE, JobStatus.STALE, JobStatus.REJECTED)


def backoff(attempts: int) -> timedelta:
    """Delay after the `attempts`-th failed send: 1, 2, 4 ... 60 s (design 6.7)."""
    return timedelta(seconds=min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempts - 1)))


def _mark_queued(job: DetectionJob, now: datetime) -> None:
    job.status = JobStatus.QUEUED
    job.published_at = now
    job.publish_attempts = (job.publish_attempts or 0) + 1
    job.last_error = None
    job.updated_at = now


def _record_send_failure(job: DetectionJob, error: QueueError, now: datetime) -> None:
    job.publish_attempts = (job.publish_attempts or 0) + 1
    job.next_publish_at = now + backoff(job.publish_attempts)
    job.last_error = f"{error.operation}: {error.code}"
    job.updated_at = now


class OutboxRelay:
    def __init__(self, clock=utcnow, batch_size: int = 50, stalled_after: timedelta | None = None,
                 stop: threading.Event | None = None):
        self.clock = clock
        self.batch_size = batch_size
        self._stop = stop      # set by SIGTERM/SIGINT (cli.install_stop_handlers); checked between steps, never in the middle of one
        if stalled_after is None:
            seconds = current_app.config.get("RELAY_STALLED_AFTER_SECONDS")
            stalled_after = timedelta(seconds=seconds) if seconds else STALLED_AFTER
        if stalled_after < MIN_STALLED_AFTER:
            raise ValueError(f"stalled_after must be at least {MIN_STALLED_AFTER} (SQS deduplication window "
                             f"{DEDUPLICATION_WINDOW} plus margin): a requeue inside it would be dropped")
        self.stalled_after = stalled_after

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    def run_loop(self, interval: float = 2.0, clean_every: int = 1800) -> int:
        """Passes until a stop is requested (SIGTERM/SIGINT). The sleep between passes ends the moment the stop is set. Returns the pass count.
        Every step of a pass commits on its own and is idempotent, so stopping between steps (or being killed anywhere) loses nothing:
        the next relay repeats what is left."""
        passes = 0
        while not self._stopping():
            self.run_once(clean=bool(clean_every) and passes % clean_every == 0)
            passes += 1
            if self._stop is not None:
                self._stop.wait(interval)
            else:
                time.sleep(interval)
        return passes

    def run_once(self, clean: bool = False) -> dict:
        stats = {"stopped": False, "recovery": {"requeued": 0, "rejected": 0, "storage_errors": 0}, "published": 0,
                 "dead_letters": {"dead_lettered": 0, "already_finished": 0, "kept": 0, "busy": 0}, "effects_retried": 0}
        # recovery first: a requeued job is published in this same pass. A stop is honoured between the steps.
        for key, step in (("recovery", self.recover_stalled), ("published", self.publish_pending),
                          ("dead_letters", self.sweep_dead_letters), ("effects_retried", self.retry_effects)):
            if self._stopping():
                stats["stopped"] = True
                return stats
            stats[key] = step()
        if self._stopping():
            stats["stopped"] = True
            return stats
        stats["oldest_pending_publish_seconds"] = self._report_old_pending()
        stats["stalled_jobs"], stats["oldest_open_job_age_seconds"] = self._report_open_jobs()
        if clean:
            stats["cleaned"] = self.clean_up()
        return stats

    # --- 1. publish -------------------------------------------------------------------------------------

    def publish_pending(self) -> int:
        now = self.clock()
        machine_ids = db.session.scalars(select(DetectionJob.machine_id).where(
            DetectionJob.status == JobStatus.PENDING_PUBLISH, DetectionJob.next_publish_at <= now)
            .group_by(DetectionJob.machine_id).order_by(func.min(DetectionJob.created_at))
            .limit(self.batch_size)).all()
        db.session.rollback()
        sent = 0
        for machine_id in machine_ids:
            if self._stopping():
                break
            sent += self._publish_machine(machine_id, now)
        return sent

    def _publish_machine(self, machine_id: int, now: datetime) -> int:
        in_order = (DetectionJob.created_at, DetectionJob.seq, DetectionJob.id)
        mine = db.session.scalars(select(DetectionJob).where(
            DetectionJob.machine_id == machine_id, DetectionJob.status == JobStatus.PENDING_PUBLISH)
            .order_by(*in_order).limit(self.batch_size).with_for_update(skip_locked=True)).all()
        if not mine:
            db.session.rollback()
            return 0
        # Every pending job of the machine, locked or not. Ours must be a prefix of it: a gap means another relay
        # holds an older job, and sending past it would break the order.
        everything = db.session.scalars(select(DetectionJob.id).where(
            DetectionJob.machine_id == machine_id, DetectionJob.status == JobStatus.PENDING_PUBLISH)
            .order_by(*in_order).limit(self.batch_size)).all()
        locked = {job.id: job for job in mine}
        queue = adapters.queue()
        sent = 0
        for job_id in everything:
            job = locked.get(job_id)
            if job is None or as_utc(job.next_publish_at) > now or self._stopping():
                break
            try:
                queue.send(job.id, job.machine_id)
            except QueueError as exc:
                log.warning("relay send failed (job=%s code=%s retryable=%s)", job.id, exc.code, exc.retryable)
                _record_send_failure(job, exc, now)
                break
            try:
                with db.session.begin_nested():
                    _mark_queued(job, now)
            except Exception:
                # The message is out but the row says pending: the next round resends it (deduplicated).
                log.exception("relay could not mark job %s queued", job.id)
                break
            sent += 1
        db.session.commit()
        return sent

    def _report_old_pending(self) -> float:
        oldest = db.session.scalar(select(func.min(DetectionJob.created_at)).where(
            DetectionJob.status == JobStatus.PENDING_PUBLISH))
        db.session.rollback()
        if oldest is None:
            return 0.0
        age = (self.clock() - as_utc(oldest)).total_seconds()
        if age > PENDING_ALERT_AGE.total_seconds():
            count = db.session.scalar(select(func.count()).select_from(DetectionJob).where(
                DetectionJob.status == JobStatus.PENDING_PUBLISH))
            db.session.rollback()
            log.warning("pending_publish older than %s min: %s job(s), oldest %.0f s",
                        int(PENDING_ALERT_AGE.total_seconds() // 60), count, age)
        return age

    # --- 1b. stalled jobs --------------------------------------------------------------------------------------

    def _stalled(self, cutoff: datetime):
        """Which jobs count as stalled, and the time each is judged by.

        queued       COALESCE(published_at, updated_at): the last SQS send we confirmed. SQS counts message
                     retention from the send and a receive does not reset it, so a queued job older than the
                     retention has no message. (A worker that keeps failing is not affected: its message goes to the
                     DLQ after three receives and the sweep makes the job dead_lettered long before the threshold.)
        processing   COALESCE(lease_until, updated_at): when the worker's lease ended. A live lease is in the
                     future and never matches; an expired one means no worker has touched the job since, although
                     SQS redelivers within a minute whenever its message exists.
        Not matched: pending_publish (this relay publishes it), dead_lettered (manual redrive), finished jobs.
        """
        return or_(
            and_(DetectionJob.status == JobStatus.QUEUED,
                 func.coalesce(DetectionJob.published_at, DetectionJob.updated_at) <= cutoff),
            and_(DetectionJob.status == JobStatus.PROCESSING,
                 func.coalesce(DetectionJob.lease_until, DetectionJob.updated_at) <= cutoff))

    def recover_stalled(self) -> dict:
        """Put stalled jobs back on the normal publish path. Returns {requeued, rejected, storage_errors}.

        Per job, in the machine's order (created_at, seq): check that the inbox photo still exists.
          present  -> status pending_publish, next_publish_at now, claim_attempt + 1, lease cleared. The bump
                      fences a worker that still holds the old claim (its _apply needs status processing AND the
                      attempt it claimed). The identity of the job (machine, session, seq, event, created_at) is
                      untouched, so the order guard (seq > last_applied_seq) judges it exactly as before; a job
                      recovered behind newer applied ones becomes stale, never a second episode change.
          missing  -> rejected / missing_image, the same end state a worker gives, without sending anything.
          error    -> left alone; the next pass tries again.
        Every change is one conditional UPDATE repeating the stalled test, so a job that a worker claimed (or
        another relay recovered) in the meantime does not match and is not touched: several relays can run at
        once and each job is recovered by exactly one of them.
        """
        stats = {"requeued": 0, "rejected": 0, "storage_errors": 0}
        now = self.clock()
        cutoff = now - self.stalled_after
        candidates = db.session.execute(select(DetectionJob.id, DetectionJob.s3_key, DetectionJob.status)
                                        .where(self._stalled(cutoff))
                                        .order_by(DetectionJob.created_at, DetectionJob.seq, DetectionJob.id)
                                        .limit(self.batch_size)).all()
        db.session.rollback()
        for job_id, s3_key, status in candidates:
            if self._stopping():
                break
            try:
                present = adapters.storage().exists(s3_key)
            except Exception as exc:
                log.warning("recovery cannot check the inbox photo of job %s: %s", job_id, type(exc).__name__)
                stats["storage_errors"] += 1
                continue
            guard = and_(DetectionJob.id == job_id, self._stalled(cutoff))
            if present:
                values = dict(status=JobStatus.PENDING_PUBLISH, next_publish_at=now, lease_until=None,
                              claim_attempt=DetectionJob.claim_attempt + 1, updated_at=now,
                              last_error=f"recovered: stalled in {status} for over {self.stalled_after}")
                key = "requeued"
            else:
                values = dict(status=JobStatus.REJECTED, lease_until=None, updated_at=now,
                              last_error=f"missing_image: inbox photo gone before recovery (stalled in {status})")
                key = "rejected"
            changed = db.session.execute(update(DetectionJob).where(guard).values(**values)
                                         .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if changed:
                stats[key] += 1
                log.warning("job %s recovered: %s (was %s)", job_id, key, status)
        return stats

    def _report_open_jobs(self) -> tuple[int, float]:
        """(jobs still stalled after this pass, age in seconds of the oldest open job) for operations. A stalled
        job that is still stalled means recovery could not act (storage errors) and needs a look."""
        now = self.clock()
        stalled = db.session.scalar(select(func.count()).select_from(DetectionJob)
                                    .where(self._stalled(now - self.stalled_after))) or 0
        oldest = db.session.scalar(select(func.min(DetectionJob.created_at))
                                   .where(DetectionJob.status.in_(OPEN_STATUSES)))
        db.session.rollback()
        age = 0.0 if oldest is None else (now - as_utc(oldest)).total_seconds()
        if stalled:
            log.warning("stalled jobs remain after recovery: %s job(s); oldest open job %.0f s", stalled, age)
        return stalled, age

    # --- 2. dead letters ------------------------------------------------------------------------------------

    def sweep_dead_letters(self) -> dict:
        stats = {"dead_lettered": 0, "already_finished": 0, "kept": 0, "busy": 0}
        queue = adapters.queue()
        try:
            messages = queue.receive_dead_letters()
        except (QueueError, ValueError) as exc:
            log.warning("cannot read the DLQ: %s", exc)
            return stats
        for message in messages:
            if self._stopping():
                break      # the rest become visible again after the DLQ visibility timeout and are swept by the next relay
            stats[self._handle_dead_letter(queue, message)] += 1
        return stats

    def _handle_dead_letter(self, queue, message: QueueMessage) -> str:
        if message.job_id is None:
            return self._keep(queue, message, "unreadable message body")
        job = db.session.get(DetectionJob, message.job_id)
        if job is None:
            db.session.rollback()
            return self._keep(queue, message, "no such job")
        now = self.clock()
        if job.status in FINISHED or job.status == JobStatus.DEAD_LETTERED:
            db.session.rollback()
            self._delete_dead_letter(queue, message)
            return "already_finished"
        if job.status == JobStatus.PROCESSING and job.lease_until is not None and as_utc(job.lease_until) > now:
            db.session.rollback()
            self._hold(queue, message, HOLD_BUSY_SECONDS)
            return "busy"
        reason = f"dlq: received {message.receive_count} time(s); last error: {job.last_error or '-'}"
        db.session.execute(update(DetectionJob).where(
            DetectionJob.id == job.id,
            DetectionJob.status.in_([JobStatus.PENDING_PUBLISH, JobStatus.QUEUED, JobStatus.PROCESSING]))
            .values(status=JobStatus.DEAD_LETTERED, last_error=reason[:500], lease_until=None, updated_at=now)
            .execution_options(synchronize_session=False))
        db.session.commit()   # the database records it first; only then the message goes
        self._delete_dead_letter(queue, message)
        log.warning("job %s dead-lettered (machine=%s): %s", job.id, job.machine_id, reason)
        return "dead_lettered"

    def _keep(self, queue, message: QueueMessage, reason: str) -> str:
        digest = hashlib.sha256(message.body.encode("utf-8", "replace")).hexdigest()[:16]
        log.warning("keeping unusable DLQ message: reason=%s message_id=%s group=%s receive_count=%s "
                    "body_bytes=%s body_sha256=%s", reason, message.message_id, message.group_id,
                    message.receive_count, len(message.body), digest)
        self._hold(queue, message, HOLD_UNUSABLE_SECONDS)
        return "kept"

    def _hold(self, queue, message: QueueMessage, seconds: int) -> None:
        try:
            queue.hold_dead_letter(message.receipt_handle, seconds)
        except QueueError as exc:
            log.info("could not hold DLQ message %s: %s", message.message_id, exc.code)

    def _delete_dead_letter(self, queue, message: QueueMessage) -> None:
        try:
            queue.delete_dead_letter(message.receipt_handle)
        except QueueError as exc:
            log.info("DLQ delete of %s failed (%s); the next sweep finds the job already handled",
                     message.message_id, exc.code)

    # --- 3. effects ---------------------------------------------------------------------------------------

    def retry_effects(self) -> int:
        now = self.clock()
        due = now - EFFECTS_RETRY_AFTER
        candidates = db.session.scalars(select(DetectionJob.id).where(
            DetectionJob.status == JobStatus.DONE, DetectionJob.effects_status == EffectsStatus.PENDING,
            DetectionJob.updated_at <= due).order_by(DetectionJob.updated_at).limit(self.batch_size)).all()
        db.session.rollback()
        worker = JudgementWorker(worker_id="relay", clock=self.clock)
        retried = 0
        for job_id in candidates:
            if self._stopping():
                break
            won = db.session.execute(update(DetectionJob).where(
                DetectionJob.id == job_id, DetectionJob.effects_status == EffectsStatus.PENDING,
                DetectionJob.updated_at <= due).values(updated_at=now)
                .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if not won:
                continue   # another relay (or a redelivered message) got it
            worker._run_effects(job_id)
            retried += 1
        return retried

    # --- 4. retention -----------------------------------------------------------------------------------

    def clean_up(self) -> dict:
        now = self.clock()
        stats = {}
        stats["commands"] = self._delete_ids(DeviceCommand, DeviceCommand.command_id, select(DeviceCommand.command_id)
                                             .where(DeviceCommand.created_at < now - COMMAND_RETENTION))
        stats["jobs"] = self._delete_ids(DetectionJob, DetectionJob.id, select(DetectionJob.id).where(
            DetectionJob.status.in_(FINISHED), DetectionJob.updated_at < now - JOB_RETENTION,
            or_(DetectionJob.effects_status.is_(None), DetectionJob.effects_status != EffectsStatus.PENDING)))
        current = select(Machine.current_session_id).where(Machine.current_session_id.is_not(None))
        stats["sessions"] = self._delete_ids(DeviceSession, DeviceSession.session_id, select(DeviceSession.session_id)
                                             .where(DeviceSession.first_received_at < now - SESSION_RETENTION,
                                                    ~exists().where(DetectionJob.session_id == DeviceSession.session_id),
                                                    ~exists().where(DeviceCommand.session_id == DeviceSession.session_id),
                                                    DeviceSession.session_id.not_in(current)))
        stats["results"] = self._delete_ids(DetectionResult, DetectionResult.id, select(DetectionResult.id).where(
            DetectionResult.created_at < now - RESULT_RETENTION))
        return stats

    def _delete_ids(self, model, key, id_query) -> int:
        ids = db.session.scalars(id_query.limit(1000)).all()
        if not ids:
            db.session.rollback()
            return 0
        deleted = db.session.execute(delete(model).where(key.in_(ids))
                                     .execution_options(synchronize_session=False)).rowcount
        db.session.commit()
        return deleted


def redrive(job_ids=None, all_dead: bool = False) -> int:
    """`flask jobs redrive`: dead_lettered -> pending_publish. The next publish is never inside SQS's 5-minute
    deduplication window of the job's last send, where a resend would be silently dropped and the job would wait
    as 'queued' with no message.

    next_publish_at = max(now, last_send + 5 min + REDRIVE_MARGIN). last_send is published_at (else created_at),
    which is a bookkeeping stamp, not the instant SQS received the message: the ingest stamps AFTER its send (safe),
    but the relay stamps the START of its pass (`now`), so the real send can be later by the sends before it in the
    batch or a slow call. Redriving at exactly stamp + 5 min was observed (Compose, ElasticMQ, 2026-11-03: stamp
    08:16:28.55, resend 5 min 0.4 s later) to land inside the window and vanish.
    The 60 s margin covers a stamp-to-send lag below 60 s. It does not cover a longer lag (a very slow relay pass)
    or a job whose published_at was never set and whose created_at is older than its send; the stalled-job recovery
    (1 day) is the net for those."""
    now = utcnow()
    query = select(DetectionJob).where(DetectionJob.status == JobStatus.DEAD_LETTERED)
    if not all_dead:
        query = query.where(DetectionJob.id.in_(job_ids or []))
    jobs = db.session.scalars(query.with_for_update()).all()
    for job in jobs:
        last_send = as_utc(job.published_at) or as_utc(job.created_at)
        job.status = JobStatus.PENDING_PUBLISH
        job.next_publish_at = max(now, last_send + DEDUPLICATION_WINDOW + REDRIVE_MARGIN)
        job.lease_until = None
        job.updated_at = now
    db.session.commit()
    return len(jobs)
