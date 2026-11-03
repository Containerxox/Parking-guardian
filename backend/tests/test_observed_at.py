"""record_verdict(observed_at=...) (ADR 0019 decision 6, design 6.6).

In Phase 2 a photo is judged after a queue delay. Every server-side time of the episode must come from when
the server received the photo (observed_at), not from when the worker finished judging it; otherwise a healthy
device whose photo waited more than EPISODE_GAP_MINUTES in the queue would be closed as SIGNAL_LOST.

The tests call record_verdict directly with times far in the past, so "processing time" (now) is hours after
every observed_at: anything still using now() shows up as a wrong timestamp or a false SIGNAL_LOST.
Heartbeats are written to the machine row the way the heartbeat endpoint does.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import EndReason, Machine, Violation, as_utc, utcnow
from app.services import detections
from app.services.detections import RESULT_NEW, RESULT_ONGOING, RESULT_PENDING

GANGNAM = "RPI-A-GANGNAM-01"
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)   # well before "now"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def rules(app):
    app.config.update(VIOLATION_CONFIRM_COUNT=2, VEHICLE_CLEAR_COUNT=3, EPISODE_GAP_MINUTES=10)


@pytest.fixture
def machine_id(app):
    with app.app_context():
        machine = db.session.scalars(select(Machine).where(Machine.serial_number == GANGNAM)).one()
        machine.last_heartbeat_at = None
        machine.last_detection_at = None
        db.session.commit()
        return machine.id


@pytest.fixture
def apply(app, machine_id):
    """apply("violation", at(3), event=None) -> DetectionOutcome; the photo was received at at(3)."""

    def run(verdict, observed_at, event=None):
        with app.app_context():
            outcome = detections.record_verdict(machine_id, verdict, observed_at, event=event, observed_at=observed_at)
            return outcome.result, (outcome.violation.id if outcome.violation else None), outcome.episode_ended

    return run


def heartbeat(app, machine_id, when):
    with app.app_context():
        db.session.get(Machine, machine_id).last_heartbeat_at = when
        db.session.commit()


def machine_times(app, machine_id):
    with app.app_context():
        machine = db.session.get(Machine, machine_id)
        return as_utc(machine.last_detection_at), as_utc(machine.last_heartbeat_at)


def violation(app, violation_id):
    with app.app_context():
        row = db.session.get(Violation, violation_id)
        return {"detected_at": as_utc(row.detected_at), "last_seen_at": as_utc(row.last_seen_at),
                "ended_at": as_utc(row.ended_at), "end_reason": row.end_reason,
                "detection_count": row.detection_count}


# --- times come from observed_at ---------------------------------------------------------------------

def test_episode_times_are_the_receipt_times_not_the_processing_time(rules, app, machine_id, apply):
    assert apply("violation", at(0))[0] == RESULT_PENDING
    result, vid, _ = apply("violation", at(1))
    assert result == RESULT_NEW
    v = violation(app, vid)
    assert v["detected_at"] == at(0) and v["last_seen_at"] == at(1) and v["ended_at"] is None
    assert machine_times(app, machine_id) == (at(1), at(1))

    assert apply("violation", at(4), event="refresh")[0] == RESULT_ONGOING
    assert violation(app, vid)["last_seen_at"] == at(4)

    result, _, ended = apply("no_vehicle", at(7), event="left")
    assert ended is True
    v = violation(app, vid)
    assert v["ended_at"] == at(7) and v["end_reason"] == EndReason.VEHICLE_LEFT


def test_sticker_end_time_is_the_receipt_time(rules, app, apply):
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    for minute in (2, 3, 4):
        apply("permitted", at(minute))
    v = violation(app, vid)
    assert v["end_reason"] == EndReason.STICKER_DETECTED and v["ended_at"] == at(4)


def test_observed_at_in_another_time_zone_is_stored_as_the_same_instant(rules, app, machine_id):
    kst = timezone(timedelta(hours=9))
    with app.app_context():
        detections.record_verdict(machine_id, "violation", T0, observed_at=T0.astimezone(kst))
    assert machine_times(app, machine_id) == (T0, T0)


# --- late processing (queue delay) -----------------------------------------------------------------

def test_photo_judged_long_after_it_was_received_is_not_signal_lost(rules, app, apply):
    """Photos received 5 minutes apart but judged hours later: the silence check uses receipt times."""
    apply("violation", at(0))
    vid = apply("violation", at(5))[1]
    assert apply("violation", at(10), event="refresh")[0] == RESULT_ONGOING
    v = violation(app, vid)
    assert v["ended_at"] is None and v["last_seen_at"] == at(10) and v["detection_count"] == 3


def test_heartbeats_during_the_queue_delay_keep_the_episode_open(rules, app, machine_id, apply):
    """The device keeps sending heartbeats while its photo waits 15 minutes in the queue."""
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    for minute in range(2, 18):
        heartbeat(app, machine_id, at(minute))
    # refresh photo received at minute 12, judged after the heartbeats up to minute 17 were recorded
    assert apply("violation", at(12), event="refresh")[0] == RESULT_ONGOING
    v = violation(app, vid)
    assert v["ended_at"] is None and v["last_seen_at"] == at(12)
    # the heartbeat time stays at the latest signal; the photo does not pull it back to minute 12
    assert machine_times(app, machine_id) == (at(12), at(17))


def test_heartbeats_cover_a_gap_between_photos(rules, app, machine_id, apply):
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    for minute in range(2, 30):
        heartbeat(app, machine_id, at(minute))
    assert apply("violation", at(30), event="refresh")[0] == RESULT_ONGOING
    assert violation(app, vid)["ended_at"] is None


def test_real_silence_is_still_signal_lost_measured_in_receipt_time(rules, app, apply):
    """No photo and no heartbeat for 12 minutes of receipt time: the episode ends at its last sighting."""
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    result, _, _ = apply("violation", at(13), event="refresh")
    v = violation(app, vid)
    assert v["end_reason"] == EndReason.SIGNAL_LOST and v["ended_at"] == at(1)
    assert result == RESULT_PENDING   # counting starts over with this photo


def test_silence_exactly_at_the_limit_is_not_signal_lost(rules, app, apply):
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    assert apply("violation", at(11), event="refresh")[0] == RESULT_ONGOING
    assert violation(app, vid)["ended_at"] is None


# --- photos applied in reverse order --------------------------------------------------------------

def test_older_photo_applied_after_a_newer_one_does_not_move_times_backwards(rules, app, machine_id, apply):
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    apply("violation", at(8), event="refresh")
    assert apply("violation", at(5), event="refresh")[0] == RESULT_ONGOING   # received earlier, applied later
    v = violation(app, vid)
    assert v["last_seen_at"] == at(8) and v["ended_at"] is None and v["detection_count"] == 4
    assert machine_times(app, machine_id) == (at(8), at(8))


def test_older_photo_after_a_much_newer_one_is_not_signal_lost(rules, app, apply):
    """Receipt time earlier than the last signal: the silence before it is negative, not a gap."""
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    apply("violation", at(9), event="refresh")
    apply("violation", at(18), event="refresh")
    assert apply("violation", at(2), event="refresh")[0] == RESULT_ONGOING
    v = violation(app, vid)
    assert v["ended_at"] is None and v["last_seen_at"] == at(18)


# --- previous_signal_at: the signal snapshot taken at ingest (decision 2026-11-02) ---------------------

def _apply_with_snapshot(app, machine_id, verdict, observed, previous, event=None):
    with app.app_context():
        outcome = detections.record_verdict(machine_id, verdict, observed, event=event, observed_at=observed,
                                            previous_signal_at=previous)
        return outcome.result, (outcome.violation.id if outcome.violation else None)


def test_snapshot_detects_a_gap_even_though_ingest_already_refreshed_the_signal(rules, app, machine_id, apply):
    """Phase 2: ingest set last_heartbeat_at to this photo's receipt before the worker runs. Reading the machine
    row would show no gap; the snapshot taken at ingest still shows the 12 silent minutes."""
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    heartbeat(app, machine_id, at(13))   # what ingest of the next photo did
    result, _ = _apply_with_snapshot(app, machine_id, "violation", at(13), previous=at(1), event="refresh")
    v = violation(app, vid)
    assert v["end_reason"] == EndReason.SIGNAL_LOST and v["ended_at"] == at(1) and result == RESULT_PENDING


def test_snapshot_with_heartbeats_before_the_photo_keeps_the_episode(rules, app, machine_id, apply):
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    heartbeat(app, machine_id, at(20))
    result, _ = _apply_with_snapshot(app, machine_id, "violation", at(20), previous=at(19), event="refresh")
    assert result == RESULT_ONGOING and violation(app, vid)["ended_at"] is None


def test_snapshot_none_means_no_earlier_signal_at_all(rules, app, machine_id):
    heartbeat(app, machine_id, at(30))   # set by ingest of this very photo
    result, _ = _apply_with_snapshot(app, machine_id, "violation", at(30), previous=None)
    assert result == RESULT_PENDING


def test_snapshot_is_not_affected_by_signals_recorded_after_it(rules, app, machine_id, apply):
    """Photos queued back to back: ingest of photo 3 already moved the machine's signal past photo 2."""
    apply("violation", at(0))
    vid = apply("violation", at(1))[1]
    heartbeat(app, machine_id, at(25))
    result, _ = _apply_with_snapshot(app, machine_id, "violation", at(15), previous=at(1), event="refresh")
    assert violation(app, vid)["end_reason"] == EndReason.SIGNAL_LOST   # 14 minutes before photo 2


# --- Phase 1 synchronous path ------------------------------------------------------------------------

def test_without_observed_at_the_processing_time_is_used(rules, app, machine_id):
    """The Phase 1 endpoint passes no observed_at: behaviour is exactly as before (processing time)."""
    before = utcnow()
    with app.app_context():
        detections.record_verdict(machine_id, "violation", T0)
    last_detection, last_heartbeat = machine_times(app, machine_id)
    after = utcnow()
    assert before <= last_detection <= after and last_heartbeat == last_detection


def test_phase1_upload_still_creates_a_violation_with_current_times(rules, app, upload):
    before = utcnow()
    assert upload(GANGNAM).status_code == 200
    response = upload(GANGNAM)
    assert response.status_code == 201
    v = violation(app, response.get_json()["violation_id"])
    assert before <= v["last_seen_at"] <= utcnow()
