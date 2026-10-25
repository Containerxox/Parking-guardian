"""The event-mode device state machine (device/pi_client.EventModeRunner) driven against the real API.

Occupancy is SCRIPTED, not detected: a timeline says when the space is empty, holds a violating car
or holds a permitted car, and the mock inference judges photos by file name. So this verifies the
device protocol and the server's episode handling end to end (arrived -> confirm -> refresh -> left,
heartbeats, one violation, one notification). It says nothing about YOLO/ROI detection accuracy.
"""
import io
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import dev_api_key
from app.extensions import db
from app.models import Machine, Violation

from .conftest import API, JPEG_BYTES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "device"))
from pi_client import EventModeRunner, EventSettings  # noqa: E402
from vehicle_detector import OccupancyConfig, OccupancyTracker  # noqa: E402

GANGNAM = "RPI-A-GANGNAM-01"
SETTINGS = EventSettings(detect_interval=5, heartbeat_interval=60, refresh_interval=600, confirm_delay=60)

# (what is in the space, seconds). The kind also picks the mock inference verdict via the file name.
TIMELINE = [("empty", 120), ("violation", 900), ("empty", 300), ("permitted", 700), ("empty", 120)]
FILENAMES = {"empty": "novehicle.jpg", "violation": "violation.jpg", "permitted": "permitted.jpg"}


class FakeClock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def kind_at(sim):
    elapsed = 0
    for kind, seconds in TIMELINE:
        elapsed += seconds
        if sim < elapsed:
            return kind
    return TIMELINE[-1][0]


class ScriptedDetector:
    """Occupancy from the timeline, through the real consecutive-frame rule (3 in / 6 out)."""

    def __init__(self, clock):
        self.clock = clock
        self.tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))

    def process_frame(self, frame):
        self.tracker.update(kind_at(self.clock()) != "empty")


class Camera:
    led = False

    def capture_frame(self):
        return None

    def capture(self):
        return JPEG_BYTES

    def set_led(self, on):
        self.led = on


@pytest.fixture
def real_rules(app):
    app.config.update(VIOLATION_CONFIRM_COUNT=2, VEHICLE_CLEAR_COUNT=3, EPISODE_GAP_MINUTES=10)


def test_scripted_parking_day_against_the_api(real_rules, client, auth):
    clock = FakeClock()
    camera = Camera()
    headers = {"X-Device-Key": dev_api_key(GANGNAM)}
    photos, heartbeats = [], []

    def send_photo(jpeg, event):
        kind = kind_at(clock())
        response = client.post(f"{API}/device/detections", headers=headers, content_type="multipart/form-data",
                               data={"file": (io.BytesIO(jpeg), FILENAMES[kind]), "event": event})
        body = response.get_json()
        photos.append((round(clock()), kind, event, response.status_code, body.get("result"), body.get("episode_ended")))
        return body if response.status_code in (200, 201) else None

    def send_heartbeat():
        heartbeats.append(round(clock()))
        return client.post(f"{API}/device/heartbeat", headers=headers).status_code == 200

    runner = EventModeRunner(camera, ScriptedDetector(clock), SETTINGS, send_photo, send_heartbeat,
                             clock=clock, sleep=clock.sleep)
    total = sum(seconds for _, seconds in TIMELINE)
    while clock() < total:
        runner.step()

    # 1. The photos the device sent, in order, and what the server answered.
    assert [(p[1], p[2], p[3], p[4], p[5]) for p in photos] == [
        ("violation", "arrived", 200, "violation_pending", None),   # frames 120,125,130 occupied -> 130 s
        ("violation", "confirm", 201, "violation", None),           # 60 s later, new photo -> confirmed
        ("violation", "refresh", 200, "violation_ongoing", None),   # 10 min after arrived
        ("empty", "left", 200, "no_vehicle", True),                 # 30 s after the car left: episode ends
        ("permitted", "arrived", 200, "permitted", None),           # a permitted car costs one photo
        ("permitted", "refresh", 200, "permitted", None),
        ("empty", "left", 200, "no_vehicle", None),                 # nothing to end
    ]
    times = [p[0] for p in photos]
    assert times[0] == 130 and times[1] == 190 and times[2] == 730 and times[3] == 1045
    assert camera.led is False                                       # off again after `left`

    # 2. Empty periods cost no photos; heartbeats keep coming once a minute.
    assert len(heartbeats) == total // 60 + 1 and heartbeats[:3] == [0, 60, 120]   # t=0 and every minute

    # 3. Server side: one violation, ended because the car left, one notification to the assigned admin.
    violations = client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["items"]
    assert len(violations) == 1
    detail = client.get(f"{API}/violations/{violations[0]['id']}", headers=auth("kim@a.com")).get_json()
    assert detail["end_reason"] == "VEHICLE_LEFT" and detail["vehicle_present"] is False
    assert detail["detection_count"] == 3                             # arrived + confirm + refresh
    assert [h["action"] for h in detail["history"]] == ["DETECTED", "NOTIFIED", "VEHICLE_LEFT"]
    unread = client.get(f"{API}/notifications?unread=true", headers=auth("kim@a.com")).get_json()["unread_count"]
    assert unread == 1

    with client.application.app_context():
        machine = db.session.scalar(select(Machine).where(Machine.serial_number == GANGNAM))
        assert machine.last_heartbeat_at is not None
        assert machine.pending_violation_count == 0 and machine.clear_count == 0
        assert db.session.scalar(select(Violation).where(Violation.ended_at.is_(None))) is None


def test_car_that_leaves_before_confirmation_leaves_no_trace(real_rules, client, auth):
    """arrived -> violation_pending, car gone 20 s later: `left` resets the counter, no confirm photo."""
    clock = FakeClock()
    headers = {"X-Device-Key": dev_api_key(GANGNAM)}
    timeline = [("violation", 25), ("empty", 300)]

    def kind():
        return timeline[0][0] if clock() < timeline[0][1] else "empty"

    class Detector:
        tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))

        def process_frame(self, frame):
            self.tracker.update(kind() != "empty")

    sent = []

    def send_photo(jpeg, event):
        response = client.post(f"{API}/device/detections", headers=headers, content_type="multipart/form-data",
                               data={"file": (io.BytesIO(jpeg), FILENAMES[kind()]), "event": event})
        sent.append((event, response.get_json()["result"]))
        return response.get_json()

    runner = EventModeRunner(Camera(), Detector(), SETTINGS, send_photo,
                             lambda: client.post(f"{API}/device/heartbeat", headers=headers).status_code == 200,
                             clock=clock, sleep=clock.sleep)
    while clock() < 325:
        runner.step()

    assert sent == [("arrived", "violation_pending"), ("left", "no_vehicle")]
    assert client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["items"] == []
    with client.application.app_context():
        machine = db.session.scalar(select(Machine).where(Machine.serial_number == GANGNAM))
        assert machine.pending_violation_count == 0
