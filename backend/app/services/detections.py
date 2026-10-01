"""Turn a stream of per-minute verdicts from one machine into violation episodes.

A device photographs its parking space every minute, so one parked vehicle produces many photos.
The rule is "one parked vehicle = one violation", like a guard who looks once a minute:

  1. Seeing a violation VIOLATION_CONFIRM_COUNT times in a row opens a violation (one notification).
     A single sighting is only remembered: it may be a wrong verdict or a car that stops briefly.
  2. While that vehicle stays, further sightings only update last_seen_at / detection_count.
     This holds even after an admin resolved it: the vehicle is still the same one.
  3. Not seeing a violation VEHICLE_CLEAR_COUNT times in a row ends the episode (vehicle left, or a
     sticker was seen in the same space). The next confirmed violation is a new one.
  4. If photos stop for longer than EPISODE_GAP_MINUTES, the open episode is closed as SIGNAL_LOST
     and counting starts over, because nobody knows what happened in between.

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


def record_verdict(machine_id: int, verdict: str, detected_at: datetime, zone_name: str | None = None) -> DetectionOutcome:
    """Apply one photo's verdict to the machine's episode state and commit.

    Image storage and notification are NOT done here: the caller does them only when
    outcome.created is True, so they happen once per episode.
    """
    config = current_app.config
    confirm_count = max(1, config["VIOLATION_CONFIRM_COUNT"])
    clear_count = max(1, config["VEHICLE_CLEAR_COUNT"])
    gap = timedelta(minutes=config["EPISODE_GAP_MINUTES"])
    now = utcnow()

    # Lock the machine row so two photos of the same machine are applied one after the other.
    # (PostgreSQL honours FOR UPDATE; SQLite serialises writers anyway.)
    machine = db.session.scalar(select(Machine).where(Machine.id == machine_id).with_for_update())
    active = _active_episode(machine.id)

    # Rule 4: a long silence invalidates whatever was being counted.
    previous = as_utc(machine.last_detection_at)
    if previous is not None and now - previous > gap:
        if active is not None:
            last_seen = as_utc(active.last_seen_at) or previous
            minutes = int((now - previous).total_seconds() // 60)
            _end_episode(active, EndReason.SIGNAL_LOST, last_seen,
                         f"장비에서 {minutes}분 동안 사진이 들어오지 않아 사건을 종료했습니다.")
            active = None
        _reset_counters(machine)

    machine.last_detection_at = now
    machine.last_heartbeat_at = now

    if verdict == VIOLATION:
        machine.clear_count = 0
        if active is not None:  # Rule 2
            active.last_seen_at = now
            active.detection_count = (active.detection_count or 1) + 1
            db.session.commit()
            return DetectionOutcome(RESULT_ONGOING, violation=active)

        # Rule 1
        machine.pending_violation_count = (machine.pending_violation_count or 0) + 1
        if machine.pending_violation_count == 1:
            machine.pending_since = detected_at
        if machine.pending_violation_count < confirm_count:
            db.session.commit()
            return DetectionOutcome(RESULT_PENDING)

        first_seen = as_utc(machine.pending_since) or detected_at
        sightings = machine.pending_violation_count
        zone = _resolve_zone(machine, zone_name)
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
                active.last_seen_at = now
                active.detection_count = (active.detection_count or 1) + 1
                db.session.commit()
            return DetectionOutcome(RESULT_ONGOING, violation=active)

    # NO_VEHICLE or PERMITTED: nothing to confirm any more
    machine.pending_violation_count = 0
    machine.pending_since = None
    ended = False
    if active is not None:  # Rule 3
        machine.clear_count = (machine.clear_count or 0) + 1
        if machine.clear_count >= clear_count:
            if verdict == PERMITTED:
                _end_episode(active, EndReason.STICKER_DETECTED, now,
                             "같은 자리에서 장애인 스티커가 탐지되어 사건을 종료했습니다. "
                             "처음 판정이 잘못되었을 수 있습니다.")
            else:
                _end_episode(active, EndReason.VEHICLE_LEFT, now, "차량이 주차 구역을 벗어난 것을 확인했습니다.")
            machine.clear_count = 0
            ended = True
    db.session.commit()
    return DetectionOutcome(verdict if verdict in (NO_VEHICLE, PERMITTED) else NO_VEHICLE,
                            violation=active, episode_ended=ended)
