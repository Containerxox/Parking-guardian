"""Check the inference service with the REAL YOLO models (design doc 9.6 steps 2-6 and 3-3).

    python scripts/check_inference_service.py                          local process (Windows or Linux)
    python scripts/check_inference_service.py --image parking-guardian-inference   Docker image, real gunicorn

Local mode starts `python wsgi_inference.py` as a separate process (gunicorn does not run on Windows). Image mode
starts the image's own command (gunicorn_inference.conf.py) as a container, with backend/models mounted at
/models and backend/uploads at /samples, and reads memory, threads and CPU time from /proc inside it.
Either way it prints markdown:

  1. start-up: time until /readyz is 200, memory (RSS) idle and after photos, CPU use per photo
  2. four photos at once: one runs, two wait, the fourth is refused with 503 busy
  3. judgements over HTTP == the same photos judged in-process (the Prototype path, torch default threads)
  4. TORCH_NUM_THREADS 1 / 2 / 4: time per photo and CPU use
  5. a model that cannot load: /readyz stays 503, /healthz turns 503 only after the retries are used up
  6. (image mode) OMP_NUM_THREADS is set before torch is imported

Nothing here touches the database or AWS. It needs backend/models/*.pt and, for local mode, ../yolov5.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
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
CONTAINER = "pg-inference-check"
IMAGE = None  # set from --image

PROC_SNIPPET = """
import json, os
best = None
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        status = open(f"/proc/{pid}/status").read()
        stat = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    except OSError:
        continue
    fields = {line.split(":")[0]: line.split(":")[1].split() for line in status.splitlines() if ":" in line}
    rss = int(fields["VmRSS"][0]) if "VmRSS" in fields else 0
    if best is None or rss > best["rss_kb"]:
        best = {"pid": int(pid), "rss_kb": rss, "peak_kb": int(fields["VmHWM"][0]) if "VmHWM" in fields else 0,
                "threads": int(fields["Threads"][0]),
                "cpu_seconds": (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")}
print(json.dumps(best))
"""

IN_PROCESS_SNIPPET = """
import json, time
from pathlib import Path
import torch
from app.adapters.inference import YoloInference
from app.config import Config
model = YoloInference(Config.YOLOV5_DIR, Config.WINDOW_MODEL_PATH, Config.STICKER_MODEL_PATH,
                      Config.WINDOW_CLASS_NAME, Config.WINDOW_CONF, Config.STICKER_CONF, Config.INFERENCE_IMAGE_SIZE)
photos = sorted(Path("/samples").glob("sample_[a-f].jpg"))
model.analyze(photos[0].read_bytes(), "warm-up")
results, seconds = {}, []
for photo in photos:
    started = time.perf_counter()
    results[photo.name] = model.analyze(photo.read_bytes(), photo.name).to_dict()
    seconds.append(time.perf_counter() - started)
print(json.dumps({"results": results, "seconds": seconds, "threads": torch.get_num_threads()}))
"""


class Handle:
    """A running service: a local process (psutil) or a container (/proc through docker exec)."""

    def __init__(self, process=None):
        self.process = process
        self._proc = None

    def stats(self) -> dict:
        if IMAGE:
            return json.loads(docker_python(PROC_SNIPPET).strip().splitlines()[-1])
        if self._proc is None:
            parent = psutil.Process(self.process.pid)  # the venv python.exe is a launcher; the biggest child has the models
            self._proc = max([parent] + parent.children(recursive=True), key=lambda p: p.memory_info().rss)
        info = self._proc.memory_info()
        return {"rss_kb": info.rss // 1024, "peak_kb": getattr(info, "peak_wset", 0) // 1024,
                "threads": self._proc.num_threads(), "cpu_seconds": sum(self._proc.cpu_times()[:2])}

    def stop(self):
        if IMAGE:
            subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
            return
        try:
            family = [psutil.Process(self.process.pid)] + psutil.Process(self.process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for p in family:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        self.process.wait(15)


def docker_python(code: str, env=None) -> str:
    args = ["docker", "exec", "-i"] + [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    done = subprocess.run(args + [CONTAINER, "python", "-"], input=code, capture_output=True, encoding="utf-8", errors="replace",
                          timeout=300)
    if done.returncode:
        raise SystemExit(f"docker exec failed: {done.stderr[-800:]}")
    return done.stdout


def start_service(**env) -> Handle:
    if IMAGE:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        args = ["docker", "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:8000",
                "-p", f"127.0.0.1:{METRICS_PORT}:9102", "-v", f"{BACKEND / 'models'}:/models:ro",
                "-v", f"{BACKEND / 'uploads'}:/samples:ro", "-e", "METRICS_PORT=9102"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        done = subprocess.run(args + [IMAGE], capture_output=True, encoding="utf-8", errors="replace")
        if done.returncode:
            raise SystemExit(done.stderr)
        return Handle()
    environment = {**os.environ, "PORT": str(PORT), "METRICS_PORT": str(METRICS_PORT), "INFERENCE_BACKEND": "yolo",
                   "PYTHONIOENCODING": "utf-8", **{k: str(v) for k, v in env.items()}}
    process = subprocess.Popen([sys.executable, "wsgi_inference.py"], cwd=BACKEND, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return Handle(process)


def get(path):
    try:
        return requests.get(URL + path, timeout=2)
    except requests.RequestException:
        return None


def wait_ready(timeout=300):
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        response = get("/readyz")
        if response is not None and response.status_code == 200:
            return time.perf_counter() - started
        time.sleep(0.25)
    raise SystemExit("service never became ready")


def analyze(path, name=None):
    started = time.perf_counter()
    response = requests.post(URL + "/v1/analyze", data=Path(path).read_bytes(), timeout=120,
                             headers={"X-Filename": name or Path(path).name, "Content-Type": "application/octet-stream"})
    return response, time.perf_counter() - started


def mib(kilobytes):
    return f"{kilobytes / 1024:.0f} MiB"


def section_startup():
    print("## 1. Start-up, memory, threads\n")
    service = start_service()
    try:
        ready_after = wait_ready()
        idle = service.stats()
        print(f"- /readyz 200 after **{ready_after:.1f} s** (process or container start included)")
        print(f"- RSS right after loading: **{mib(idle['rss_kb'])}**")
        print(f"- cores seen by this host: {psutil.cpu_count(logical=False)} physical / {psutil.cpu_count()} logical\n")

        rows, results, cpu_ratios = [], {}, []
        for round_no in (1, 2):
            for sample in SAMPLES:
                before = service.stats()["cpu_seconds"]
                response, seconds = analyze(sample)
                used = service.stats()["cpu_seconds"] - before
                body = response.json()
                if round_no == 1:
                    results[sample.name] = body
                    rows.append((sample.name, response.status_code, body.get("verdict"), body.get("sticker_count"),
                                 body.get("elapsed_ms"), seconds))
                else:
                    cpu_ratios.append(used / seconds)
        print("| photo | status | verdict | stickers | service ms | round trip s |\n|---|---|---|---|---|---|")
        for name, status, verdict, stickers, ms, seconds in rows:
            print(f"| {name} | {status} | {verdict} | {stickers} | {ms} | {seconds:.2f} |")
        print(f"\n- first photo {rows[0][5]:.2f} s, the other five median {statistics.median(r[5] for r in rows[1:]):.2f} s")
        print(f"- service CPU time / wall time per photo (second round): median **{statistics.median(cpu_ratios):.2f}** "
              "(about 1.0 means one thread did the work)")
        busy = service.stats()
        print(f"- RSS after 12 photos: **{mib(busy['rss_kb'])}** (peak {mib(busy['peak_kb'])})")
        print(f"- threads in the server process: {busy['threads']}\n")
        metrics = requests.get(f"http://127.0.0.1:{METRICS_PORT}/metrics", timeout=5).text
        loaded = [line for line in metrics.splitlines() if line.startswith("pg_model_load_seconds ")]
        print(f"- metrics port answers: {'yes' if 'pg_inference_waiting' in metrics else 'NO'}; {loaded[0] if loaded else ''}\n")
        section_concurrency()
        return results
    finally:
        service.stop()


def section_concurrency():
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


def in_process():
    """The same photos judged without the service, with torch default threads: returns (results, seconds, threads)."""
    if IMAGE:
        # exec'd into the running service container: same image, same torch, but no OMP_NUM_THREADS set
        out = docker_python(IN_PROCESS_SNIPPET)
        data = json.loads([line for line in out.splitlines() if line.startswith("{")][-1])
        return data["results"], data["seconds"], data["threads"]
    import torch
    from app.adapters.inference import YoloInference
    from app.config import Config

    model = YoloInference(Config.YOLOV5_DIR, Config.WINDOW_MODEL_PATH, Config.STICKER_MODEL_PATH,
                          Config.WINDOW_CLASS_NAME, Config.WINDOW_CONF, Config.STICKER_CONF, Config.INFERENCE_IMAGE_SIZE)
    model.analyze(SAMPLES[0].read_bytes(), "warm-up")
    results, seconds = {}, []
    for sample in SAMPLES:
        started = time.perf_counter()
        results[sample.name] = model.analyze(sample.read_bytes(), sample.name).to_dict()
        seconds.append(time.perf_counter() - started)
    return results, seconds, torch.get_num_threads()


def section_in_process(over_http):
    print("## 3. Same photos, judged in-process (the Prototype path)\n")
    local, seconds, threads = in_process()
    differences = 0
    print("| photo | over HTTP | in-process | window box equal | sticker boxes equal |\n|---|---|---|---|---|")
    for sample in SAMPLES:
        mine, remote = local[sample.name], over_http[sample.name]
        same_window, same_stickers = mine["window"] == remote["window"], mine["stickers"] == remote["stickers"]
        differences += mine["verdict"] != remote["verdict"] or not same_window or not same_stickers
        print(f"| {sample.name} | {remote['verdict']} | {mine['verdict']} | {same_window} | {same_stickers} |")
    print(f"\n- photos whose verdict or boxes differ: **{differences}** of {len(SAMPLES)}")
    print(f"- in-process time per photo (torch default threads = {threads}): median **{statistics.median(seconds):.2f} s**\n")


def section_threads():
    print("## 4. TORCH_NUM_THREADS takes effect in the service (warm photos, median of 6)\n")
    print("| TORCH_NUM_THREADS | median s per photo | service CPU time / wall time |\n|---|---|---|")
    for threads in (1, 2, 4):
        service = start_service(TORCH_NUM_THREADS=threads)
        try:
            wait_ready()
            analyze(SAMPLES[0])  # the first photo pays one-time costs
            times, ratios = [], []
            for sample in SAMPLES:
                before = service.stats()["cpu_seconds"]
                _, seconds = analyze(sample)
                times.append(seconds)
                ratios.append((service.stats()["cpu_seconds"] - before) / seconds)
            print(f"| {threads} | {statistics.median(times):.2f} | {statistics.median(ratios):.2f} |")
        finally:
            service.stop()
    print()


def section_failed_model():
    print("## 5. A model that cannot load (3 attempts, 1 s base wait)\n")
    missing = "/models/does-not-exist.pt" if IMAGE else str(BACKEND / "models" / "does-not-exist.pt")
    service = start_service(WINDOW_MODEL_PATH=missing, MODEL_LOAD_ATTEMPTS=3, MODEL_LOAD_RETRY_SECONDS=1)
    try:
        timeline, started, last = [], time.perf_counter(), None
        while time.perf_counter() - started < 120:
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
        service.stop()


def section_openmp():
    """Image mode: is OMP_NUM_THREADS set before torch is imported? (a fresh interpreter, same import order)"""
    print("## 6. OMP_NUM_THREADS is applied before torch is imported\n")
    check = ("import json, os\nimport wsgi_inference\nimport torch\n"
             "print(json.dumps({'OMP_NUM_THREADS': os.environ.get('OMP_NUM_THREADS'), "
             "'MKL_NUM_THREADS': os.environ.get('MKL_NUM_THREADS'), 'torch_threads': torch.get_num_threads()}))\n")
    control = "import json, torch\nprint(json.dumps({'torch_threads': torch.get_num_threads()}))\n"
    service = start_service()
    try:
        wait_ready()
        print("| how torch is imported | result |\n|---|---|")
        for label, code, env in (("after wsgi_inference (default)", check, None),
                                 ("after wsgi_inference, TORCH_NUM_THREADS=2", check, {"TORCH_NUM_THREADS": "2"}),
                                 ("control: plain `import torch`, no env", control, None)):
            out = docker_python(code, env=env)
            print(f"| {label} | `{out.strip().splitlines()[-1]}` |")
        print()
    finally:
        service.stop()


def docker_versions() -> str:
    service = start_service()
    try:
        wait_ready()
        out = docker_python("import sys, torch, flask, gunicorn; print(f'python {sys.version.split()[0]}, "
                            "torch {torch.__version__}, flask {flask.__version__}, gunicorn {gunicorn.__version__}')")
        return out.strip().splitlines()[-1]
    finally:
        service.stop()


def main():
    global IMAGE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", help="run this inference image as a container instead of a local process")
    IMAGE = parser.parse_args().image
    if not SAMPLES:
        raise SystemExit("no backend/uploads/sample_*.jpg")
    mode = f"image `{IMAGE}`" if IMAGE else "local process"
    print(f"# Inference service check with real models ({time.strftime('%Y-%m-%d')}, {mode})\n")
    if IMAGE:
        print(f"- {docker_versions()}\n")
    judged = section_startup()
    if IMAGE:
        service = start_service()  # section 3 execs into a running container
        try:
            wait_ready()
            section_in_process(judged)
        finally:
            service.stop()
    else:
        section_in_process(judged)
    section_threads()
    section_failed_model()
    if IMAGE:
        section_openmp()


if __name__ == "__main__":
    main()
