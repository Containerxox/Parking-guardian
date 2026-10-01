"""One parked vehicle = one violation, even though the device uploads a photo every minute."""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Machine, Violation, utcnow

from .conftest import API

GANGNAM = "RPI-A-GANGNAM-01"   # assigned: kim
PANGYO = "RPI-A-PANGYO-01"


@pytest.fixture
def real_rules(app):
    """Use the production defaults: 2 sightings to confirm, 3 clear photos to end."""
    app.config.update(VIOLATION_CONFIRM_COUNT=2, VEHICLE_CLEAR_COUNT=3, EPISODE_GAP_MINUTES=10)


def _violations(client, auth, email="kim@a.com", query=""):
    return client.get(f"{API}/violations{query}", headers=auth(email)).get_json()["items"]


def _unread(client, auth, email="kim@a.com"):
    return client.get(f"{API}/notifications?unread=true", headers=auth(email)).get_json()["unread_count"]


def test_one_sighting_is_not_enough(real_rules, client, auth, upload):
    first = upload(GANGNAM)
    assert first.status_code == 200 and first.get_json()["result"] == "violation_pending"
    assert _violations(client, auth) == [] and _unread(client, auth) == 0

    # a car that stopped briefly: the next photo is empty, so nothing is ever recorded
    assert upload(GANGNAM, "novehicle.jpg").get_json()["result"] == "no_vehicle"
    assert upload(GANGNAM).get_json()["result"] == "violation_pending"  # counting starts again from 1
    assert _violations(client, auth) == []


def test_two_hours_of_photos_make_one_violation_and_one_notification(real_rules, client, auth, upload):
    assert upload(GANGNAM, detected_at="2026-10-01 09:01:00").get_json()["result"] == "violation_pending"
    confirmed = upload(GANGNAM, detected_at="2026-10-01 09:02:00")
    assert confirmed.status_code == 201 and confirmed.get_json()["result"] == "violation"
    violation_id = confirmed.get_json()["violation_id"]

    for _ in range(118):
        again = upload(GANGNAM)
        assert again.status_code == 200
        assert again.get_json() == {**again.get_json(), "result": "violation_ongoing", "violation_id": violation_id}

    items = _violations(client, auth)
    assert [v["id"] for v in items] == [violation_id]
    assert items[0]["detection_count"] == 120
    assert items[0]["detected_at"] == "2026-10-01T00:01:00Z"  # the first sighting (09:01 KST), not the confirming one
    assert items[0]["vehicle_present"] is True and items[0]["has_image"] is True
    assert _unread(client, auth) == 1

    history = client.get(f"{API}/violations/{violation_id}", headers=auth("kim@a.com")).get_json()["history"]
    assert [h["action"] for h in history] == ["DETECTED", "NOTIFIED"]


def test_resolved_vehicle_that_stays_does_not_raise_a_new_violation(real_rules, client, auth, upload):
    upload(GANGNAM)
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    kim = auth("kim@a.com")
    assert client.post(f"{API}/violations/{violation_id}/resolve", headers=kim, json={}).status_code == 200

    for _ in range(5):  # the reported car is still parked
        assert upload(GANGNAM).get_json()["result"] == "violation_ongoing"
    assert len(_violations(client, auth)) == 1 and _unread(client, auth) == 1
    resolved = _violations(client, auth, query="?status=resolved")[0]
    assert resolved["status"] == "RESOLVED" and resolved["vehicle_present"] is True


def test_vehicle_leaving_ends_the_episode_and_the_next_car_is_new(real_rules, client, auth, upload):
    upload(GANGNAM)
    first_id = upload(GANGNAM).get_json()["violation_id"]

    # one empty photo (someone walked past the camera) does not end it
    assert "episode_ended" not in upload(GANGNAM, "novehicle.jpg").get_json()
    assert upload(GANGNAM).get_json()["result"] == "violation_ongoing"

    # three in a row do
    assert "episode_ended" not in upload(GANGNAM, "novehicle.jpg").get_json()
    assert "episode_ended" not in upload(GANGNAM, "novehicle.jpg").get_json()
    assert upload(GANGNAM, "novehicle.jpg").get_json()["episode_ended"] is True

    detail = client.get(f"{API}/violations/{first_id}", headers=auth("kim@a.com")).get_json()
    assert detail["vehicle_present"] is False and detail["end_reason"] == "VEHICLE_LEFT" and detail["ended_at"]
    assert detail["status"] == "NOTIFIED"  # still needs an admin: leaving does not resolve it
    assert detail["history"][-1]["action"] == "VEHICLE_LEFT"

    # the next car goes through confirmation again and becomes a second violation
    assert upload(GANGNAM).get_json()["result"] == "violation_pending"
    second = upload(GANGNAM)
    assert second.status_code == 201 and second.get_json()["violation_id"] != first_id
    assert len(_violations(client, auth)) == 2 and _unread(client, auth) == 2


def test_sticker_seen_in_the_same_space_ends_the_episode_with_its_own_reason(real_rules, client, auth, upload):
    upload(GANGNAM)
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    for _ in range(3):
        last = upload(GANGNAM, "permitted.jpg").get_json()
    assert last["result"] == "permitted" and last["episode_ended"] is True
    detail = client.get(f"{API}/violations/{violation_id}", headers=auth("kim@a.com")).get_json()
    assert detail["end_reason"] == "STICKER_DETECTED"
    assert "잘못" in detail["history"][-1]["message"]


def test_long_silence_closes_the_episode_and_restarts_counting(real_rules, app, client, auth, upload):
    upload(GANGNAM)
    first_id = upload(GANGNAM).get_json()["violation_id"]

    with app.app_context():  # the device was offline for 30 minutes
        machine = db.session.scalar(select(Machine).where(Machine.serial_number == GANGNAM))
        machine.last_detection_at = utcnow() - timedelta(minutes=30)
        db.session.commit()

    back = upload(GANGNAM)
    assert back.get_json()["result"] == "violation_pending"  # not "ongoing": nobody knows what happened meanwhile
    detail = client.get(f"{API}/violations/{first_id}", headers=auth("kim@a.com")).get_json()
    assert detail["end_reason"] == "SIGNAL_LOST" and detail["history"][-1]["action"] == "SIGNAL_LOST"
    assert upload(GANGNAM).status_code == 201


def test_machines_are_tracked_separately(real_rules, client, auth, upload):
    assert upload(GANGNAM).get_json()["result"] == "violation_pending"
    assert upload(PANGYO).get_json()["result"] == "violation_pending"  # not confirmed by another machine's photo
    assert upload(GANGNAM).status_code == 201
    assert upload(PANGYO).status_code == 201
    assert len(_violations(client, auth)) == 2


def test_database_allows_only_one_ongoing_episode_per_machine(app, upload):
    """The guarantee against duplicates when uploads race or are retried."""
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    with app.app_context():
        existing = db.session.get(Violation, violation_id)
        db.session.add(Violation(
            company_id=existing.company_id, parking_lot_id=existing.parking_lot_id, machine_id=existing.machine_id,
            detected_at=utcnow(), status="DETECTED",
        ))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # once the first episode has ended, a new one is allowed
        existing = db.session.get(Violation, violation_id)
        existing.ended_at = utcnow()
        db.session.add(Violation(
            company_id=existing.company_id, parking_lot_id=existing.parking_lot_id, machine_id=existing.machine_id,
            detected_at=utcnow(), status="DETECTED",
        ))
        db.session.commit()
