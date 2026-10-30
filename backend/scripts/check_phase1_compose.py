"""Phase 1 integration drills on Docker Compose (design doc 9.6 steps 3-2 to 3-4).

    # once: a database of its own, so the development data is never touched
    docker compose up -d db
    docker exec parking-guardian-db-1 psql -U parking -d parking_guardian -c "create database parking_guardian_phase1"
    docker compose -f docker-compose.yml -f docker-compose.phase1.yml build
    PHASE1_DB_NAME=parking_guardian_phase1 python backend/scripts/check_phase1_compose.py

It starts, stops, restarts and re-creates the compose services itself (db, backend = the `api` image, inference)
and prints markdown. It refuses to run against the development database name.

  A. the API starts and serves admins while the inference service is not there at all
  B. the inference service starts: detections go from 503 to 200 without touching the API
  C. real photos through API -> inference: verdicts, violation created after two sightings
  D. inference stopped, then started again
  E. inference restarted while detections and admin requests keep coming
  F. every photo delayed beyond the API deadline (INFERENCE_FAULT_DELAY_MS)
  G. 4 and 8 photos at once through the API: the admission limit and the bulkhead, with admin latency alongside
"""
import io
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.cli import dev_api_key  # noqa: E402

DB_NAME = os.environ.get("PHASE1_DB_NAME", "parking_guardian_phase1")
if DB_NAME == "parking_guardian":
    raise SystemExit("refusing to run against the development database; set PHASE1_DB_NAME to another name")

API = "http://127.0.0.1:5000/api/v1"
AI = "http://127.0.0.1:8000"
SERIAL = "RPI-A-GANGNAM-01"
SAMPLES = {p.name: p for p in sorted((BACKEND / "uploads").glob("sample_[a-f].jpg"))}
# What the in-process (Prototype) path answered for the same photos on 2026-10-30 (backend/scripts/check_inference_service.py)
EXPECTED = {"sample_a.jpg": "permitted", "sample_b.jpg": "violation", "sample_c.jpg": "permitted",
            "sample_d.jpg": "violation", "sample_e.jpg": "permitted", "sample_f.jpg": "no_vehicle"}
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.phase1.yml")]


def compose(*args, **env):
    done = subprocess.run(COMPOSE + list(args), capture_output=True, encoding="utf-8", errors="replace", cwd=ROOT,
                          env={**os.environ, "PHASE1_DB_NAME": DB_NAME, **{k: str(v) for k, v in env.items()}})
    if done.returncode:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{done.stderr[-1500:]}")
    return done.stdout


def exec_python(service, code):
    done = subprocess.run(COMPOSE + ["exec", "-T", service, "python", "-"], input=code, capture_output=True, encoding="utf-8",
                          errors="replace", cwd=ROOT, env={**os.environ, "PHASE1_DB_NAME": DB_NAME})
    return done.stdout


def get(url, timeout=3):
    try:
        return requests.get(url, timeout=timeout)
    except requests.RequestException:
        return None


def wait_for(url, status=200, timeout=240):
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        response = get(url)
        if response is not None and response.status_code == status:
            return time.perf_counter() - started
        time.sleep(0.5)
    raise SystemExit(f"{url} never answered {status}")


def login(email="kim@a.com"):
    response = requests.post(f"{API}/auth/login", json={"email": email, "password": "Passw0rd!"}, timeout=10)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def admin_ok(headers):
    """(status of the violation list, seconds)"""
    started = time.perf_counter()
    response = requests.get(f"{API}/violations", headers=headers, timeout=15)
    return response.status_code, time.perf_counter() - started


def detect(sample, **form):
    started = time.perf_counter()
    try:
        response = requests.post(f"{API}/device/detections", headers={"X-Device-Key": dev_api_key(SERIAL)},
                                 files={"file": (sample, io.BytesIO(SAMPLES[sample].read_bytes()), "image/jpeg")},
                                 data=form, timeout=60)
    except requests.RequestException as exc:
        return None, {"error": type(exc).__name__}, time.perf_counter() - started
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body, time.perf_counter() - started


def code_of(body):
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else error


def metric_lines(service, port, prefixes):
    code = (f"import urllib.request\nfor line in urllib.request.urlopen('http://127.0.0.1:{port}/metrics').read()"
            f".decode().splitlines():\n    print(line)\n")
    lines = exec_python(service, code).splitlines()
    return [line for line in lines if any(line.startswith(p) for p in prefixes) and not line.endswith(" 0.0")]


def section_a_api_without_inference():
    print("## A. The API without any inference service\n")
    compose("up", "-d", "db", "backend")
    ready_after = wait_for(f"{API}/readyz")
    body = requests.get(f"{API}/readyz", timeout=5).json()
    headers = login()
    status, seconds = admin_ok(headers)
    code, answer, took = detect("sample_f.jpg")
    print(f"- API `/readyz` 200 after {ready_after:.1f} s (db + migrations + seed included): `{json.dumps(body)}`")
    print(f"- admin login and violation list: {status} in {seconds:.2f} s")
    print(f"- a detection with the inference container not running: **{code} {code_of(answer)}** after {took:.2f} s\n")
    return headers


def section_b_inference_starts(headers):
    print("## B. The inference service starts while the API keeps serving\n")
    compose("up", "-d", "inference")
    started = time.perf_counter()
    timeline, last, api_codes, tries = [], None, set(), 0
    while time.perf_counter() - started < 300:
        ai_ready = get(f"{AI}/readyz")
        ai_state = ai_ready.status_code if ai_ready is not None else None
        api_codes.add(get(f"{API}/readyz").status_code)
        code, answer, took = detect("sample_f.jpg")
        tries += 1
        state = (ai_state, code, code_of(answer) if code != 200 else "ok")
        if state != last:
            timeline.append((time.perf_counter() - started, state, took))
            last = state
        if code == 200:
            break
        time.sleep(1.0)
    print("| seconds | inference /readyz | API detection | answer | detection took s |\n|---|---|---|---|---|")
    for seconds, (ai_state, code, answer), took in timeline:
        print(f"| {seconds:.1f} | {ai_state} | {code} | {answer} | {took:.2f} |")
    print(f"\n- API `/readyz` answered only {sorted(api_codes)} the whole time ({tries} checks)")
    print(f"- inference container health: `{compose('ps', '--format', '{{.Name}} {{.Health}}', 'inference').strip()}`\n")


def section_c_judgements():
    print("## C. Real photos through API -> inference\n")
    headers = login()
    print("| photo | API status | result | verdict | stickers | expected (Prototype) | match |\n|---|---|---|---|---|---|---|")
    mismatches = 0
    for name in SAMPLES:
        code, body, _ = detect(name)
        inference = body.get("inference") or {}
        match = inference.get("verdict") == EXPECTED[name]
        mismatches += not match
        print(f"| {name} | {code} | {body.get('result')} | {inference.get('verdict')} | {inference.get('sticker_count')} "
              f"| {EXPECTED[name]} | {match} |")
    # A violation needs two sightings IN A ROW; the table above interleaves permitted photos, which reset the count.
    first, second = detect("sample_b.jpg"), detect("sample_b.jpg")
    listing = requests.get(f"{API}/violations", headers=headers, timeout=10).json()
    count = len(listing["items"])
    print(f"\n- verdict mismatches against the in-process results: **{mismatches}** of {len(SAMPLES)}")
    print(f"- sample_b twice in a row: {first[0]} `{first[1].get('result')}`, then {second[0]} `{second[1].get('result')}` "
          f"(violation_id {second[1].get('violation_id')}); violations visible to the admin: {count}\n")


def section_d_stop_start():
    print("## D. Inference stopped, then started\n")
    headers = login()
    compose("stop", "inference")
    status, seconds = admin_ok(headers)
    ready = requests.get(f"{API}/readyz", timeout=5).status_code
    code, answer, took = detect("sample_f.jpg")
    print(f"- while stopped: admin list {status} in {seconds:.2f} s, API `/readyz` {ready}, "
          f"detection **{code} {code_of(answer)}** after {took:.2f} s")
    compose("start", "inference")
    started = time.perf_counter()
    first_503 = None
    while True:
        code, answer, took = detect("sample_f.jpg")
        if code == 200:
            break
        first_503 = first_503 or (code, code_of(answer), took)
        time.sleep(1.0)
    print(f"- right after `start`: detection {first_503[0]} {first_503[1]} after {first_503[2]:.2f} s")
    print(f"- first detection answered 200 **{time.perf_counter() - started:.1f} s** after `start` "
          "(model loading included)\n")


def section_e_restart_under_traffic():
    print("## E. Inference restarted while detections and admin requests keep coming\n")
    headers = login()
    events, stop = [], threading.Event()

    def traffic():
        while not stop.is_set():
            code, answer, took = detect("sample_f.jpg")
            events.append(("detection", time.perf_counter(), code, took))
            time.sleep(0.5)

    def admin():
        while not stop.is_set():
            status, seconds = admin_ok(headers)
            events.append(("admin", time.perf_counter(), status, seconds))
            time.sleep(0.5)

    threads = [threading.Thread(target=traffic), threading.Thread(target=admin)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(3)
    restart_at = time.perf_counter()
    compose("restart", "inference")
    while True:  # until detections work again
        recent = [e for e in events if e[0] == "detection" and e[1] > restart_at]
        if recent and recent[-1][2] == 200:
            break
        time.sleep(1)
    time.sleep(3)
    stop.set()
    for t in threads:
        t.join(30)
    detections = [e for e in events if e[0] == "detection"]
    admins = [e for e in events if e[0] == "admin"]
    codes = {}
    for e in detections:
        codes[e[2]] = codes.get(e[2], 0) + 1
    print(f"- detections during the run: {dict(sorted(codes.items(), key=lambda kv: str(kv[0])))}")
    print(f"- admin requests: {len(admins)}, all 200: {all(e[2] == 200 for e in admins)}, "
          f"slowest {max(e[3] for e in admins):.2f} s")
    gap = [e for e in detections if e[2] != 200]
    print(f"- detections were not served for {gap[-1][1] - gap[0][1]:.1f} s after the restart began "
          f"(restart command itself took {gap[0][1] - restart_at:.1f} s to get going)\n" if gap else "- no failed detection\n")


def section_f_delay():
    print("## F. Every photo delayed beyond the API deadline (INFERENCE_FAULT_DELAY_MS=15000, deadline 10 s)\n")
    headers = login()
    compose("up", "-d", "inference", INFERENCE_FAULT_DELAY_MS=15000)
    wait_for(f"{AI}/readyz")
    result = {}

    def photo():
        result["detection"] = detect("sample_f.jpg")

    thread = threading.Thread(target=photo)
    started = time.perf_counter()
    thread.start()
    admin_timings = []
    while thread.is_alive():
        time.sleep(1)
        admin_timings.append(admin_ok(headers))
    thread.join()
    code, answer, took = result["detection"]
    print(f"- detection: **{code} {code_of(answer)}** after {took:.2f} s (deadline 10 s)")
    print(f"- admin requests while it was waiting: {len(admin_timings)}, statuses "
          f"{sorted({s for s, _ in admin_timings})}, slowest {max(t for _, t in admin_timings):.2f} s")
    lines = metric_lines("backend", 9102, ["pg_ai_client_requests_total", "pg_ai_client_retries_total"])
    print("- API client metrics: " + "; ".join(l.replace("pg_ai_client_", "") for l in lines if "timeout" in l or "retries" in l))
    compose("up", "-d", "inference", INFERENCE_FAULT_DELAY_MS=0)  # back to normal
    wait_for(f"{AI}/readyz")
    print("- delay removed, inference ready again\n")


def burst(count, sample="sample_a.jpg"):
    headers = login()
    results, admin_results = [None] * count, []
    stop = threading.Event()

    def one(i):
        results[i] = detect(sample)

    def admin():
        while not stop.is_set():
            admin_results.append(admin_ok(headers))
            time.sleep(0.3)

    watcher = threading.Thread(target=admin)
    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    watcher.start()
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    total = time.perf_counter() - started
    stop.set()
    watcher.join(10)
    return results, admin_results, total


def section_g_bursts():
    print("## G. Photos at once through the API (API threads 8, AI_CLIENT_MAX_CONCURRENCY 4, INFERENCE_MAX_WAITING 2)\n")
    for count in (4, 8):
        results, admin_results, total = burst(count)
        print(f"### {count} photos at once ({SAMPLES['sample_a.jpg'].name}, about 3.5 s each)\n")
        print("| API status | answer | seconds |\n|---|---|---|")
        for code, answer, took in sorted(results, key=lambda r: r[2]):
            print(f"| {code} | {code_of(answer) or answer.get('result')} | {took:.2f} |")
        ok = sum(1 for code, _, _ in results if code == 200)
        print(f"\n- judged {ok}, refused {count - ok}; whole burst {total:.1f} s")
        print(f"- admin list during the burst: {len(admin_results)} requests, statuses "
              f"{sorted({s for s, _ in admin_results})}, slowest {max(t for _, t in admin_results):.2f} s\n")
        time.sleep(15)  # let the queue drain
    print("API client metrics (non-zero):")
    for line in metric_lines("backend", 9102, ["pg_ai_client_requests_total", "pg_ai_client_retries_total",
                                               "pg_ai_client_duration_seconds_count"]):
        print(f"- `{line}`")
    print("\nInference metrics (non-zero):")
    for line in metric_lines("inference", 9102, ["pg_inference_rejected_total", "pg_inference_requests_total",
                                                 "pg_model_load_seconds"]):
        print(f"- `{line}`")
    print()


def main():
    print(f"# Phase 1 Compose drills ({time.strftime('%Y-%m-%d')}, database `{DB_NAME}`)\n")
    headers = section_a_api_without_inference()
    section_b_inference_starts(headers)
    section_c_judgements()
    section_d_stop_start()
    section_e_restart_under_traffic()
    section_f_delay()
    section_g_bursts()


if __name__ == "__main__":
    main()
