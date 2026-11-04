"""관찰자의 표본 수집 엔진 (읽기 전용). p2_watch.py 가 쓴다.

왜 다시 만들었나 (2026-11-04 스모크에서 확인): 설정은 15초인데 실제 표본 간격은 평균 48초(13개 표본, 최소 39 s, 최대 65 s)였다. 원인 = 한 표본이
외부 프로세스 9개를 **순차**로 불렀다. `aws.exe` 는 시작만 약 3 s(로컬 측정, 클라우드 접속 없음)라 AWS 호출 4개에 12 s 이상, 나머지 kubectl 호출·`kubectl exec`(앱을 import 해
DB 를 읽는 Python)와 합쳐 한 표본에 약 33 s. 그 결과 표본 시각은 "시작 시각"이고 값은 그 뒤 최대 30 s 사이에 읽혀 서로 어긋났다.

새 구조:
  * 소스마다 **독립 폴러 스레드**가 자기 주기로 돈다(느린 소스가 빠른 소스를 붙잡지 않는다). 각 호출은 자기 timeout 이 있고 시작·완료 시각·소요(ms)·성공 여부를 남긴다.
  * **고정 주기 틱**(FixedRateTicker)이 interval 마다 "지금까지의 최신 값"을 모아 표본 한 줄을 만든다. 틱이 늦으면 늦은 정도(late_s)와 건너뛴 틱 수(missed)를 표본에 적는다.
  * 각 값에는 age(그 값을 읽은 지 몇 초)가 붙고, 너무 오래된 값(stale)은 표본에서 빠진다 - 오래된 값으로 ABORT 를 내지 않기 위해서다.
  * AWS 는 프로세스를 띄우지 않고 **boto3 읽기 전용 래퍼**(ReadOnlyAws: 메서드 3개뿐)로 호출한다. 시작 비용 0, 호출당 수백 ms. boto3 가 없으면 CLI 실행기로 되돌아간다(AwsViaRunner).
  * 호출 빈도 상한: SQS 속성 15 s(2개 큐 = 분당 8회), CloudWatch 60 s(분당 1회, 지표 자체가 1분 해상도), Secrets Manager describe 300 s(분당 0.2회).
"""
import re
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------------------------- 고정 주기 틱
class FixedRateTicker:
    """interval 간격의 고정 시각 틱. 처리가 늦으면 밀리지 않고 건너뛴 틱 수를 센다(표본 누락을 숨기지 않는다)."""

    def __init__(self, interval, clock=time.time, sleep=time.sleep, start=None):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.next = start if start is not None else clock()
        self.index = -1

    def wait(self):
        """다음 틱 시각까지 기다린 뒤 {'index','scheduled','late_s','missed'} 를 돌려준다."""
        now = self.clock()
        missed = 0
        if now > self.next + self.interval:             # 지난 틱을 통째로 놓쳤다: 그만큼 건너뛴다
            missed = int((now - self.next) // self.interval)
            self.next += missed * self.interval
        delay = self.next - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.index += 1
        scheduled = self.next
        late = max(0.0, self.clock() - scheduled)
        self.next += self.interval
        return {"index": self.index, "scheduled": scheduled, "late_s": round(late, 3), "missed": missed}


# ---------------------------------------------------------------------------------------------- 소스 폴러
class Poller:
    """한 소스를 자기 주기로 읽는다. fn() 이 값을 돌려주거나 예외를 던진다(timeout 은 fn 안의 실행기가 건다)."""

    def __init__(self, name, fn, period_s, field=None, clock=time.time):
        self.name, self.fn, self.period_s, self.field, self.clock = name, fn, period_s, field or name, clock
        self.lock = threading.Lock()
        self.latest = None
        self.runs = 0
        self.stop_event = threading.Event()
        self.thread = None

    def poll_once(self):
        t0 = self.clock()
        try:
            value, ok, err = self.fn(), True, None
        except Exception as exc:  # noqa: BLE001  (부분 실패: 이 소스만 실패로 기록하고 계속한다)
            value, ok, err = None, False, f"{type(exc).__name__}: {str(exc)[:120]}"
        t1 = self.clock()
        with self.lock:
            prev = self.latest
            self.runs += 1
            fails = 0 if ok else ((prev or {}).get("consecutive_failures", 0) + 1)
            # 실패하면 마지막 성공 값을 남겨 age 로 낡음을 판단하게 한다
            keep = (prev or {}).get("value") if not ok else value
            keep_t = (prev or {}).get("ok_t_end") if not ok else t1
            self.latest = {"value": keep, "t_start": t0, "t_end": t1, "ms": int((t1 - t0) * 1000), "ok": ok, "error": err, "consecutive_failures": fails,
                           "ok_t_end": keep_t, "runs": self.runs}

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                t0 = self.clock()
                self.poll_once()
                self.stop_event.wait(max(0.0, self.period_s - (self.clock() - t0)))
        self.thread = threading.Thread(target=loop, name=f"poll-{self.name}", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return dict(self.latest) if self.latest else None


def build_sample(pollers, now, interval, max_age_s=None, critical=("pods", "db"), tick=None):
    """폴러들의 최신 값으로 표본 한 줄을 만든다.
    - 표본의 값 필드(pods, db, sqs …)는 '최근 max_age_s 이내에 성공한 값'만 넣는다. 오래됐거나 한 번도 못 읽었으면 빠지고 errors 에 이유가 남는다.
    - sources[name] = 그 소스의 마지막 시도(시작·완료 시각, 소요 ms, 성공 여부, 오류, 값의 age, 낡음 여부)
    - degraded = 핵심 소스(critical)가 낡았거나 틱이 2 interval 넘게 늦은 표본. 시간에 기반한 ABORT 를 보류하는 근거다."""
    s = {"ts": now, "sources": {}}
    errors = []
    degraded = False
    for p in pollers:
        snap = p.snapshot()
        limit = max_age_s if max_age_s is not None else max(3 * interval, 2 * p.period_s)
        if snap is None:
            s["sources"][p.name] = {"ok": False, "error": "아직 읽지 못함", "stale": True}
            errors.append(f"{p.name}: 아직 읽지 못함")
            degraded = degraded or p.name in critical
            continue
        age = None if snap["ok_t_end"] is None else round(now - snap["ok_t_end"], 3)
        stale = snap["ok_t_end"] is None or age > limit
        s["sources"][p.name] = {"t_start": round(snap["t_start"], 3), "t_end": round(snap["t_end"], 3), "ms": snap["ms"], "ok": snap["ok"], "error": snap["error"],
                                "age_s": age, "stale": stale, "consecutive_failures": snap["consecutive_failures"]}
        if snap["value"] is not None and not stale:
            s[p.field] = snap["value"]
        if not snap["ok"] or stale:
            errors.append(f"{p.name}: {snap['error'] or '낡은 값'}")
            degraded = degraded or p.name in critical
    if tick:
        s["tick"] = dict(tick)
        degraded = degraded or tick.get("late_s", 0) > 2 * interval
    if errors:
        s["errors"] = errors
    s["degraded"] = degraded
    return s


# ---------------------------------------------------------------------------------------------- AWS: 읽기 전용 래퍼
class ReadOnlyAws:
    """boto3 를 직접 노출하지 않고 읽기 메서드 3개만 연다: SQS 속성, CloudWatch 지표, Secrets Manager describe(값이 아니라 회전 시각).
    호출은 프로세스를 띄우지 않고(시작 비용 0) 연결 5 s / 읽기 5 s / 재시도 1회로 제한한다."""

    METHODS = ("sqs_attrs", "metric_points", "secret_dates")

    def __init__(self, region="ap-northeast-2", profile=None, clients=None):
        self._region, self._profile, self._clients = region, profile, clients or {}

    def _client(self, service):
        if service not in self._clients:
            import boto3
            from botocore.config import Config
            session = boto3.Session(profile_name=self._profile, region_name=self._region) if self._profile else boto3.Session(region_name=self._region)
            self._clients[service] = session.client(service, config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 1}))
        return self._clients[service]

    def sqs_attrs(self, queue_url):
        a = self._client("sqs").get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])["Attributes"]
        return {"visible": int(a.get("ApproximateNumberOfMessages", 0)), "not_visible": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
                "delayed": int(a.get("ApproximateNumberOfMessagesDelayed", 0))}

    def metric_points(self, namespace, metric, dimensions, start_epoch, end_epoch, period=60, stat="Maximum"):
        r = self._client("cloudwatch").get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
            StartTime=datetime.fromtimestamp(start_epoch, tz=timezone.utc), EndTime=datetime.fromtimestamp(end_epoch, tz=timezone.utc), Period=period, Statistics=[stat])
        return sorted(({"t": p["Timestamp"].timestamp(), "value": p[stat]} for p in r["Datapoints"]), key=lambda x: x["t"])

    def secret_dates(self, secret_id):
        d = self._client("secretsmanager").describe_secret(SecretId=secret_id)
        f = lambda v: v.timestamp() if v is not None else None  # noqa: E731
        return {"last_rotated": f(d.get("LastRotatedDate")), "next_rotation": f(d.get("NextRotationDate")), "rotation_enabled": d.get("RotationEnabled")}


# ---------------------------------------------------------------------------------------------- cAdvisor 텍스트 해석
_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>[-+0-9.eE]+|NaN|\+Inf|-Inf)(?:\s+(?P<ts>\d+))?\s*$')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
WANTED = ("container_cpu_usage_seconds_total", "container_memory_working_set_bytes")


def parse_cadvisor(text, namespace="parking-guardian"):
    """cAdvisor Prometheus 텍스트(`/metrics/cadvisor`) → {pod: {'cpu_s': 컨테이너 합, 'containers': {c: cpu_s}, 'mem_bytes': {c: 바이트}, 'ts_ms': 가장 늦은 시각}}.
    누적 카운터(container_cpu_usage_seconds_total)의 두 시점 차이가 그 사이에 쓴 CPU 초다 - 표본이 짧은 버스트를 놓쳐도 합계는 정확하다.
    container 가 비었거나 POD 인 시리즈(파드 전체·일시정지 컨테이너)는 합산에서 뺀다. [미확인] 실제 클러스터 출력으로는 아직 확인하지 않았다."""
    pods = {}
    for line in text.splitlines():
        if not line or line[0] == "#" or not line.startswith(WANTED):
            continue
        m = _LINE.match(line)
        if not m or m["name"] not in WANTED:
            continue
        labels = dict(_LABEL.findall(m["labels"]))
        if labels.get("namespace") != namespace or not labels.get("pod") or labels.get("container") in (None, "", "POD"):
            continue
        if m["name"] == "container_cpu_usage_seconds_total" and labels.get("cpu", "total") != "total":
            continue
        p = pods.setdefault(labels["pod"], {"cpu_s": 0.0, "containers": {}, "mem_bytes": {}, "ts_ms": None})
        try:
            value = float(m["value"])
        except ValueError:
            continue
        c = labels["container"]
        if m["name"] == "container_cpu_usage_seconds_total":
            p["containers"][c] = value
        else:
            p["mem_bytes"][c] = value
        if m["ts"]:
            p["ts_ms"] = max(p["ts_ms"] or 0, int(m["ts"]))
    for p in pods.values():
        p["cpu_s"] = round(sum(p["containers"].values()), 6)
    return pods


def cpu_rates(snapshots, pod_app, pod_created=None, window_start=None, requests=None):
    """cAdvisor 스냅샷 목록 [{'t': 시각, 'pods': parse_cadvisor 결과}] → 앱별 CPU 사용 요약.
    평균 CPU(m) = Δ누적 CPU 초 / Δ시간 × 1000. 구간(스냅샷 사이) 최대, 총 CPU 초, requests 대비 비율을 낸다.
    파드가 시험 중에 새로 생겼으면(생성 시각 ≥ window_start) 누적 카운터가 0 에서 시작했으므로 첫 관찰값을 그대로 더한다(초기화·모델 로딩 CPU 포함).
    카운터가 줄면(재시작) 그 구간은 버린다. 반환에 'resolution_s'(스냅샷 간격 중앙값)를 넣어 해상도를 숨기지 않는다."""
    requests = requests or {}
    pod_created = pod_created or {}
    per_pod = {}
    for prev, cur in zip(snapshots, snapshots[1:]):
        dt = cur["t"] - prev["t"]
        for pod, c in cur["pods"].items():
            q = prev["pods"].get(pod)
            if q is None or dt <= 0:
                continue
            delta = c["cpu_s"] - q["cpu_s"]
            if delta < 0:
                continue
            d = per_pod.setdefault(pod, {"intervals": [], "cpu_s": 0.0, "sec": 0.0, "mem_max": 0.0})
            d["intervals"].append(delta / dt * 1000.0)
            d["cpu_s"] += delta
            d["sec"] += dt
    seen_first = {}
    for snap in snapshots:
        for pod, c in snap["pods"].items():
            seen_first.setdefault(pod, (snap["t"], c["cpu_s"]))
            per_pod.setdefault(pod, {"intervals": [], "cpu_s": 0.0, "sec": 0.0, "mem_max": 0.0})
            per_pod[pod]["mem_max"] = max(per_pod[pod]["mem_max"], sum(c["mem_bytes"].values()))
    out = {}
    gaps = sorted(b["t"] - a["t"] for a, b in zip(snapshots, snapshots[1:]))
    for pod, d in per_pod.items():
        app = pod_app.get(pod)
        if app is None:
            continue
        extra, pre = 0.0, 0.0
        created = pod_created.get(pod)
        if window_start is not None and created is not None and created >= window_start and pod in seen_first:
            extra = seen_first[pod][1]              # 0 에서 시작한 카운터의 첫 관찰값까지의 CPU
            pre = max(0.0, seen_first[pod][0] - created)   # 생성부터 첫 관찰까지의 시간(평균 계산의 분모에 포함)
        total = d["cpu_s"] + extra
        a = out.setdefault(app, {"pods": 0, "cpu_s_total": 0.0, "observed_s": 0.0, "interval_peak_m": 0.0, "mem_mi_max": 0.0, "intervals": 0, "over_request_intervals": 0})
        req = requests.get(app)
        a["pods"] += 1
        a["cpu_s_total"] = round(a["cpu_s_total"] + total, 3)
        a["observed_s"] += d["sec"] + pre
        a["intervals"] += len(d["intervals"])
        a["interval_peak_m"] = round(max(a["interval_peak_m"], max(d["intervals"], default=0.0)), 1)
        a["mem_mi_max"] = round(max(a["mem_mi_max"], d["mem_max"] / 1048576), 1)
        a["over_request_intervals"] += sum(1 for x in d["intervals"] if req and x > req)
        a["cpu_request_m"] = req
    for app, a in out.items():
        a["avg_m"] = round(a["cpu_s_total"] / a["observed_s"] * 1000, 1) if a["observed_s"] else None
        a["avg_over_request"] = round(a["avg_m"] / a["cpu_request_m"], 3) if a["avg_m"] is not None and a.get("cpu_request_m") else None
        a["peak_over_request"] = round(a["interval_peak_m"] / a["cpu_request_m"], 3) if a.get("cpu_request_m") else None
        a["resolution_s_median"] = round(gaps[len(gaps) // 2], 1) if gaps else None
        a["observed_s"] = round(a["observed_s"], 1)
    return out
