"""Phase 2 device protocol (ADR 0019 decision 9, design 6.10): local outbox, retries, heartbeat commands.

No camera, model or server: a fake clock, a scripted occupancy detector and a scripted transport.

    python -m pytest device/test_async_mode.py -q
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from outbox import ExecutedCommands, Outbox, OutboxItem  # noqa: E402
from pi_client import AsyncEventRunner, EventSettings, SendOutcome, classify_job_response  # noqa: E402
from vehicle_detector import OccupancyConfig, OccupancyTracker  # noqa: E402

SETTINGS = EventSettings(detect_interval=5, heartbeat_interval=60, refresh_interval=600, confirm_delay=60)
SERVER_EPOCH = datetime(2026, 11, 2, 14, 59, 0, tzinfo=timezone.utc)   # server clock at fake time 0


class FakeClock:
    """One clock for scheduling (monotonic) and for the outbox (wall): starts just before midnight KST."""

    def __init__(self, start=SERVER_EPOCH.timestamp()):   # 2026-11-02T14:59:00Z = 23:59:00 KST
        self.now = start
        self.start = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def elapsed(self):
        return self.now - self.start


class ScriptedDetector:
    def __init__(self, frames):
        self.frames = list(frames)
        self.tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))

    def process_frame(self, frame):
        self.tracker.update(self.frames.pop(0) if self.frames else (self.tracker.state or False))


class FakeCamera:
    def __init__(self):
        self.led, self.shots = False, 0

    def capture_frame(self):
        return "frame"

    def capture(self):
        self.shots += 1
        return b"\xff\xd8\xffjpeg-%d" % self.shots

    def set_led(self, on):
        self.led = on


class FakeServer:
    """Records every send attempt; replies from a script (default: 202). Heartbeats can carry commands."""

    def __init__(self, clock):
        self.clock = clock
        self.attempts = []         # (elapsed, item metadata dict, jpeg)
        self.replies = []          # SendOutcome or Exception to raise, consumed in order
        self.accepted = {}         # event_id -> jpeg
        self.heartbeats = []       # bodies
        self.commands = []         # commands returned with every heartbeat
        self.violation_active = False
        self.heartbeat_fails = 0

    def send_job(self, item):
        self.attempts.append((self.clock.elapsed(), item.metadata(), item.jpeg))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            duplicate = item.event_id in self.accepted
            self.accepted.setdefault(item.event_id, item.jpeg)
            return SendOutcome("duplicate" if duplicate else "accepted", status=200 if duplicate else 202)
        if reply.kind in ("accepted", "duplicate"):
            self.accepted.setdefault(item.event_id, item.jpeg)
        return reply

    def heartbeat(self, body):
        self.heartbeats.append((self.clock.elapsed(), body))
        if self.heartbeat_fails:
            self.heartbeat_fails -= 1
            return None
        server_now = SERVER_EPOCH + timedelta(seconds=self.clock.elapsed())
        return {"ok": True, "server_time": server_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "violation_active": self.violation_active, "commands": list(self.commands)}

    def events(self):
        return [meta["event"] for _, meta, _ in self.attempts]


def make(tmp_path, frames, rng=lambda: 0.5):
    clock = FakeClock()
    camera = FakeCamera()
    server = FakeServer(clock)
    outbox = Outbox(tmp_path / "outbox", clock=clock)
    runner = AsyncEventRunner(camera, ScriptedDetector(frames), SETTINGS, outbox=outbox,
                              send_job=server.send_job, send_heartbeat=server.heartbeat,
                              clock=clock, sleep=clock.sleep, wall=clock, rng=rng)
    return runner, clock, camera, server, outbox


def run_until(runner, clock, seconds):
    while clock.elapsed() < seconds:
        runner.step()


def command(session, offset_due=60, offset_expires=600, at=0, command_id=None):
    """A heartbeat command whose due/expiry are `offset_*` seconds after server time `at`."""
    base = SERVER_EPOCH + timedelta(seconds=at)
    return {"command_id": command_id or str(uuid.uuid4()), "type": "capture_confirm", "session_id": str(session),
            "due_at": (base + timedelta(seconds=offset_due)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (base + timedelta(seconds=offset_expires)).strftime("%Y-%m-%dT%H:%M:%SZ")}


# --- outbox: stored before sending, identical on every retry ------------------------------------------------

def test_photo_is_in_the_outbox_before_the_first_send_attempt(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 100)
    seen_on_disk = []

    def send_job(item):
        seen_on_disk.append([i.event_id for i in Outbox(tmp_path / "outbox", clock=clock).items()])
        raise ConnectionError("network down")

    runner.send_job = send_job
    run_until(runner, clock, 12)
    assert seen_on_disk and seen_on_disk[0]   # the item was on disk when the send started
    [item] = outbox.items()
    assert item.event == "arrived" and item.jpeg == b"\xff\xd8\xffjpeg-1"


def test_every_retry_sends_the_same_event_metadata_and_bytes(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    server.replies = [ConnectionError("down"), SendOutcome("retry", status=503), ConnectionError("down")]
    run_until(runner, clock, 200)
    attempts = [(meta, jpeg) for _, meta, jpeg in server.attempts if meta["event"] == "arrived"]
    assert len(attempts) == 4
    assert all(a == attempts[0] for a in attempts)                    # event_id, session, seq, event, bytes
    assert attempts[0][0]["seq"] == 1 and attempts[0][0]["command_id"] is None
    assert outbox.items() == []


def test_202_and_a_200_after_a_lost_response_are_both_success(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    # first attempt: the server accepted it but the response was lost
    lost = ConnectionError("read timeout after the server accepted")
    original = server.send_job

    def accept_then_lose(item):
        result = original(item)
        if len(server.attempts) == 1:
            raise lost
        return result

    runner.send_job = accept_then_lose
    run_until(runner, clock, 100)
    assert [o for o in server.events()] == ["arrived", "arrived"]
    assert outbox.items() == []   # the 200 (duplicate) removed it


def test_backoff_is_full_jitter_and_respects_retry_after(tmp_path):
    rolls = iter([1.0, 1.0, 1.0, 0.0])
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000, rng=lambda: next(rolls, 1.0))
    server.replies = [ConnectionError("x"), ConnectionError("x"), ConnectionError("x"),
                      SendOutcome("retry", status=429, retry_after=30), None]
    run_until(runner, clock, 400)
    times = [t for t, meta, _ in server.attempts if meta["event"] == "arrived"]
    gaps = [round(b - a) for a, b in zip(times, times[1:])]
    # attempt n waits random(0, min(60, 2 * 2**n)) * roll: 4, 8, 16, then max(0, Retry-After 30)
    assert gaps == [4, 8, 16, 30]


@pytest.mark.parametrize("status", [400, 401, 403, 409, 413])
def test_permanent_errors_are_not_retried_and_kept_for_inspection(tmp_path, status, caplog):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    server.replies = [SendOutcome("drop", status=status, detail="event_id_conflict" if status == 409 else "")]
    run_until(runner, clock, 300)
    assert server.events().count("arrived") == 1
    assert outbox.items() == []
    [dead] = outbox.dead_items()
    assert dead.event == "arrived" and str(status) in dead.reason
    assert str(status) in caplog.text


def test_outbox_sends_in_order_and_a_failing_head_blocks_the_rest(tmp_path):
    frames = [True] * 4 + [False] * 6 + [True] * 1000   # arrived, left, arrived again
    runner, clock, camera, server, outbox = make(tmp_path, frames)
    server.replies = [ConnectionError("down")] * 6
    run_until(runner, clock, 400)
    sent_order = [meta["event"] for _, meta, _ in server.attempts]
    first_success = len(server.replies) == 0
    assert first_success
    delivered = [e for e in server.accepted]
    metas = {meta["event_id"]: meta for _, meta, _ in server.attempts}
    assert [metas[e]["event"] for e in delivered] == ["arrived", "left", "arrived"]
    assert sent_order[:6] == ["arrived"] * 6                          # nothing overtook the failing head


# --- restart -------------------------------------------------------------------------------------------

def test_restart_resumes_the_outbox_in_order_with_the_same_items(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 4 + [False] * 6 + [True] * 1000)
    server.replies = [ConnectionError("down")] * 100
    run_until(runner, clock, 150)
    before = [(i.event_id, i.event, i.jpeg) for i in outbox.items()]
    assert [e for _, e, _ in before] == ["arrived", "left", "arrived"]

    # process killed: a new process opens the same directory
    server2 = FakeServer(clock)
    outbox2 = Outbox(tmp_path / "outbox", clock=clock)
    runner2 = AsyncEventRunner(FakeCamera(), ScriptedDetector([False] * 1000), SETTINGS, outbox=outbox2,
                               send_job=server2.send_job, send_heartbeat=server2.heartbeat,
                               clock=clock, sleep=clock.sleep, wall=clock, rng=lambda: 0.5)
    clock.sleep(120)
    runner2.step()
    assert [(m["event_id"], m["event"], j) for _, m, j in server2.attempts] == before
    assert outbox2.items() == []


def test_half_written_files_from_a_crash_are_ignored(tmp_path):
    clock = FakeClock()
    outbox = Outbox(tmp_path / "outbox", clock=clock)
    (tmp_path / "outbox" / "items" / "0001.item.tmp").write_bytes(b"partial")
    (tmp_path / "outbox" / "items" / "0002.item").write_bytes(b"not json\nxx")
    assert Outbox(tmp_path / "outbox", clock=clock).items() == []
    assert [d.reason for d in Outbox(tmp_path / "outbox", clock=clock).dead_items()] == ["unreadable outbox file"]


# --- give-up and capacity (design 6.10) ----------------------------------------------------------------------

def test_refresh_is_given_up_after_10_minutes_and_arrived_after_30(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 10000)
    server.replies = [ConnectionError("down")] * 10000
    run_until(runner, clock, 1300)   # arrived at ~10 s, refresh at ~610 s
    events = [i.event for i in outbox.items()]
    assert events == ["arrived", "refresh", "refresh"] or events == ["arrived", "refresh"]
    run_until(runner, clock, 1900)
    assert "arrived" not in [i.event for i in outbox.items()]
    reasons = {d.event: d.reason for d in outbox.dead_items()}
    assert "gave up" in reasons["arrived"] and "gave up" in reasons["refresh"]


def test_capacity_drops_the_oldest_refresh_first(tmp_path):
    clock = FakeClock()
    outbox = Outbox(tmp_path / "outbox", clock=clock, max_items=3)
    session = str(uuid.uuid4())
    for seq, event in enumerate(["arrived", "refresh", "refresh", "left"], start=1):
        outbox.put(OutboxItem.new(session, seq, event, b"\xff\xd8\xff%d" % seq, clock))
        clock.sleep(1)
    assert [i.event for i in outbox.items()] == ["arrived", "refresh", "left"]
    assert [i.seq for i in outbox.items()] == [1, 3, 4]
    assert [d.reason for d in outbox.dead_items()] == ["outbox full"]


# --- heartbeat and commands --------------------------------------------------------------------------------

def test_heartbeat_reports_session_occupancy_last_seq_and_acks(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [False] * 2 + [True] * 1000)
    run_until(runner, clock, 130)
    first, later = server.heartbeats[0][1], server.heartbeats[-1][1]
    assert first == {"session_id": None, "occupied": None, "last_seq": 0, "acked_commands": []}
    assert later["session_id"] == runner.session_id and later["occupied"] is True and later["last_seq"] == 1


def test_confirm_command_is_executed_once_with_its_command_id(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    run_until(runner, clock, 61)   # arrived at 10 s, heartbeat at 60 s
    cmd = command(runner.session_id, offset_due=60, at=10)   # due = server time of arrived + 60 s
    server.commands = [cmd]        # delivered on every heartbeat until fulfilled
    run_until(runner, clock, 400)
    confirms = [meta for _, meta, _ in server.attempts if meta["event"] == "confirm"]
    assert len(confirms) == 1 and confirms[0]["command_id"] == cmd["command_id"]
    assert confirms[0]["session_id"] == runner.session_id and confirms[0]["seq"] == 2
    assert all(cmd["command_id"] not in body["acked_commands"] for _, body in server.heartbeats)


def test_confirm_waits_for_due_at_using_the_server_clock_offset(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    run_until(runner, clock, 61)
    server.commands = [command(runner.session_id, offset_due=60, at=10)]   # due at server +70 s
    run_until(runner, clock, 300)
    [t] = [t for t, meta, _ in server.attempts if meta["event"] == "confirm"]
    assert 120 <= t <= 125   # received at the 120 s heartbeat; due already passed -> right away


def test_expired_or_foreign_session_commands_are_acked_not_executed(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    run_until(runner, clock, 61)
    expired = command(runner.session_id, offset_due=-600, offset_expires=-10, at=60)
    foreign = command(uuid.uuid4(), at=60)
    server.commands = [expired, foreign]
    run_until(runner, clock, 200)
    assert "confirm" not in server.events()
    acked = {c for _, body in server.heartbeats for c in body["acked_commands"]}
    assert {expired["command_id"], foreign["command_id"]} <= acked


def test_leaving_before_the_confirm_drops_and_acks_it(tmp_path):
    # occupied until 120 s, empty from 125 s -> left at 150 s; the command arrives with the 120 s heartbeat, due 260 s
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 25 + [False] * 1000)
    run_until(runner, clock, 61)
    cmd = command(runner.session_id, offset_due=200, at=60)
    server.commands = [cmd]
    run_until(runner, clock, 125)
    assert cmd["command_id"] in runner.pending_confirms              # scheduled, not yet executed
    run_until(runner, clock, 400)
    assert "confirm" not in server.events() and "left" in server.events()
    assert cmd["command_id"] not in runner.pending_confirms
    acked = {c for _, body in server.heartbeats for c in body["acked_commands"]}
    assert cmd["command_id"] in acked


def test_an_executed_command_is_never_acked_even_if_redelivered_after_restart(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    run_until(runner, clock, 61)
    cmd = command(runner.session_id, offset_due=0, at=60)
    server.commands = [cmd]
    run_until(runner, clock, 130)
    assert server.events().count("confirm") == 1
    # restart; the server still lists the command (fulfilment not visible yet) and the session changed
    executed = ExecutedCommands(tmp_path / "outbox" / "executed.json")
    assert cmd["command_id"] in executed
    server2 = FakeServer(clock)
    server2.commands = [cmd]
    runner2 = AsyncEventRunner(FakeCamera(), ScriptedDetector([True] * 1000), SETTINGS,
                               outbox=Outbox(tmp_path / "outbox", clock=clock), send_job=server2.send_job,
                               send_heartbeat=server2.heartbeat, clock=clock, sleep=clock.sleep, wall=clock,
                               rng=lambda: 0.5)
    run_until(runner2, clock, 400)
    assert "confirm" not in server2.events()
    assert all(cmd["command_id"] not in body["acked_commands"] for _, body in server2.heartbeats)


def test_acks_are_kept_until_a_heartbeat_gets_through(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    run_until(runner, clock, 61)
    foreign = command(uuid.uuid4(), at=60)
    server.commands = [foreign]
    run_until(runner, clock, 121)      # learned at 120 s
    server.commands = []
    server.heartbeat_fails = 1         # the 180 s heartbeat is lost
    run_until(runner, clock, 310)
    bodies = [body for t, body in server.heartbeats if t >= 180]            # heartbeats at 180, 240, 300
    assert len(bodies) == 3
    assert foreign["command_id"] in bodies[0]["acked_commands"]             # sent, but the response was lost
    assert foreign["command_id"] in bodies[1]["acked_commands"]             # so it is sent again
    assert foreign["command_id"] not in bodies[2]["acked_commands"]         # confirmed by the 240 s response


def test_led_follows_violation_active_from_the_heartbeat(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    server.violation_active = True
    run_until(runner, clock, 61)
    assert camera.led is True
    server.violation_active = False
    run_until(runner, clock, 121)
    assert camera.led is False


def test_detected_at_is_fixed_at_capture_across_midnight_retries(tmp_path):
    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)   # starts at 23:59:00 local
    server.replies = [ConnectionError("down")] * 5
    run_until(runner, clock, 300)                                           # retries run past midnight
    stamps = {meta["detected_at"] for _, meta, _ in server.attempts if meta["event"] == "arrived"}
    assert len(stamps) == 1                                                  # the capture time, whatever the retry
    captured = datetime.fromisoformat(next(iter(stamps)))
    assert captured.tzinfo is not None
    assert abs((captured - (SERVER_EPOCH + timedelta(seconds=10))).total_seconds()) <= 1   # arrived at 10 s


# --- response classification ----------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [(202, "accepted"), (200, "duplicate"), (503, "retry"), (500, "retry"),
                                         (429, "retry"), (404, "retry"), (409, "drop"), (400, "drop"),
                                         (401, "drop"), (403, "drop"), (413, "drop")])
def test_classify_job_response(status, kind):
    outcome = classify_job_response(status, {"error": {"code": "x"}}, retry_after="7")
    assert outcome.kind == kind
    if kind == "retry":
        assert outcome.retry_after == 7


def test_phase1_runner_is_untouched_by_default():
    from pi_client import EventModeRunner, parse_protocol

    assert parse_protocol({}) == "sync" and parse_protocol({"PG_PROTOCOL": "async"}) == "async"
    assert issubclass(AsyncEventRunner, EventModeRunner)
    with pytest.raises(SystemExit):
        parse_protocol({"PG_PROTOCOL": "carrier-pigeon"})


# --- the real HTTP transport (requests stubbed) --------------------------------------------------------------

class StubResponse:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _item(command_id=None, event="confirm"):
    clock = FakeClock()
    return OutboxItem.new(str(uuid.uuid4()), 2, event, b"\xff\xd8\xffphoto", clock, command_id=command_id)


def test_post_job_sends_exactly_the_stored_item(monkeypatch):
    import pi_client

    seen = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        seen.update(url=url, headers=headers, files=files, data=data, timeout=timeout)
        return StubResponse(202, {"job_id": "j", "status": "queued"})

    monkeypatch.setattr(pi_client.requests, "post", fake_post)
    item = _item(command_id=str(uuid.uuid4()))
    outcome = pi_client.post_job("http://srv/api/v1/", "pgk_x", item)
    assert outcome.kind == "accepted"
    assert seen["url"] == "http://srv/api/v1/device/detection-jobs" and seen["headers"] == {"X-Device-Key": "pgk_x"}
    assert seen["data"] == {k: str(v) for k, v in item.metadata().items()}      # event_id, session_id, seq, event, command_id, detected_at
    assert seen["files"]["file"][1] == item.jpeg and seen["files"]["file"][2] == "image/jpeg"
    assert seen["timeout"] == (5, 60)


def test_post_job_omits_a_missing_command_id_and_reads_retry_after(monkeypatch):
    import pi_client

    sent = {}
    monkeypatch.setattr(pi_client.requests, "post", lambda url, **kw: sent.update(kw) or StubResponse(
        503, None, {"Retry-After": "12"}, text="<html>busy</html>"))
    outcome = pi_client.post_job("http://srv/api/v1", "k", _item(event="refresh"))
    assert "command_id" not in sent["data"]
    assert (outcome.kind, outcome.status, outcome.retry_after) == ("retry", 503, 12.0)   # non-JSON body is fine


def test_post_job_lets_network_errors_reach_the_runner(monkeypatch):
    import pi_client
    import requests

    def boom(*args, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(pi_client.requests, "post", boom)
    with pytest.raises(OSError):   # the runner retries on OSError (requests' exceptions derive from it)
        pi_client.post_job("http://srv/api/v1", "k", _item())


def test_post_job_409_is_a_drop_with_the_server_error_code(monkeypatch):
    import pi_client

    monkeypatch.setattr(pi_client.requests, "post", lambda url, **kw: StubResponse(
        409, {"error": {"code": "event_id_conflict", "message": "x"}}))
    outcome = pi_client.post_job("http://srv/api/v1", "k", _item())
    assert (outcome.kind, outcome.status, outcome.detail) == ("drop", 409, "event_id_conflict")


def test_post_heartbeat_sends_json_and_returns_the_response(monkeypatch):
    import pi_client

    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return StubResponse(200, {"ok": True, "commands": []})

    monkeypatch.setattr(pi_client.requests, "post", fake_post)
    body = {"session_id": "s", "occupied": True, "last_seq": 3, "acked_commands": ["c"]}
    assert pi_client.post_heartbeat("http://srv/api/v1", "k", body) == {"ok": True, "commands": []}
    assert seen["json"] == body and seen["url"].endswith("/device/heartbeat")


@pytest.mark.parametrize("response", [StubResponse(500), StubResponse(200, None, text="oops"), StubResponse(200, [1])])
def test_post_heartbeat_failures_return_none(monkeypatch, response):
    import pi_client

    monkeypatch.setattr(pi_client.requests, "post", lambda *a, **k: response)
    assert pi_client.post_heartbeat("http://srv/api/v1", "k", {}) is None


def test_post_heartbeat_network_error_returns_none(monkeypatch):
    import pi_client
    import requests

    monkeypatch.setattr(pi_client.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))
    assert pi_client.post_heartbeat("http://srv/api/v1", "k", {}) is None


def test_a_409_command_conflict_is_not_retried_and_is_kept_with_its_reason(tmp_path, caplog):
    """Decision 2026-11-02: the server refuses an invalid command_id with 409 command_conflict. The device already
    treats every 409 as not retryable; this pins it for the new code."""
    outcome = classify_job_response(409, {"error": {"code": "command_conflict", "message": "x"}})
    assert (outcome.kind, outcome.status, outcome.detail) == ("drop", 409, "command_conflict")

    runner, clock, camera, server, outbox = make(tmp_path, [True] * 1000)
    server.replies = [SendOutcome("drop", status=409, detail="command_conflict")]
    run_until(runner, clock, 300)
    assert server.events().count("arrived") == 1                       # one attempt, never retried
    assert outbox.items() == []
    [dead] = outbox.dead_items()
    assert dead.reason == "409 command_conflict" and "409" in caplog.text
