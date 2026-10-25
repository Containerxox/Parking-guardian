"""PC 에서 이벤트 모드 전체 경로를 돌려 본다: 실제 사진 프레임 -> YOLO26s -> ROI 점유 판정 -> EventModeRunner -> 서버.

Raspberry Pi 가 없을 때 카메라 대신 사진 목록(타임라인)을 프레임으로 넣는다. 탐지기는 진짜(vehicle_detector.
VehicleDetector) 이고, 상태 기계와 전송도 pi_client 의 것을 그대로 쓴다. 가짜인 것은 카메라와 시계뿐이다.

    backend\\venv\\Scripts\\python device\\pc_event_test.py --key pgk_dev_rpi-a-gangnam-01 ^
        --timeline "empty:60,backend/uploads/sample_c.jpg:200,empty:90" --speed 10 --out result.json

타임라인 항목은 `사진경로:초` 다. `empty` 는 첫 번째 실제 사진을 강하게 흐린 합성 프레임이다. 즉 "탐지되는 것이 없는
프레임"을 흉내 낼 뿐 실제 빈 주차면 사진이 아니다. 실제 빈 자리에서의 오탐 여부는 이 도구로 알 수 없다.

출력: 프레임마다 (시뮬레이션 시각, 사용한 사진, 프레임 점유 판정, 인정된 박스 수, 추적 상태, 탐지 ms), 보낸 사진과
서버 응답, 요약. --out 을 주면 JSON 으로 저장한다. 시뮬레이션 시각은 --speed 배속으로 흐른다.

실제 빈 자리 사진이 있으면 `empty` 대신 그 사진을 넣는다 (backend/uploads/sample_f.jpg). 브라우저로 관리자 화면을
보면서 진행하려면 --interactive: 위반이 확정되면(result=violation) 멈추고 Enter 를 기다린다. 기다리는 동안 Heartbeat
는 실제 시간으로 계속 보내고(서버의 10분 끊김 판정 방지), Enter 를 누르면 타임라인의 다음 항목(빈 자리)으로 건너뛰어
출차(left)를 보낸다. 컨테이너 서버(--server http://127.0.0.1:5000/api/v1)에 보내면 개발 DB 에 위반이 생긴다.

    backend\\venv\\Scripts\\python device\\pc_event_test.py --server http://127.0.0.1:5000/api/v1 --interactive ^
        --timeline "backend/uploads/sample_f.jpg:60,backend/uploads/sample_e.jpg:700,backend/uploads/sample_f.jpg:90"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pi_client import EventModeRunner, EventSettings, load_jpeg, send_heartbeat, send_photo  # noqa: E402
from vehicle_detector import OccupancyConfig, VehicleDetector  # noqa: E402

log = logging.getLogger("pc_event_test")


def parse_timeline(text: str) -> list[tuple[str, float]]:
    steps = []
    for token in text.split(","):
        path, _, seconds = token.strip().rpartition(":")
        if not path or not seconds:
            raise SystemExit(f"타임라인 항목은 '사진경로:초' 입니다: {token!r}")
        steps.append((path, float(seconds)))
    return steps


def blurred_copy(frame_bgr):
    import cv2

    out = frame_bgr
    for _ in range(3):
        out = cv2.GaussianBlur(out, (0, 0), 25)
    return out


class TimelineCamera:
    """타임라인에 따라 프레임(640 폭 BGR)과 전송용 JPEG 를 돌려준다."""

    def __init__(self, steps, clock, send_width: int, jpeg_quality: int):
        import cv2

        self.steps = steps
        self.clock = clock
        self.frames, self.jpegs = {}, {}
        first_real = next((p for p, _ in steps if p != "empty"), None)
        if first_real is None:
            raise SystemExit("타임라인에 실제 사진이 하나는 있어야 합니다.")
        for path, _ in steps:
            if path in self.frames:
                continue
            source = first_real if path == "empty" else path
            image = cv2.imread(source)
            if image is None:
                raise SystemExit(f"사진을 읽을 수 없습니다: {source}")
            frame = cv2.resize(image, (640, int(image.shape[0] * 640 / image.shape[1])))
            if path == "empty":
                frame = blurred_copy(frame)
                ok, encoded = cv2.imencode(".jpg", blurred_copy(cv2.resize(image, (send_width, int(image.shape[0] * send_width / image.shape[1])))
                                                                 if image.shape[1] > send_width else image),
                                           [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                self.jpegs[path] = encoded.tobytes()
            else:
                self.jpegs[path] = load_jpeg(path, send_width, jpeg_quality)
            self.frames[path] = frame
        self.led = False

    def current(self) -> str:
        elapsed, now = 0.0, self.clock()
        for path, seconds in self.steps:
            elapsed += seconds
            if now < elapsed:
                return path
        return self.steps[-1][0]

    def current_step_end(self) -> float:
        """지금 보여 주는 타임라인 항목이 끝나는 시뮬레이션 시각. 다음 항목(예: 빈 자리)으로 건너뛸 때 쓴다."""
        elapsed, now = 0.0, self.clock()
        for _, seconds in self.steps:
            elapsed += seconds
            if now < elapsed:
                return elapsed
        return elapsed

    def capture_frame(self):
        return self.frames[self.current()]

    def capture(self) -> bytes:
        return self.jpegs[self.current()]

    def set_led(self, on: bool) -> None:
        self.led = bool(on)


class SimClock:
    def __init__(self, speed: float):
        self.speed = speed
        self.start = time.monotonic()
        self._paused_at: float | None = None   # --interactive 로 멈춰 있는 동안의 시뮬레이션 시각

    def __call__(self) -> float:
        if self._paused_at is not None:
            return self._paused_at
        return (time.monotonic() - self.start) * self.speed

    def sleep(self, seconds: float) -> None:
        real = seconds / self.speed
        if real > 0:
            time.sleep(real)

    def pause(self) -> None:
        self._paused_at = self()

    def resume(self, jump_to: float | None = None) -> None:
        """멈춘 시각(또는 그보다 뒤인 jump_to)부터 다시 흐르게 한다."""
        target = max(self._paused_at or 0.0, jump_to or 0.0)
        self.start = time.monotonic() - target / self.speed
        self._paused_at = None


def wait_for_enter(message: str, heartbeat, interval_seconds: float) -> None:
    """Enter 를 기다린다. 기다리는 동안 실제 시간으로 Heartbeat 를 보내 서버의 끊김 판정(10분)을 막는다."""
    import threading

    pressed = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), pressed.set()), daemon=True).start()
    print(f"\n{message}", flush=True)
    while not pressed.wait(timeout=interval_seconds):
        heartbeat()
        print("  (대기 중 Heartbeat 전송)", flush=True)


class LoggingDetector:
    """VehicleDetector 를 감싸 프레임마다 판정을 기록한다."""

    def __init__(self, detector: VehicleDetector, camera: TimelineCamera, clock):
        self.detector = detector
        self.tracker = detector.tracker
        self.camera = camera
        self.clock = clock
        self.rows = []

    def process_frame(self, frame):
        judgement = self.detector.process_frame(frame)
        row = {
            "sim_s": round(self.clock(), 1),
            "image": os.path.basename(self.camera.current()),
            "frame_occupied": judgement.occupied,
            "accepted_boxes": sum(1 for b in judgement.boxes if b.accepted),
            "all_boxes": len(judgement.boxes),
            "state": self.tracker.state,
            "ms": judgement.elapsed_ms,
        }
        self.rows.append(row)
        print(f"  frame {row['sim_s']:7.1f}s {row['image']:<14} occupied={str(row['frame_occupied']):<5} "
              f"boxes={row['accepted_boxes']}/{row['all_boxes']} state={row['state']} {row['ms']} ms")
        return judgement


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=os.environ.get("PG_SERVER_URL", "http://127.0.0.1:5001/api/v1"))
    parser.add_argument("--key", default=os.environ.get("PG_DEVICE_KEY", "pgk_dev_rpi-a-gangnam-01"))
    parser.add_argument("--zone", default=os.environ.get("PG_ZONE_NAME") or None)
    parser.add_argument("--timeline", default="empty:60,backend/uploads/sample_c.jpg:200,empty:90")
    parser.add_argument("--speed", type=float, default=10.0, help="시뮬레이션 배속")
    parser.add_argument("--model", default=os.environ.get("PG_DETECTOR_MODEL", "device/yolo26s.pt"))
    parser.add_argument("--imgsz", type=int, default=int(os.environ.get("PG_DETECTOR_IMGSZ", "480")))
    parser.add_argument("--send-width", type=int, default=1280)
    parser.add_argument("--jpeg-quality", type=int, default=92)
    parser.add_argument("--out", help="결과 JSON 저장 경로")
    parser.add_argument("--interactive", action="store_true",
                        help="위반이 확정되면(result=violation) 멈추고 Enter 를 기다린다. 기다리는 동안 Heartbeat 는 "
                             "실제 시간으로 계속 보낸다. Enter 를 누르면 타임라인의 다음 항목(빈 자리)으로 건너뛴다")
    args = parser.parse_args()

    steps = parse_timeline(args.timeline)
    total = sum(s for _, s in steps)
    settings = EventSettings.from_env()
    clock = SimClock(args.speed)
    camera = TimelineCamera(steps, clock, args.send_width, args.jpeg_quality)
    detector = LoggingDetector(VehicleDetector(model_path=args.model, imgsz=args.imgsz,
                                               config=OccupancyConfig.from_env()), camera, clock)
    events = []

    def photo(jpeg, event):
        # 서버 응답을 기다리는 동안은 시뮬레이션 시계를 세운다. 컨테이너 서버의 첫 추론은 모델 로딩으로 20~30초가
        # 걸리는데, 10배속에서는 그것만으로 타임라인 전체(수백 초)가 지나가 버린다. 실제 걸린 시간은 server_ms 에 남긴다.
        clock.pause()
        started = time.monotonic()
        body = send_photo(args.server, args.key, jpeg, args.zone, event=event)
        clock.resume()
        inference = (body or {}).get("inference") or {}
        events.append({"sim_s": round(clock(), 1), "image": os.path.basename(camera.current()), "event": event,
                       "kb": len(jpeg) // 1024, "result": (body or {}).get("result"),
                       "verdict": inference.get("verdict"), "sticker_count": inference.get("sticker_count"),
                       "server_ms": int((time.monotonic() - started) * 1000),
                       "violation_id": (body or {}).get("violation_id"),
                       "episode_ended": bool((body or {}).get("episode_ended")), "ok": body is not None})
        print(f"  >>> {event:<8} {events[-1]['image']:<14} -> {events[-1]['result']}  (verdict={events[-1]['verdict']} "
              f"stickers={events[-1]['sticker_count']} {events[-1]['server_ms']} ms, {len(jpeg) // 1024} KB)")
        if args.interactive and events[-1]["result"] == "violation":
            # 위반이 확정된 뒤에만 멈춘다 (violation_pending 에서는 멈추지 않는다). 시뮬레이션 시계를 세워 두고,
            # 실제 시간으로 Heartbeat 를 보내 SIGNAL_LOST 를 막는다. Enter 뒤에는 다음 타임라인 항목으로 건너뛴다.
            clock.pause()
            paused_at = clock()
            wait_for_enter(f"위반이 확정되었습니다 (violation_id={events[-1]['violation_id']}). "
                           "관리자 화면에서 알림과 위반 내역을 확인한 후 Enter 키를 누르세요.",
                           heartbeat, settings.heartbeat_interval)
            clock.resume(jump_to=camera.current_step_end())
            if runner.refresh_due is not None:
                # 건너뛴 시간만큼 refresh 기한도 미룬다. 안 그러면 재개 직후 빈 자리 사진이 refresh 로 나간다.
                runner.refresh_due += clock() - paused_at
            print(f"  재개: 시뮬레이션 {clock():.0f}초, 다음 사진 {os.path.basename(camera.current())}", flush=True)
        return body

    heartbeats = []

    def heartbeat():
        ok = send_heartbeat(args.server, args.key)
        heartbeats.append(round(clock()))
        return ok

    # 모델을 먼저 올리고 한 프레임 돌려 둔다(첫 추론은 몇 초 걸린다). 시뮬레이션 시계는 그 뒤에 시작한다.
    detector.detector._ensure_loaded()
    detector.detector.detect_boxes(camera.frames[steps[0][0]])
    print(f"timeline {args.timeline} ({total:g}s) at {args.speed}x | model {args.model} imgsz {args.imgsz} | "
          f"ROI {OccupancyConfig.from_env().roi}")
    clock.start = time.monotonic()
    runner = EventModeRunner(camera, detector, settings, photo, heartbeat, clock=clock, sleep=clock.sleep)
    while clock() < total:
        runner.step()

    transitions = [(r["sim_s"], r["state"]) for i, r in enumerate(detector.rows)
                   if i == 0 or r["state"] != detector.rows[i - 1]["state"]]
    summary = {
        "timeline": args.timeline, "speed": args.speed, "model": args.model, "imgsz": args.imgsz,
        "frames": len(detector.rows),
        "detect_ms_mean": round(sum(r["ms"] for r in detector.rows) / max(1, len(detector.rows))),
        "state_transitions": transitions,
        "events": events,
        "heartbeats": len(heartbeats),
        "led_on_at_end": camera.led,
        "frame_rows": detector.rows,
    }
    print()
    print(f"frames {summary['frames']} (탐지 평균 {summary['detect_ms_mean']} ms) | 상태 변화 {transitions}")
    print(f"events {[(e['sim_s'], e['event'], e['result']) for e in events]} | heartbeats {len(heartbeats)}")
    if args.interactive:
        if any(e["episode_ended"] for e in events):
            print("사건이 종료되었습니다. 브라우저에서 위반 상세의 종료 이력(VEHICLE_LEFT)을 확인하세요.")
        elif not any(e["result"] == "violation" for e in events):
            print("위반이 확정되지 않아 멈추지 않았습니다. 서버 판정값(위 result)을 확인하세요.")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
