"""이벤트 모드 상태 기계(pi_client.EventModeRunner) 테스트. 카메라, 모델, 서버 없이 가짜 시간으로 돈다.

    python -m pytest device/test_event_mode.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pi_client import EventModeRunner, EventSettings  # noqa: E402
from vehicle_detector import OccupancyConfig, OccupancyTracker  # noqa: E402

SETTINGS = EventSettings(detect_interval=5, heartbeat_interval=60, refresh_interval=600, confirm_delay=60)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedDetector:
    """process_frame 이 불릴 때마다 미리 정한 점유값을 차례로 돌려준다. 연속 프레임 규칙은 실제 tracker 를 쓴다."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))

    def process_frame(self, frame):
        occupied = self.frames.pop(0) if self.frames else (self.tracker.state or False)
        self.tracker.update(occupied)


class FakeCamera:
    def __init__(self):
        self.led = False
        self.shots = 0

    def capture_frame(self):
        return "frame"

    def capture(self):
        self.shots += 1
        return b"jpeg-%d" % self.shots

    def set_led(self, on):
        self.led = on


class FakeServer:
    """서버 응답을 흉내 낸다. results 는 event -> 응답(또는 None=전송 실패) 의 큐."""

    def __init__(self, results):
        self.results = list(results)
        self.photos = []        # (time, event, jpeg)
        self.heartbeats = []    # times

    def photo(self, jpeg, event, clock):
        self.photos.append((clock(), event, jpeg))
        return self.results.pop(0) if self.results else {"result": "no_vehicle"}

    def heartbeat(self, clock):
        self.heartbeats.append(clock())
        return True


def make(frames, results):
    clock = FakeClock()
    camera = FakeCamera()
    server = FakeServer(results)
    runner = EventModeRunner(camera, ScriptedDetector(frames), SETTINGS,
                             send_photo=lambda jpeg, event: server.photo(jpeg, event, clock),
                             send_heartbeat=lambda: server.heartbeat(clock),
                             clock=clock, sleep=clock.sleep)
    return runner, clock, camera, server


def run_until(runner, clock, seconds):
    while clock.now < seconds:
        runner.step()


def test_empty_space_sends_only_heartbeats():
    runner, clock, camera, server = make(frames=[False] * 100, results=[])
    run_until(runner, clock, 300)
    assert server.photos == []
    assert len(server.heartbeats) == 5 and server.heartbeats[:2] == [0, 60]
    assert runner.occupied is False and camera.led is False


def test_arrival_sends_one_photo_and_a_violation_needs_a_confirm_photo():
    # 비점유 2프레임, 그 뒤 점유. 3프레임 연속 점유(=15초)에서 arrived.
    runner, clock, camera, server = make(
        frames=[False, False] + [True] * 100,
        results=[{"result": "violation_pending"}, {"result": "violation", "violation_id": 7}])
    run_until(runner, clock, 120)
    events = [(t, e) for t, e, _ in server.photos]
    assert events[0][1] == "arrived" and events[0][0] == 20          # 프레임 0,5 비점유 / 10,15,20 점유
    assert events[1] == (80, "confirm")                              # arrived 60초 뒤 새 사진
    assert server.photos[0][2] != server.photos[1][2]                # 같은 사진을 다시 보내는 것이 아니다
    assert camera.led is True and runner.last_result == "violation"


def test_permitted_car_costs_a_single_photo_and_periodic_refresh():
    runner, clock, camera, server = make(frames=[True] * 1000, results=[{"result": "permitted"}] * 10)
    run_until(runner, clock, 1300)
    events = [(t, e) for t, e, _ in server.photos]
    assert events == [(10, "arrived"), (610, "refresh"), (1210, "refresh")]   # 부팅 뒤 3프레임, 그 뒤 10분마다
    assert camera.led is False
    assert len(server.heartbeats) == 22                                        # 0, 60, ..., 1260


def test_leaving_sends_a_left_photo_after_six_empty_frames_and_turns_the_led_off():
    frames = [True] * 10 + [False] * 100
    runner, clock, camera, server = make(
        frames=frames,
        results=[{"result": "violation_pending"}, {"result": "violation", "violation_id": 1},
                 {"result": "no_vehicle", "episode_ended": True}])
    run_until(runner, clock, 200)
    events = [(t, e) for t, e, _ in server.photos]
    assert [e for _, e in events] == ["arrived", "confirm", "left"]
    # 점유 프레임 0..45(10개), 비점유 50..75 => 6프레임째인 75초에 left
    assert events[2][0] == 75
    assert runner.occupied is False and camera.led is False


def test_car_leaving_before_confirm_cancels_the_confirm_photo():
    frames = [True] * 4 + [False] * 100   # arrived at 10 s, empty from 20 s, left at 45 s (< confirm 70 s)
    runner, clock, camera, server = make(
        frames=frames, results=[{"result": "violation_pending"}, {"result": "no_vehicle"}])
    run_until(runner, clock, 200)
    assert [e for _, e, _ in server.photos] == ["arrived", "left"]
    assert runner.confirm_due is None


def test_failed_send_is_logged_and_not_retried():
    runner, clock, camera, server = make(frames=[True] * 1000, results=[None, {"result": "permitted"}])
    run_until(runner, clock, 700)
    assert [e for _, e, _ in server.photos] == ["arrived", "refresh"]   # arrived 실패, 재시도 없음, refresh 는 정상
    assert runner.photos_sent == 1


def test_settings_come_from_environment():
    settings = EventSettings.from_env({"PG_DETECT_INTERVAL": "2", "PG_REFRESH_INTERVAL": "300"})
    assert settings.detect_interval == 2 and settings.refresh_interval == 300
    assert settings.heartbeat_interval == 60 and settings.confirm_delay == 60
