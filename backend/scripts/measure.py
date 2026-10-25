"""Measure the server cost of one device for a scripted parking scenario (Before/After, ADR 0014).

A virtual device plays a scenario such as "empty 30 min, permitted car 60 min, empty 20 min,
violation car 60 min, ..." against a running backend and records what the server had to do:
photos, bytes, inference time, request latency, CPU time and memory of the server process, and
how long the admin waited for the notification. Simulated time runs faster than real time
(--speed) so a 4-hour scenario takes minutes.

Two device modes share the same scenario so the numbers are comparable (AI_FINAL_DESIGN.md 7):

  interval  the old device: a photo every simulated minute, car or no car (device/pi_client.py
            PG_MODE=interval, kept only for this comparison). This is the "Before" measurement.
  event     the device decides when to send (PG_MODE=event, the production default since
            2026-10-24): arrived / confirm / refresh / left photos plus a heartbeat every minute. The occupancy detector is replaced by the
            scenario itself, so this measures the protocol, not the Pi's YOLO. "After".

Typical use (a dedicated server on port 5001 with its own database, see docs/measurements/README.md):

    python scripts/measure.py --mode interval --url http://127.0.0.1:5001/api/v1 --server-pid 1234 \
        --out ../docs/measurements/before-interval.json

The scenario images are the sample photos (sample_a/sample_c: sticker, sample_b: violation). There is
no photo of the empty space, so one is synthesised by blurring a sample until no windshield is found.
Treat its inference time as representative of "no vehicle", not of a real empty-space photo.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(REPO_DIR))

from device import pi_client  # noqa: E402  (load_jpeg, send_photo, event-mode runner)

DEFAULT_SCENARIO = "empty:30,permitted:60,empty:20,violation:60,empty:30,permitted:30,empty:10"  # 240 min
IMAGES = {
    "permitted": BACKEND_DIR / "uploads" / "sample_a.jpg",
    "violation": BACKEND_DIR / "uploads" / "sample_b.jpg",
}
VIOLATION_RESULTS = ("violation", "violation_ongoing", "violation_pending")


# ----------------------------------------------------------------------------- scenario
def parse_scenario(text: str) -> list[tuple[str, int]]:
    """'empty:30,violation:60' -> [('empty', 30), ('violation', 60)] (minutes of simulated time)."""
    steps = []
    for token in text.split(","):
        kind, _, minutes = token.strip().partition(":")
        if kind not in ("empty", "permitted", "violation") or not minutes.isdigit():
            raise SystemExit(f"잘못된 시나리오 항목: {token!r} (예: empty:30,violation:60)")
        steps.append((kind, int(minutes)))
    return steps


class Scenario:
    """What is in the parking space at a given simulated time."""

    def __init__(self, steps: list[tuple[str, int]]):
        self.steps = steps
        self.total_minutes = sum(m for _, m in steps)

    def kind_at(self, sim_seconds: float) -> str:
        elapsed = 0.0
        for kind, minutes in self.steps:
            elapsed += minutes * 60
            if sim_seconds < elapsed:
                return kind
        return self.steps[-1][0]

    def arrivals(self) -> list[tuple[str, int]]:
        """[(kind, sim_seconds)] for every step where a car arrives."""
        out, elapsed = [], 0
        for kind, minutes in self.steps:
            if kind != "empty":
                out.append((kind, elapsed))
            elapsed += minutes * 60
        return out


def synthesize_empty_image(source: Path, width: int) -> bytes:
    """A heavily blurred copy of a sample photo: same size and JPEG cost, no windshield to find."""
    import cv2

    image = cv2.imread(str(source))
    if image is None:
        raise SystemExit(f"사진을 읽을 수 없습니다: {source}")
    height, w = image.shape[:2]
    if w > width:
        image = cv2.resize(image, (width, int(height * width / w)), interpolation=cv2.INTER_AREA)
    for _ in range(3):
        image = cv2.GaussianBlur(image, (0, 0), 25)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise SystemExit("빈 자리 사진 합성에 실패했습니다.")
    return encoded.tobytes()


# ----------------------------------------------------------------------------- server probe
class ServerProbe(threading.Thread):
    """Samples CPU time and RSS of the server process once a second."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        import psutil

        self.process = psutil.Process(pid)
        self.stop_event = threading.Event()
        self.cpu_start = None
        self.cpu_end = None
        self.rss_samples: list[int] = []

    def run(self) -> None:
        times = self.process.cpu_times()
        self.cpu_start = times.user + times.system
        while not self.stop_event.is_set():
            self.rss_samples.append(self.process.memory_info().rss)
            times = self.process.cpu_times()
            self.cpu_end = times.user + times.system
            self.stop_event.wait(1.0)

    def summary(self) -> dict:
        if not self.rss_samples:
            return {}
        return {
            "cpu_seconds": round((self.cpu_end or 0) - (self.cpu_start or 0), 2),
            "rss_mb_min": round(min(self.rss_samples) / 1024 / 1024),
            "rss_mb_max": round(max(self.rss_samples) / 1024 / 1024),
        }


# ----------------------------------------------------------------------------- clock
class SimClock:
    """Simulated time that runs `speed` times faster than the wall clock."""

    def __init__(self, speed: float):
        self.speed = speed
        self.real_start = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self.real_start) * self.speed

    def sleep_until(self, sim_seconds: float) -> None:
        remaining = (sim_seconds - self.now()) / self.speed
        if remaining > 0:
            time.sleep(remaining)


# ----------------------------------------------------------------------------- recording
class Recorder:
    def __init__(self):
        self.requests: list[dict] = []
        self.heartbeats = 0
        self.heartbeat_bytes = 0

    def photo(self, sim_seconds: float, kind: str, event: str | None, jpeg: bytes, body: dict | None,
              latency_s: float, real_at: float) -> None:
        self.requests.append({
            "sim_minute": round(sim_seconds / 60, 2),
            "kind": kind,
            "event": event,
            "bytes": len(jpeg),
            "ok": body is not None,
            "result": (body or {}).get("result"),
            "violation_id": (body or {}).get("violation_id"),
            "episode_ended": bool((body or {}).get("episode_ended")),
            "inference_ms": ((body or {}).get("inference") or {}).get("elapsed_ms"),
            "latency_ms": round(latency_s * 1000),
            "real_at": real_at,
        })

    def heartbeat(self, ok: bool) -> None:
        if ok:
            self.heartbeats += 1
            self.heartbeat_bytes += 200  # headers + empty body, rough


def post_photo(url: str, key: str, jpeg: bytes, zone: str | None, event: str | None, timeout: float = 180):
    """Same request as device/pi_client.py; returns (body or None, latency seconds)."""
    started = time.perf_counter()
    body = pi_client.send_photo(url, key, jpeg, zone, event=event, timeout=(5, timeout))
    return body, time.perf_counter() - started


def post_heartbeat(url: str, key: str) -> bool:
    return pi_client.send_heartbeat(url, key)


# ----------------------------------------------------------------------------- device modes
def run_interval(args, scenario: Scenario, images: dict, clock: SimClock, rec: Recorder) -> None:
    """Today's device: one photo per simulated minute, whatever is in the space."""
    minute = 0
    while minute * 60 < scenario.total_minutes * 60:
        sim = minute * 60
        clock.sleep_until(sim)
        kind = scenario.kind_at(sim)
        body, latency = post_photo(args.url, args.key, images[kind], args.zone, None)
        rec.photo(sim, kind, None, images[kind], body, latency, time.time())
        print(f"  [{sim / 60:6.1f} min] {kind:<9} -> {(body or {}).get('result')}  {round(latency * 1000)} ms")
        minute += 1


class ScenarioDetector:
    """Stands in for VehicleDetector in event mode: occupancy comes from the scenario, with the same
    consecutive-frame rule the real device applies (OccupancyTracker)."""

    def __init__(self, scenario: Scenario, clock: SimClock, config):
        from device.vehicle_detector import OccupancyTracker

        self.scenario = scenario
        self.clock = clock
        self.tracker = OccupancyTracker(config)
        self.last_frame_ms = 0

    def process_frame(self, frame=None):
        occupied = self.scenario.kind_at(self.clock.now()) != "empty"
        self.tracker.update(occupied)
        return occupied


def run_event(args, scenario: Scenario, images: dict, clock: SimClock, rec: Recorder) -> None:
    """The event-mode device from device/pi_client.py with the scenario as its camera and detector."""
    from device.vehicle_detector import OccupancyConfig

    settings = pi_client.EventSettings(
        detect_interval=args.detect_interval, heartbeat_interval=60, refresh_interval=args.refresh_interval,
        confirm_delay=60)
    detector = ScenarioDetector(scenario, clock, OccupancyConfig())

    class ScenarioCamera:
        def capture_frame(self):
            return None

        def capture(self) -> bytes:
            return images[scenario.kind_at(clock.now())]

        def set_led(self, on: bool) -> None:
            pass

    def photo_sender(jpeg: bytes, event: str):
        sim = clock.now()
        kind = scenario.kind_at(sim)
        body, latency = post_photo(args.url, args.key, jpeg, args.zone, event)
        rec.photo(sim, kind, event, jpeg, body, latency, time.time())
        print(f"  [{sim / 60:6.1f} min] {kind:<9} {event:<8} -> {(body or {}).get('result')}  {round(latency * 1000)} ms")
        return body

    def heartbeat_sender():
        ok = post_heartbeat(args.url, args.key)
        rec.heartbeat(ok)
        return ok

    runner = pi_client.EventModeRunner(ScenarioCamera(), detector, settings, photo_sender, heartbeat_sender,
                                       clock=clock.now, sleep=lambda s: clock.sleep_until(clock.now() + s))
    end = scenario.total_minutes * 60
    while clock.now() < end:
        runner.step()


# ----------------------------------------------------------------------------- admin side
def fetch_notifications(url: str, email: str, password: str) -> list[dict]:
    session = requests.Session()
    login = session.post(f"{url}/auth/login", json={"email": email, "password": password}, timeout=30)
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    items, page = [], 1
    while True:
        response = session.get(f"{url}/notifications", headers=headers, params={"page": page, "size": 100}, timeout=30)
        response.raise_for_status()
        body = response.json()
        items.extend(body["items"])
        if not body["items"] or len(items) >= body["total"]:
            return items
        page += 1


def fetch_violations(url: str, email: str, password: str) -> list[dict]:
    session = requests.Session()
    login = session.post(f"{url}/auth/login", json={"email": email, "password": password}, timeout=30)
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = session.get(f"{url}/violations", headers=headers, params={"size": 100}, timeout=30)
    response.raise_for_status()
    return response.json()["items"]


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ----------------------------------------------------------------------------- report
def summarize(args, scenario: Scenario, rec: Recorder, probe: ServerProbe | None, run_real_seconds: float,
              violations: list[dict], notifications: list[dict], started_at: datetime) -> dict:
    photos = rec.requests
    ok = [p for p in photos if p["ok"]]
    inference = [p["inference_ms"] for p in ok if p["inference_ms"] is not None]
    latency = [p["latency_ms"] for p in ok]
    total_bytes = sum(p["bytes"] for p in photos)
    hours = scenario.total_minutes / 60
    per_day = 24 / hours

    new_violations = [v for v in violations if parse_iso(v["created_at"]) >= started_at]
    new_notifications = [n for n in notifications if parse_iso(n["created_at"]) >= started_at]

    # Notification delay: from the simulated arrival of a violating car to the notification row.
    delays = []
    arrivals = [a for a in scenario.arrivals() if a[0] == "violation"]
    for (_, arrival_sim), violation in zip(arrivals, sorted(new_violations, key=lambda v: v["created_at"])):
        notes = [n for n in new_notifications if n.get("violation_id") == violation["id"]]
        if not notes:
            continue
        created_real = parse_iso(min(n["created_at"] for n in notes)).timestamp()
        arrival_real = started_at.timestamp() + arrival_sim / args.speed
        real_delay = created_real - arrival_real
        delays.append({"violation_id": violation["id"], "real_seconds": round(real_delay, 1),
                       "simulated_minutes": round(real_delay * args.speed / 60, 1)})

    return {
        "mode": args.mode,
        "scenario": args.scenario,
        "scenario_hours": hours,
        "speed": args.speed,
        "run_real_seconds": round(run_real_seconds),
        "started_at": started_at.isoformat(),
        "photos": {
            "sent": len(photos),
            "ok": len(ok),
            "by_event": {e: sum(1 for p in photos if p["event"] == e) for e in sorted({p["event"] for p in photos}, key=str)},
            "bytes": total_bytes,
            "mb": round(total_bytes / 1024 / 1024, 1),
        },
        "heartbeats": rec.heartbeats,
        "inference_ms": {
            "count": len(inference),
            "mean": round(statistics.mean(inference)) if inference else None,
            "median": round(statistics.median(inference)) if inference else None,
            "p95": round(sorted(inference)[int(len(inference) * 0.95) - 1]) if len(inference) >= 20 else None,
            "total_seconds": round(sum(inference) / 1000, 1),
        },
        "latency_ms": {
            "mean": round(statistics.mean(latency)) if latency else None,
            "max": max(latency) if latency else None,
        },
        "server_process": probe.summary() if probe else {},
        "violations_created": len(new_violations),
        "violation_end_reasons": [v["end_reason"] for v in new_violations],
        "notifications_created": len(new_notifications),
        "notification_delay": delays,
        "per_day_estimate": {
            "note": f"scenario x {per_day:.1f} (24 h / {hours:g} h). An estimate, not a measurement.",
            "photos": round(len(photos) * per_day),
            "mb": round(total_bytes / 1024 / 1024 * per_day, 1),
            "inference_minutes": round(sum(inference) / 1000 / 60 * per_day, 1) if inference else None,
            "heartbeats": round(rec.heartbeats * per_day),
        },
        "requests": photos,
    }


def print_summary(s: dict) -> None:
    print()
    print(f"=== {s['mode']} | {s['scenario_hours']:g} h scenario at {s['speed']}x ({s['run_real_seconds']} s real) ===")
    print(f"photos sent      {s['photos']['sent']} ({s['photos']['mb']} MB)  by event: {s['photos']['by_event']}")
    print(f"heartbeats       {s['heartbeats']}")
    inf = s["inference_ms"]
    print(f"inference        n={inf['count']} mean={inf['mean']} ms median={inf['median']} ms p95={inf['p95']} ms total={inf['total_seconds']} s")
    print(f"latency          mean={s['latency_ms']['mean']} ms max={s['latency_ms']['max']} ms")
    print(f"server process   {s['server_process']}")
    print(f"violations       {s['violations_created']} {s['violation_end_reasons']}   notifications {s['notifications_created']}")
    print(f"notify delay     {s['notification_delay']}")
    print(f"per day (est.)   {s['per_day_estimate']}")


# ----------------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("interval", "event"), required=True)
    parser.add_argument("--url", default="http://127.0.0.1:5001/api/v1")
    parser.add_argument("--serial", default="RPI-A-GANGNAM-01", help="seeded demo machine")
    parser.add_argument("--key", help="device key (overrides --serial)")
    parser.add_argument("--zone")
    parser.add_argument("--admin", default="kim@a.com", help="admin assigned to the machine's lot")
    parser.add_argument("--password", default="Passw0rd!")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--speed", type=float, default=30.0, help="simulated seconds per real second")
    parser.add_argument("--send-width", type=int, default=1280, help="like PG_SEND_WIDTH on the device")
    parser.add_argument("--detect-interval", type=float, default=5.0, help="event mode: seconds between frames")
    parser.add_argument("--refresh-interval", type=float, default=600.0, help="event mode: seconds between refresh photos")
    parser.add_argument("--server-pid", type=int, help="backend process to sample CPU and memory from")
    parser.add_argument("--out", help="write the full result as JSON here")
    parser.add_argument("--no-warmup", action="store_true", help="skip the model-loading request")
    args = parser.parse_args()
    args.key = args.key or f"pgk_dev_{args.serial.lower()}"

    scenario = Scenario(parse_scenario(args.scenario))
    images = {kind: pi_client.load_jpeg(str(path), args.send_width) for kind, path in IMAGES.items()}
    images["empty"] = synthesize_empty_image(IMAGES["permitted"], args.send_width)
    print(f"images: " + ", ".join(f"{k} {len(v) // 1024} KB" for k, v in images.items()))

    if not args.no_warmup:
        print("warm-up (loads the models; not counted) ...", end=" ", flush=True)
        started = time.perf_counter()
        body, _ = post_photo(args.url, args.key, images["empty"], args.zone, None, timeout=300)
        if body is None:
            print("\n서버가 응답하지 않습니다. --url 과 서버 상태를 확인하세요.")
            return 1
        print(f"{round(time.perf_counter() - started, 1)} s, result={body.get('result')}")
        if body.get("result") != "no_vehicle":
            print("합성한 빈 자리 사진이 no_vehicle 로 판정되지 않았습니다. 측정을 중단합니다.")
            return 1

    probe = None
    if args.server_pid:
        probe = ServerProbe(args.server_pid)
        probe.start()

    started_at = datetime.now(timezone.utc)
    clock = SimClock(args.speed)
    rec = Recorder()
    print(f"scenario {args.scenario} ({scenario.total_minutes} min) at {args.speed}x, mode={args.mode}")
    try:
        if args.mode == "interval":
            run_interval(args, scenario, images, clock, rec)
        else:
            run_event(args, scenario, images, clock, rec)
    except KeyboardInterrupt:
        print("중단됨. 지금까지의 결과만 정리합니다.")
    run_real = time.monotonic() - clock.real_start
    if probe:
        probe.stop_event.set()
        probe.join(timeout=3)

    violations = fetch_violations(args.url, args.admin, args.password)
    notifications = fetch_notifications(args.url, args.admin, args.password)
    summary = summarize(args, scenario, rec, probe, run_real, violations, notifications, started_at)
    print_summary(summary)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
