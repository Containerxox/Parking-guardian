"""The Phase 2 device runner (device/pi_client.AsyncEventRunner + device/outbox.py) driven against the real API,
the in-memory queue and the real judgement worker (ADR 0019 decisions 8-9, design 6.10).

Occupancy is SCRIPTED and inference is scripted from the photo bytes, so this verifies the protocol end to end:
outbox -> POST /device/detection-jobs -> worker -> confirm command -> heartbeat -> confirm photo with its
command_id -> fulfilled -> violation. It says nothing about YOLO/ROI accuracy. The device clock is fake (it
advances only by sleeping); the server clock is real, which is why the command's due time is carried as an offset
from the server's own `server_time` (design 6.9).
"""
import io
import uuid
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import dev_api_key
from app.extensions import db
from app.models import (CommandStatus, DetectionJob, DeviceCommand, EndReason, JobStatus, Machine, Violation)

from .conftest import API
from .test_worker import GANGNAM, app_overrides, clock, drain, env, worker  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "device"))
from outbox import ExecutedCommands, Outbox, OutboxItem  # noqa: E402
from pi_client import AsyncEventRunner, EventSettings, classify_job_response  # noqa: E402
from vehicle_detector import OccupancyConfig, OccupancyTracker  # noqa: E402

SETTINGS = EventSettings(detect_interval=5, heartbeat_interval=60, refresh_interval=600, confirm_delay=60)
KEY = {"X-Device-Key": dev_api_key(GANGNAM)}
LEAVES_AT = 132   # default: the car is in the space until this device-clock second (tests may change camera.leaves_at)


class DeviceClock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 14, 59, tzinfo=timezone.utc).timestamp()
        self.start = self.now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def elapsed(self):
        return self.now - self.start


class ScriptedDetector:
    def __init__(self, clock, camera):
        self.clock, self.camera = clock, camera
        self.tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))

    def process_frame(self, frame):
        self.tracker.update(self.clock.elapsed() < self.camera.leaves_at)


class Camera:
    """A violating car until LEAVES_AT, then an empty space. Every photo is different bytes."""

    def __init__(self, clock):
        self.clock, self.shots, self.led = clock, 0, False
        self.leaves_at = LEAVES_AT

    def capture_frame(self):
        return "frame"

    def capture(self):
        self.shots += 1
        kind = b"violation-" if self.clock.elapsed() < self.leaves_at else b"novehicle-"
        return b"\xff\xd8\xff\xe0" + kind + b"%d" % self.shots

    def set_led(self, on):
        self.led = on


class Link:
    """The network between the device and the Flask test client, with failures the tests can switch on."""

    def __init__(self, app, clock):
        self.client = app.test_client()
        self.clock = clock
        self.down_until = -1.0          # device-clock second: every request fails before this
        self.jobs_down_until = -1.0     # same, but only photo uploads fail (heartbeats still get through)
        self.lose_next_job_response = 0  # the server handles it, the response never arrives
        self.lose_next_heartbeat_with_command = 0
        self.job_requests = []

    def send_job(self, item):
        if self.clock.elapsed() < max(self.down_until, self.jobs_down_until):
            raise ConnectionError("network down")
        form = {"file": (io.BytesIO(item.jpeg), "p.jpg"),
                **{k: str(v) for k, v in item.metadata().items() if v is not None}}
        response = self.client.post(f"{API}/device/detection-jobs", data=form, content_type="multipart/form-data",
                                    headers=KEY)
        self.job_requests.append((item.event, item.event_id, response.status_code))
        if self.lose_next_job_response:
            self.lose_next_job_response -= 1
            raise ConnectionError("response lost")
        return classify_job_response(response.status_code, response.get_json(), response.headers.get("Retry-After"))

    def send_heartbeat(self, body):
        if self.clock.elapsed() < self.down_until:
            raise ConnectionError("network down")
        response = self.client.post(f"{API}/device/heartbeat", json=body, headers=KEY)
        data = response.get_json() if response.status_code == 200 else None
        if data and data.get("commands") and self.lose_next_heartbeat_with_command:
            self.lose_next_heartbeat_with_command -= 1
            return None   # the server marked it delivered, but this response never reached the device
        return data


@pytest.fixture
def rig(app, env, worker, tmp_path):
    clock = DeviceClock()
    link = Link(app, clock)
    camera = Camera(clock)
    runner = AsyncEventRunner(camera, ScriptedDetector(clock, camera), SETTINGS, outbox=Outbox(tmp_path / "outbox", clock=clock),
                              send_job=link.send_job, send_heartbeat=link.send_heartbeat, clock=clock,
                              sleep=clock.sleep, wall=clock, rng=lambda: 0.5)   # deterministic jitter: no flaky timing

    def run(seconds):
        while clock.elapsed() < seconds:
            runner.step()
            drain(app, worker)

    return runner, clock, link, camera, run


def _state(app):
    with app.app_context():
        machine = db.session.scalars(select(Machine).where(Machine.serial_number == GANGNAM)).one()
        jobs = db.session.scalars(select(DetectionJob).where(DetectionJob.machine_id == machine.id)
                                  .order_by(DetectionJob.created_at, DetectionJob.seq)).all()
        commands = db.session.scalars(select(DeviceCommand).where(DeviceCommand.machine_id == machine.id)).all()
        violations = db.session.scalars(select(Violation).where(Violation.machine_id == machine.id)).all()
        return ([(j.event, j.seq, j.status, str(j.command_id) if hasattr(j, "command_id") else None) for j in jobs],
                [(c.status, str(c.command_id)) for c in commands],
                [(v.detection_count, v.ended_at is not None, v.end_reason, v.image_status) for v in violations])


def test_normal_confirm_flow_ends_with_a_fulfilled_command_and_a_violation(app, rig):
    runner, clock, link, camera, run = rig
    run(125)
    jobs, commands, violations = _state(app)
    assert [(e, s) for e, s, _, _ in jobs] == [("arrived", 1), ("confirm", 2)]
    assert all(status == JobStatus.DONE for _, _, status, _ in jobs)
    [(command_status, command_id)] = commands
    assert command_status == CommandStatus.FULFILLED                       # by the confirm photo, not by delivery
    assert violations == [(2, False, None, "UPLOADED")]                    # two sightings confirmed one violation
    assert runner.outbox.items() == [] and runner.acks == []              # nothing left to send, nothing wrongly acked
    assert command_id in runner.executed
    assert [e for e, _, code in link.job_requests] == ["arrived", "confirm"]   # one request per photo


def test_a_heartbeat_response_lost_after_delivery_still_gives_one_confirm(app, rig):
    runner, clock, link, camera, run = rig
    link.lose_next_heartbeat_with_command = 1
    # The device clock is fake and runs ahead of the real server clock, so on the second delivery (device t=120) the
    # server still says "due in ~60 s" and the confirm is scheduled for t=180. The car stays until then.
    camera.leaves_at = 1000
    run(260)
    jobs, commands, violations = _state(app)
    assert [e for e, _, _, _ in jobs] == ["arrived", "confirm"]            # the command came again, executed once
    assert [c[0] for c in commands] == [CommandStatus.FULFILLED]
    assert violations[0][0] == 2
    assert camera.shots == 2


def test_leaving_ends_the_episode_and_turns_the_led_off(app, rig):
    runner, clock, link, camera, run = rig
    run(300)
    jobs, commands, violations = _state(app)
    assert [e for e, _, _, _ in jobs] == ["arrived", "confirm", "left"]
    assert violations == [(2, True, EndReason.VEHICLE_LEFT, "UPLOADED")]   # one empty-space photo closes it
    assert camera.led is False
    assert [c[0] for c in commands] == [CommandStatus.FULFILLED]


def test_network_down_then_a_lost_202_gives_one_job_per_event_in_order(app, rig):
    runner, clock, link, camera, run = rig
    link.down_until = 45          # arrived (10 s) is stored locally and retried until the network returns
    link.lose_next_job_response = 1
    # With jitter 0.5 the arrival is accepted at t=70 (its response is lost), the duplicate answers at t=100, the
    # command arrives with the t=120 heartbeat and the confirm runs at t=180. The car must still be there then.
    camera.leaves_at = 400
    run(460)
    jobs, commands, violations = _state(app)
    assert [e for e, _, _, _ in jobs] == ["arrived", "confirm", "left"]    # one job each, in order
    sent = [(e, code) for e, _, code in link.job_requests]
    assert ("arrived", 202) in sent and ("arrived", 200) in sent           # the lost 202, then the 200 duplicate
    assert len({event_id for _, event_id, _ in link.job_requests if event_id}) == 3
    assert runner.outbox.items() == [] and runner.outbox.dead_items() == []
    assert violations[0][1] is True


def test_losing_the_executed_record_cannot_make_the_server_count_the_confirm_twice(app, rig):
    """T9 (ADR 0019 "confirm 명령 중복 반영 보호"): the first confirm photo is stored on the device but cannot be
    delivered; executed.json is lost; the server still lists the command, so the device takes a second photo for it.
    When the network returns the server must end up with ONE confirm job and detection_count 2, not 3."""
    runner, clock, link, camera, run = rig
    camera.leaves_at = 2000
    run(100)                                  # arrived accepted and judged at t=10; the command arrives at the t=60 heartbeat
    link.jobs_down_until = 400                # photos cannot be delivered from now on; heartbeats still work
    run(125)                                  # t=120: the command is executed, confirm photo #1 is stored but stuck
    [first] = [i for i in runner.outbox.items() if i.event == "confirm"]
    command_id = first.command_id
    assert command_id in runner.executed

    path = runner.outbox.root / "executed.json"
    path.unlink()                             # the "already executed" record is lost, the Outbox is intact
    runner.executed = ExecutedCommands(path)

    run(250)                                  # the next heartbeat redelivers the command: executed again (t~235)
    confirms = [i for i in runner.outbox.items() if i.event == "confirm"]
    assert len(confirms) == 2 and {c.command_id for c in confirms} == {command_id}
    assert confirms[0].event_id != confirms[1].event_id            # two different photos for one command

    run(520)                                  # the network is back at t=400: both are delivered, in order
    jobs, commands, violations = _state(app)
    assert [event for event, _, _, _ in jobs] == ["arrived", "confirm"]          # the server has ONE confirm job
    assert [status for status, _ in commands] == [CommandStatus.FULFILLED]
    assert violations == [(2, False, None, "UPLOADED")]                          # detection_count 2, not 3
    codes = sorted(code for event, _, code in link.job_requests if event == "confirm")
    assert codes == [200, 202]                                                   # the second one was a duplicate
    assert runner.outbox.items() == [] and runner.outbox.dead_items() == []     # the device treated 200 as done


def test_a_photo_naming_an_invalid_command_is_dropped_by_the_device_and_never_retried(app, rig):
    """Server and device agree: an explicit command_id that is not a valid confirm command of this machine and session
    comes back 409 command_conflict, which the device treats as not retryable (dead/, one attempt)."""
    runner, clock, link, camera, run = rig
    camera.leaves_at = 2000
    run(30)                                                           # arrived accepted: the session exists
    item = OutboxItem.new(runner.session_id, 90, "confirm", b"\xff\xd8\xff\xe0violation-bogus", clock,
                          command_id=str(uuid.uuid4()))              # a command nobody ever created
    outcome = link.send_job(item)                                     # straight over the wire
    assert (outcome.kind, outcome.status, outcome.detail) == ("drop", 409, "command_conflict")

    second = runner.outbox.put(OutboxItem.new(runner.session_id, 91, "confirm", b"\xff\xd8\xff\xe0violation-bogus2",
                                              clock, command_id=str(uuid.uuid4())))
    run(clock.elapsed() + 120)               # the runner sends it once and gives up on it (the genuine confirm also runs)
    bogus = {item.event_id, second.event_id}
    assert [code for _, event_id, code in link.job_requests if event_id in bogus] == [409, 409]   # one attempt each
    assert [d.reason for d in runner.outbox.dead_items()] == ["409 command_conflict"]
    assert [i.event_id for i in runner.outbox.items() if i.event_id in bogus] == []
