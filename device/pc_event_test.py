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

Phase 2 비동기 프로토콜(ADR 0019)은 --protocol async 로 같은 타임라인을 돌린다: 사진은 로컬 Outbox 에 먼저 저장되고
POST /device/detection-jobs 로 가며(202/200 = 접수), confirm 은 Heartbeat 응답의 명령으로 받는다. 서버는 QUEUE_BACKEND 가
설정되어 있고 Worker(flask judge-worker)와 재전송기(flask outbox-relay)가 같은 큐를 쓰며 돌고 있어야 한다. 판정은 응답에
없으므로 결과는 관리자 화면이나 DB 에서 본다. 서버가 판단하는 간격(confirm 60초)은 실제 시간이므로 의미 있는 확인은
--speed 1 에서 한다 (배속이 크면 confirm 이 그만큼 일찍 나간다). 같은 프로토콜의 자동 검증은 서버 쪽
backend/tests/test_async_device_integration.py 와 device/test_async_mode.py 에 있다.

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

from outbox import Outbox  # noqa: E402
from pi_client import (AsyncEventRunner, EventModeRunner, EventSettings, load_jpeg, post_heartbeat, post_job,  # noqa: E402
                       send_heartbeat, send_photo)
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
    parser.add_argument("--protocol", choices=("sync", "async"), default=os.environ.get("PG_PROTOCOL", "sync"),
                        help="sync: POST /device/detections (Phase 1, 기본) | async: Outbox + /device/detection-jobs (Phase 2)")
    parser.add_argument("--outbox-dir", help="async 의 Outbox 디렉터리. 기본은 이번 실행 전용 임시 폴더")
    parser.add_argument("--interactive", action="store_true",
                        help="위반이 확정되면(result=violation) 멈추고 Enter 를 기다린다. 기다리는 동안 Heartbeat 는 "
                             "실제 시간으로 계속 보낸다. Enter 를 누르면 타임라인의 다음 항목(빈 자리)으로 건너뛴다")
    args = parser.parse_args()
    if args.protocol == "async" and args.interactive:
        raise SystemExit("--interactive 는 sync 프로토콜의 즉시 판정(result=violation)에 의존합니다. async 와 함께 쓸 수 없습니다.")
    if args.protocol == "async" and args.speed != 1:
        print(f"주의: async 에서 서버가 정한 시간(confirm 60초 등)은 실제 시간입니다. --speed {args.speed:g} 에서는 "
              "명령이 그만큼 일찍 실행됩니다. 프로토콜 확인은 되지만 간격 확인에는 --speed 1 을 쓰세요.")

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
    if args.protocol == "async":
        import tempfile

        outbox_dir = args.outbox_dir or tempfile.mkdtemp(prefix="pg_outbox_")
        wall_start = time.time()
        commands_seen: list[dict] = []

        def job(item):
            clock.pause()   # 서버 응답을 기다리는 동안 시뮬레이션 시계를 세운다 (sync 와 같다)
            started = time.monotonic()
            try:
                outcome = post_job(args.server, args.key, item)
            except OSError as exc:
                events.append({"sim_s": round(clock(), 1), "event": item.event, "seq": item.seq,
                               "event_id": item.event_id, "kind": "network_error", "error": type(exc).__name__})
                print(f"  >>> {item.event:<8} seq={item.seq} -> 네트워크 오류 ({type(exc).__name__}). 같은 내용으로 다시 보냅니다")
                raise
            finally:
                clock.resume()
            events.append({"sim_s": round(clock(), 1), "image": os.path.basename(camera.current()), "event": item.event,
                           "seq": item.seq, "session_id": item.session_id, "event_id": item.event_id,
                           "command_id": item.command_id, "kb": len(item.jpeg) // 1024, "kind": outcome.kind,
                           "http": outcome.status, "server_ms": int((time.monotonic() - started) * 1000)})
            print(f"  >>> {item.event:<8} seq={item.seq} {events[-1]['image']:<14} -> {outcome.kind} (HTTP {outcome.status}"
                  f"{', command ' + item.command_id[:8] if item.command_id else ''}, {events[-1]['server_ms']} ms)")
            return outcome

        def heartbeat_async(body):
            response = post_heartbeat(args.server, args.key, body)
            heartbeats.append(round(clock()))
            for command in (response or {}).get("commands", []):
                if command["command_id"] not in {c["command_id"] for c in commands_seen}:
                    commands_seen.append(command)
                    print(f"  <<< Heartbeat 명령 {command['type']} {command['command_id'][:8]} (due {command['due_at']})")
            return response

        runner = AsyncEventRunner(camera, detector, settings, outbox=Outbox(outbox_dir, clock=time.time),
                                  send_job=job, send_heartbeat=heartbeat_async, clock=clock, sleep=clock.sleep,
                                  wall=lambda: wall_start + clock())
        print(f"async 프로토콜 | Outbox {outbox_dir}")
    else:
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
    if args.protocol == "async":
        summary.update({
            "protocol": "async", "outbox_dir": outbox_dir, "outbox_pending": len(runner.outbox.items()),
            "outbox_dead": [d.__dict__ for d in runner.outbox.dead_items()],
            "commands_delivered": commands_seen, "acks_waiting": list(runner.acks),
            "outbox_health": runner.health.snapshot(),   # also persisted in <outbox>/health.json
        })
    print()
    print(f"frames {summary['frames']} (탐지 평균 {summary['detect_ms_mean']} ms) | 상태 변화 {transitions}")
    if args.protocol == "async":
        print(f"events {[(e['sim_s'], e['event'], e['kind']) for e in events]} | heartbeats {len(heartbeats)}")
        print(f"Outbox 에 남은 것 {summary['outbox_pending']}건, 포기·보관 {len(summary['outbox_dead'])}건, "
              f"받은 명령 {len(commands_seen)}개, 보낼 ack {len(runner.acks)}개")
        print("판정과 위반은 응답에 없습니다. 관리자 화면 또는 DB(detection_jobs, device_commands)에서 확인하세요.")
    else:
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
