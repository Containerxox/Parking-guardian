"""Turn the stream of photo verdicts from one machine into violation episodes.

A device photographs its parking space again and again (every minute in interval mode; on occupancy
changes and every few minutes while a car stays in event mode), so one parked vehicle produces many
photos. The rule is "one parked vehicle = one violation", like a guard who looks in regularly:

  1. Seeing a violation VIOLATION_CONFIRM_COUNT times in a row opens a violation (one notification).
     A single sighting is only remembered: it may be a wrong verdict or a car that stops briefly.
  2. While that vehicle stays, further sightings only update last_seen_at / detection_count.
     This holds even after an admin resolved it: the vehicle is still the same one.
  3. Not seeing a violation VEHICLE_CLEAR_COUNT times in a row ends the episode (vehicle left, or a
     sticker was seen in the same space). The next confirmed violation is a new one.
     A device in event mode (device/pi_client.py, PG_MODE=event) sends one photo of the empty space
     tagged event=left and then nothing until the next car. If the server sees no vehicle on it, the
     episode ends on that single photo: the device has already watched the space stay empty for a
     while, and waiting for more photos would leave the episode open for the next car.
  4. If the device is silent (no photo and no heartbeat) for longer than EPISODE_GAP_MINUTES, the open
     episode is closed as SIGNAL_LOST and counting starts over, because nobody knows what happened in
     between. Heartbeats count as a signal because an event-mode device sends photos only every few
     minutes while a car is parked.

All state lives on the machine row and the violation row, so the result is the same no matter which
API instance handles the next photo.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..adapters.inference import NO_VEHICLE, PERMITTED, VIOLATION
from ..extensions import db
from ..models import EndReason, HistoryAction, Machine, ParkingZone, Violation, as_utc, utcnow
from . import violations as violation_service

log = logging.getLogger(__name__)

# Values returned to the device in "result"
RESULT_PENDING = "violation_pending"    # seen, waiting for confirmation
RESULT_NEW = "violation"                # confirmed: a violation was just created
RESULT_ONGOING = "violation_ongoing"    # the already-recorded vehicle is still there

# Values a device may send in the "event" form field (event mode). interval mode sends none.
EVENT_ARRIVED = "arrived"   # occupancy confirmed, first photo of this car
EVENT_CONFIRM = "confirm"   # new photo taken ~60 s after a violation_pending answer
EVENT_REFRESH = "refresh"   # periodic photo while the car stays
EVENT_LEFT = "left"         # occupancy ended, photo of the empty space
EVENTS = (EVENT_ARRIVED, EVENT_CONFIRM, EVENT_REFRESH, EVENT_LEFT)


@dataclass
class DetectionOutcome:
    result: str
    violation: Violation | None = None
    created: bool = False       # True only when a new violation row was created by this photo
    episode_ended: bool = False


def _active_episode(machine_id: int) -> Violation | None:
    return db.session.scalar(select(Violation).where(Violation.machine_id == machine_id,
                                                     Violation.ended_at.is_(None)))


def _end_episode(violation: Violation, reason: str, ended_at: datetime, message: str) -> None:
    violation.ended_at = ended_at
    violation.end_reason = reason
    action = HistoryAction.SIGNAL_LOST if reason == EndReason.SIGNAL_LOST else HistoryAction.VEHICLE_LEFT
    violation_service.add_history(violation.id, action, message)


def _reset_counters(machine: Machine) -> None:
    machine.pending_violation_count = 0
    machine.pending_since = None
    machine.clear_count = 0


def _resolve_zone(machine: Machine, zone_name: str | None) -> ParkingZone | None:
    """Zone named by the device, or the single zone this machine watches."""
    if zone_name:
        zone = db.session.scalar(select(ParkingZone).where(
            ParkingZone.parking_lot_id == machine.parking_lot_id, ParkingZone.zone_name == zone_name.strip()))
        if zone:
            return zone
    zones = db.session.scalars(select(ParkingZone).where(ParkingZone.machine_id == machine.id)).all()
    return zones[0] if len(zones) == 1 else None


def _later(stored: datetime | None, value: datetime) -> datetime:
    """Signal times only move forward: a photo applied late or out of order must not pull them back."""
    stored = as_utc(stored)
    return value if stored is None or value > stored else stored


# record_verdict(previous_signal_at=...) not given: read the machine row (Phase 1). None is a real value there
# (the machine had no signal before this photo), so a separate marker is needed.
FROM_MACHINE_ROW = object()


def _create_episode_in_caller_transaction(machine: Machine, zone, first_seen: datetime, sightings: int,
                                          now: datetime) -> DetectionOutcome:
    """Rule 1 confirmation for record_verdict(commit=False). If another transaction created the machine's
    active episode first (unique index), only the savepoint is rolled back and the sighting counts as ongoing."""
    try:
        with db.session.begin_nested():
            violation = violation_service.create_violation(machine, zone, first_seen)
    except IntegrityError:
        log.info("episode already exists for machine %s, treating as ongoing", machine.id)
        active = _active_episode(machine.id)
        if active is not None:
            active.last_seen_at = _later(active.last_seen_at, now)
            active.detection_count = (active.detection_count or 1) + 1
        db.session.flush()
        return DetectionOutcome(RESULT_ONGOING, violation=active)
    violation.last_seen_at = now
    violation.detection_count = sightings
    _reset_counters(machine)
    db.session.flush()
    return DetectionOutcome(RESULT_NEW, violation=violation, created=True)


def record_verdict(machine_id: int, verdict: str, detected_at: datetime, zone_name: str | None = None,
                   event: str | None = None, observed_at: datetime | None = None,
                   previous_signal_at=FROM_MACHINE_ROW, commit: bool = True) -> DetectionOutcome:
    """Apply one photo's verdict to the machine's episode state and commit.

    `commit=False` (Phase 2 worker, decision 2026-11-02): only flush, inside the caller's transaction, so the
    verdict, the result row and the job's done status commit or roll back together. The race on the unique
    active-episode index then uses a savepoint instead of rolling back the caller's transaction. The default
    (commit=True) is the Phase 1 behaviour, unchanged.

    `event` is the device's reason for sending the photo (one of EVENTS) or None for an interval-mode
    device. Only EVENT_LEFT changes anything here (rule 3); the verdict always comes from the photo.

    `observed_at` is when the server received the photo (ADR 0019 decision 6, design 6.6). Every server-side
    time of the episode uses it: the silence check (rule 4), last_detection_at / last_heartbeat_at,
    last_seen_at and end times. A Phase 2 worker passes the job's received_at, so a photo that waited in the
    queue is judged as of its arrival. The Phase 1 synchronous endpoint passes nothing and gets the processing
    time, as before. `detected_at` (device clock) is still only used for display (pending_since, detected_at).

    `previous_signal_at` is the machine's last signal just before this photo, as snapshotted by the Phase 2
    ingest (detection_jobs.prev_signal_at; None = no earlier signal). Ingest refreshes the machine's signal at
    receipt, so the worker must not read it from the machine row. Not given (Phase 1): read the machine row.

    Image storage and notification are NOT done here: the caller does them only when
    outcome.created is True, so they happen once per episode.
    """
    config = current_app.config
    confirm_count = max(1, config["VIOLATION_CONFIRM_COUNT"])
    clear_count = max(1, config["VEHICLE_CLEAR_COUNT"])
    gap = timedelta(minutes=config["EPISODE_GAP_MINUTES"])
    now = as_utc(observed_at) if observed_at is not None else utcnow()
    finish = db.session.commit if commit else db.session.flush

    # Lock the machine row so two photos of the same machine are applied one after the other.
    # (PostgreSQL honours FOR UPDATE; SQLite serialises writers anyway.)
    # of=Machine is required on PostgreSQL: Machine eagerly joins its parking lot and company with
    # outer joins, and a plain FOR UPDATE is rejected on the nullable side of an outer join.
    machine = db.session.scalar(select(Machine).where(Machine.id == machine_id).with_for_update(of=Machine))
    active = _active_episode(machine.id)

    # Rule 4: a long silence invalidates whatever was being counted. The last signal is the later of
    # the last judged photo and the last heartbeat. A heartbeat that arrived while this photo waited in a
    # queue is later than `now`, so the difference is negative: no silence before this photo.
    if previous_signal_at is FROM_MACHINE_ROW:
        signals = [t for t in (as_utc(machine.last_detection_at), as_utc(machine.last_heartbeat_at)) if t is not None]
        previous = max(signals) if signals else None
    else:
        previous = as_utc(previous_signal_at)
    if previous is not None and now - previous > gap:
        if active is not None:
            last_seen = as_utc(active.last_seen_at) or previous
            minutes = int((now - previous).total_seconds() // 60)
            _end_episode(active, EndReason.SIGNAL_LOST, last_seen,
                         f"장비에서 {minutes}분 동안 사진도 Heartbeat도 들어오지 않아 사건을 종료했습니다.")
            active = None
        _reset_counters(machine)

    machine.last_detection_at = _later(machine.last_detection_at, now)
    machine.last_heartbeat_at = _later(machine.last_heartbeat_at, now)

    if verdict == VIOLATION:
        machine.clear_count = 0
        if active is not None:  # Rule 2
            active.last_seen_at = _later(active.last_seen_at, now)
            active.detection_count = (active.detection_count or 1) + 1
            finish()
            return DetectionOutcome(RESULT_ONGOING, violation=active)

        # Rule 1
        machine.pending_violation_count = (machine.pending_violation_count or 0) + 1
        if machine.pending_violation_count == 1:
            machine.pending_since = detected_at
        if machine.pending_violation_count < confirm_count:
            finish()
            return DetectionOutcome(RESULT_PENDING)

        first_seen = as_utc(machine.pending_since) or detected_at
        sightings = machine.pending_violation_count
        zone = _resolve_zone(machine, zone_name)
        if not commit:
            return _create_episode_in_caller_transaction(machine, zone, first_seen, sightings, now)
        try:
            violation = violation_service.create_violation(machine, zone, first_seen)
            violation.last_seen_at = now
            violation.detection_count = sightings
            _reset_counters(machine)
            db.session.commit()
            return DetectionOutcome(RESULT_NEW, violation=violation, created=True)
        except IntegrityError:
            # Another request created the episode a moment ago (unique index on the active episode).
            db.session.rollback()
            log.info("episode already exists for machine %s, treating as ongoing", machine_id)
            active = _active_episode(machine_id)
            if active is not None:
                active.last_seen_at = _later(active.last_seen_at, now)
                active.detection_count = (active.detection_count or 1) + 1
                db.session.commit()
            return DetectionOutcome(RESULT_ONGOING, violation=active)

    # NO_VEHICLE or PERMITTED: nothing to confirm any more
    machine.pending_violation_count = 0
    machine.pending_since = None
    ended = False
    if active is not None:  # Rule 3
        machine.clear_count = (machine.clear_count or 0) + 1
        # The empty-space photo of an event-mode device ends the episode by itself, but only when the
        # server agrees the space is empty. A sticker on a "left" photo means the device was wrong
        # about the space being empty, so the normal count applies.
        needed = 1 if (event == EVENT_LEFT and verdict == NO_VEHICLE) else clear_count
        if machine.clear_count >= needed:
            if verdict == PERMITTED:
                _end_episode(active, EndReason.STICKER_DETECTED, now,
                             "같은 자리에서 장애인 스티커가 탐지되어 사건을 종료했습니다. "
                             "처음 판정이 잘못되었을 수 있습니다.")
            else:
                _end_episode(active, EndReason.VEHICLE_LEFT, now, "차량이 주차 구역을 벗어난 것을 확인했습니다.")
            machine.clear_count = 0
            ended = True
    finish()
    return DetectionOutcome(verdict if verdict in (NO_VEHICLE, PERMITTED) else NO_VEHICLE,
                            violation=active, episode_ended=ended)
