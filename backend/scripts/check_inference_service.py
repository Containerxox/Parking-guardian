"""Check the inference service with the REAL YOLO models (design doc 9.6 step 2-6).

    python scripts/check_inference_service.py            (from backend/, with the venv that has torch)

Starts `python wsgi_inference.py` as a separate process (gunicorn does not run on Windows; the Linux container
check is step 3) and prints what it saw as markdown:

  1. start-up: time until /readyz is 200, memory (RSS) idle and after photos, torch threads actually used
  2. four photos at once: one runs, two wait, the fourth is refused with 503 busy
  3. judgements over HTTP == the same photos judged in-process (the Prototype path)
  4. TORCH_NUM_THREADS 1 / 2 / 4: time per photo and CPU use
  5. a model that cannot load: /readyz stays 503, /healthz turns 503 only after the retries are used up

Nothing here touches the database or AWS. It needs backend/models/*.pt and ../yolov5.
"""
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
import requests

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

SAMPLES = sorted((BACKEND / "uploads").glob("sample_[a-f].jpg"))
PORT, METRICS_PORT = 8011, 9111
URL = f"http://127.0.0.1:{PORT}"  # 127.0.0.1, not localhost: localhost takes over a minute to connect on this PC


def start_service(**env):
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    environment = {**os.environ, "PORT": str(PORT), "METRICS_PORT": str(METRICS_PORT), "INFERENCE_BACKEND": "yolo",
                   "PYTHONIOENCODING": "utf-8", **{k: str(v) for k, v in env.items()}}
    process = subprocess.Popen([sys.executable, "wsgi_inference.py"], cwd=BACKEND, env=environment, stdout=log,
                               stderr=subprocess.STDOUT)
    return process, log.name


def stop_service(process):
    """The venv python.exe is only a launcher that starts the real interpreter as a child: end the whole tree."""
    try:
        family = [psutil.Process(process.pid)] + psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for p in family:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    process.wait(15)


def server_process(process):
    """The process that really holds the models (the biggest one in the launcher's tree)."""
    parent = psutil.Process(process.pid)
    return max([parent] + parent.children(recursive=True), key=lambda p: p.memory_info().rss)


def get(path):
    try:
        return requests.get(URL + path, timeout=2)
    except requests.RequestException:
        return None


def wait_ready(timeout=240):
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        response = get("/readyz")
        if response is not None and response.status_code == 200:
            return time.perf_counter() - started
        time.sleep(0.25)
    raise SystemExit("service never became ready")


def analyze(path, name=None):
    started = time.perf_counter()
    response = requests.post(URL + "/v1/analyze", data=Path(path).read_bytes(), timeout=60,
                             headers={"X-Filename": name or Path(path).name, "Content-Type": "application/octet-stream"})
    return response, time.perf_counter() - started


def mib(nbytes):
    return f"{nbytes / 1024 / 1024:.0f} MiB"


def section_startup_and_judgement():
    print("## 1. Start-up, memory, threads\n")
    process, log_path = start_service()
    try:
        ready_after = wait_ready()
        proc = server_process(process)
        idle_rss = proc.memory_info().rss
        print(f"- /readyz 200 after **{ready_after:.1f} s** (process start included)")
        print(f"- RSS right after loading: **{mib(idle_rss)}**")
        print(f"- cores seen: {psutil.cpu_count(logical=False)} physical / {psutil.cpu_count()} logical\n")

        rows, results, cpu_ratios = [], {}, []
        for round_no in (1, 2):
            for sample in SAMPLES:
                cpu_before = sum(proc.cpu_times()[:2])
                response, seconds = analyze(sample)
                cpu_used = sum(proc.cpu_times()[:2]) - cpu_before
                body = response.json()
                if round_no == 1:
                    results[sample.name] = body
                    rows.append((sample.name, response.status_code, body.get("verdict"), body.get("sticker_count"),
                                 body.get("elapsed_ms"), seconds))
                else:
                    cpu_ratios.append(cpu_used / seconds)
        print("| photo | status | verdict | stickers | service ms | round trip s |\n|---|---|---|---|---|---|")
        for name, status, verdict, stickers, ms, seconds in rows:
            print(f"| {name} | {status} | {verdict} | {stickers} | {ms} | {seconds:.2f} |")
        first, rest = rows[0][5], [r[5] for r in rows[1:]]
        print(f"\n- first photo {first:.2f} s, the other five median {statistics.median(rest):.2f} s")
        print(f"- service CPU time / wall time per photo (second round): median **{statistics.median(cpu_ratios):.2f}** "
              "(about 1.0 means one thread did the work)")
        busy_rss = proc.memory_info().rss
        print(f"- RSS after 12 photos: **{mib(busy_rss)}** (peak working set {mib(proc.memory_info().peak_wset)})")
        print(f"- threads in the server process: {proc.num_threads()}\n")
        metrics = requests.get(f"http://127.0.0.1:{METRICS_PORT}/metrics", timeout=5).text
        loaded = [line for line in metrics.splitlines() if line.startswith("pg_model_load_seconds ")]
        print(f"- metrics port answers: {'yes' if 'pg_inference_waiting' in metrics else 'NO'}; {loaded[0] if loaded else ''}\n")
        concurrency_section()
        return results
    finally:
        stop_service(process)


def concurrency_section():
    print("## 2. Four photos at once (INFERENCE_MAX_WAITING=2)\n")
    outcomes = [None] * 4

    def send(i):
        response, seconds = analyze(SAMPLES[0])
        outcomes[i] = (response.status_code, response.headers.get("Retry-After"), response.json().get("error"), seconds)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    print("| status | Retry-After | error | seconds |\n|---|---|---|---|")
    for status, retry, error, seconds in sorted(outcomes, key=lambda o: o[3]):
        print(f"| {status} | {retry or ''} | {error or ''} | {seconds:.2f} |")
    codes = sorted(o[0] for o in outcomes)
    print(f"\n- statuses {codes}: {'as designed (3 judged, 1 refused)' if codes == [200, 200, 200, 503] else 'NOT as designed'}\n")


def section_in_process(results):
    print("## 3. Same photos, judged in-process (the Prototype path)\n")
    from app.adapters.inference import YoloInference
    from app.config import Config

    model = YoloInference(Config.YOLOV5_DIR, Config.WINDOW_MODEL_PATH, Config.STICKER_MODEL_PATH,
                          Config.WINDOW_CLASS_NAME, Config.WINDOW_CONF, Config.STICKER_CONF, Config.INFERENCE_IMAGE_SIZE)
    differences, seconds_in_process = 0, []
    model.analyze(SAMPLES[0].read_bytes(), "warm-up")  # the models load on the first photo
    print("| photo | over HTTP | in-process | window box equal | sticker boxes equal |\n|---|---|---|---|---|")
    for sample in SAMPLES:
        started = time.perf_counter()
        local = model.analyze(sample.read_bytes(), sample.name).to_dict()
        seconds_in_process.append(time.perf_counter() - started)
        remote = results[sample.name]
        same_window = local["window"] == remote["window"]
        same_stickers = local["stickers"] == remote["stickers"]
        differences += local["verdict"] != remote["verdict"] or not same_window or not same_stickers
        print(f"| {sample.name} | {remote['verdict']} | {local['verdict']} | {same_window} | {same_stickers} |")
    print(f"\n- photos whose verdict or boxes differ: **{differences}** of {len(SAMPLES)}")
    print(f"- in-process time per photo (torch default threads = {model_threads()}): median "
          f"**{statistics.median(seconds_in_process):.2f} s**\n")


def model_threads():
    import torch

    return torch.get_num_threads()


def section_threads():
    print("## 4. TORCH_NUM_THREADS takes effect in the service (warm photos, median of 6)\n")
    print("| TORCH_NUM_THREADS | median s per photo | service CPU time / wall time |\n|---|---|---|")
    for threads in (1, 2, 4):
        process, _ = start_service(TORCH_NUM_THREADS=threads)
        try:
            wait_ready()
            proc = server_process(process)
            analyze(SAMPLES[0])  # the first photo pays one-time costs
            times, ratios = [], []
            for sample in SAMPLES:
                cpu_before = sum(proc.cpu_times()[:2])
                _, seconds = analyze(sample)
                times.append(seconds)
                ratios.append((sum(proc.cpu_times()[:2]) - cpu_before) / seconds)
            print(f"| {threads} | {statistics.median(times):.2f} | {statistics.median(ratios):.2f} |")
        finally:
            stop_service(process)
    print()


def section_failed_model():
    print("## 5. A model that cannot load (3 attempts, 1 s base wait)\n")
    process, log_path = start_service(WINDOW_MODEL_PATH=str(BACKEND / "models" / "does-not-exist.pt"),
                                      MODEL_LOAD_ATTEMPTS=3, MODEL_LOAD_RETRY_SECONDS=1)
    try:
        timeline, started, last = [], time.perf_counter(), None
        while time.perf_counter() - started < 90:
            health, ready = get("/healthz"), get("/readyz")
            state = (health.status_code if health is not None else None, ready.status_code if ready is not None else None)
            if state != last and None not in state:
                timeline.append((time.perf_counter() - started, state))
                last = state
            if state[0] == 503:
                break
            time.sleep(0.25)
        print("| seconds after start | /healthz | /readyz |\n|---|---|---|")
        for seconds, (health, ready) in timeline:
            print(f"| {seconds:.1f} | {health} | {ready} |")
        response, _ = analyze(SAMPLES[0])
        print(f"\n- a photo meanwhile: {response.status_code} {response.json()}")
        print(f"- /healthz body at the end: {get('/healthz').json()}\n")
    finally:
        stop_service(process)


if __name__ == "__main__":
    if not SAMPLES:
        raise SystemExit("no backend/uploads/sample_*.jpg")
    print(f"# Inference service check with real models ({time.strftime('%Y-%m-%d')})\n")
    judged = section_startup_and_judgement()
    section_in_process(judged)
    section_threads()
    section_failed_model()
