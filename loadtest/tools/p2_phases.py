"""Worker 준비 단계 분석 (읽기 전용, 파일만 읽는다): 관찰자 타임라인 + 수집기 보고서 + (선택) KEDA 연산자 로그.

  python tools/p2_phases.py --timeline timeline.jsonl [--report p2-report.json] [--keda-log keda.log] [--out phases.json]

Worker 한 대가 0 에서 올라올 때의 단계를 구분해 보여 준다. 각 시각에는 근거 표시가 붙는다:
  observed(KEDA 로그)  KEDA 가 스케일 대상을 0 -> N 으로 조정했다고 로그에 남긴 시각(확장 결정)
  observed(K8s)        Pod 객체의 시각: 생성 · 노드 배치 · init(모델 내려받기) 종료 · 컨테이너 시작 · Ready
  observed(로그)       Worker 로그 줄의 시각(모델 로딩 완료로 보이는 줄이 로그에 있을 때만)
  observed(DB)         detection_results 의 판정 저장 시각
  estimated(...)       직접 관측하지 못해 다른 값에서 추정한 것 - 반드시 '추정'으로 읽는다
Kubernetes Ready 는 모델 준비 완료와 같지 않다: Worker 에는 readiness probe 가 없어서(R3) 컨테이너가 시작하면 바로 Ready 가 되고, 모델 로딩(약 9초)은 그 뒤에 한다 [관찰: 스모크].
모델 준비 완료를 로그에서 못 찾으면 '첫 판정 완료 시각 - 그 판정의 추론 시간'을 **상한**으로 추정한다(그 시각에는 이미 로딩이 끝나 있었다).
이 도구는 Worker 코드·probe 를 바꾸지 않는다 - 관측만 한다.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_collect as pc  # noqa: E402
import p2_watch as pw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PHASE_ORDER = ("keda_decision", "pod_created", "pod_scheduled", "init_done", "container_started", "k8s_ready", "model_loaded", "first_result")
LABEL = {"keda_decision": "KEDA 확장 결정", "pod_created": "Pod 생성", "pod_scheduled": "노드 배치", "init_done": "init 종료(모델 내려받기)", "container_started": "컨테이너 시작",
         "k8s_ready": "Kubernetes Ready", "model_loaded": "모델 로딩 완료", "first_result": "첫 판정 완료"}
LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s")


def _t(v):
    return None if v is None else pc.ts(v)


def collect_worker_pods(samples):
    pods = {}
    for s in samples:
        for p in s.get("pods") or []:
            if p["app"] != "worker":
                continue
            d = pods.setdefault(p["name"], {"first_seen": s["ts"], "last_seen": s["ts"], "created": None, "scheduled_at": None, "init_done_at": None, "started_at": None, "ready_at": None})
            d["last_seen"] = s["ts"]
            for k in ("created", "scheduled_at", "init_done_at", "started_at", "ready_at"):
                d[k] = d[k] or p.get(k)
    return pods


def collect_logs(samples):
    """타임라인에 바뀔 때만 기록된 worker_logs 중 Pod 별로 가장 마지막 것."""
    out = {}
    for s in samples:
        for pod, st in (s.get("worker_logs") or {}).items():
            out[pod] = st
    return out


def model_ready_from_log(lines):
    """로그 줄 중 모델 로딩 완료로 보이는 첫 줄의 시각(epoch) 또는 None. 줄 형식은 [미확인]이라 pw.MODEL_READY 정규식에 맞는 줄만 인정한다."""
    for ln in lines or []:
        if pw.MODEL_READY.search(ln):
            m = LOG_TS.match(ln)
            if m:
                return pc.ts(m.group(1))
    return None


def worker_phases(samples, report=None, keda_decisions=None):
    pods, logs = collect_worker_pods(samples), collect_logs(samples)
    detail = (report or {}).get("workers_detail") or {}
    decisions = [d for d in (keda_decisions or []) if d.get("to") is not None and d["to"] > (d.get("from") or 0)]
    rows = []
    for name, d in sorted(pods.items(), key=lambda kv: kv[1]["first_seen"]):
        ph = {}

        def put(key, t, source):
            if t is not None:
                ph[key] = {"label": LABEL[key], "t": round(t, 3), "source": source}
        created = _t(d["created"])
        put("pod_created", created if created is not None else d["first_seen"], "observed(K8s)" if created is not None else "estimated(처음 본 표본 시각 - 상한)")
        put("pod_scheduled", _t(d["scheduled_at"]), "observed(K8s)")
        put("init_done", _t(d["init_done_at"]), "observed(K8s)")
        put("container_started", _t(d["started_at"]), "observed(K8s)")
        put("k8s_ready", _t(d["ready_at"]), "observed(K8s)")
        ref = created if created is not None else d["first_seen"]
        cand = [x for x in decisions if ref - 90 <= x["ts"] <= ref + 5]
        if cand:
            put("keda_decision", max(cand, key=lambda x: x["ts"])["ts"], "observed(KEDA 로그)")
        wd = next((v for k, v in detail.items() if k.startswith(name)), None)
        if wd and wd.get("first_result") is not None:
            put("first_result", wd["first_result"], "observed(DB)")
        t_log = model_ready_from_log((logs.get(name) or {}).get("lines"))
        if t_log is not None:
            put("model_loaded", t_log, "observed(로그)")
        elif wd and wd.get("first_result") is not None and wd.get("first_elapsed_ms") is not None:
            put("model_loaded", wd["first_result"] - wd["first_elapsed_ms"] / 1000.0, "estimated(첫 판정 완료 - 그 추론 시간: 로딩은 이 시각 이전에 끝났다 = 상한)")
        order = [k for k in PHASE_ORDER if k in ph]
        deltas = {f"{a}->{b}": round(ph[b]["t"] - ph[a]["t"], 2) for a, b in zip(order, order[1:])}
        rows.append({"pod": name, "phases": ph, "deltas_s": deltas,
                     "cold_start_total_s": round(ph["first_result"]["t"] - ph["keda_decision"]["t"], 2) if "first_result" in ph and "keda_decision" in ph else None,
                     "k8s_ready_vs_model_loaded_s": round(ph["model_loaded"]["t"] - ph["k8s_ready"]["t"], 2) if "model_loaded" in ph and "k8s_ready" in ph else None,
                     "missing": [k for k in PHASE_ORDER if k not in ph]})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeline", required=True); ap.add_argument("--report"); ap.add_argument("--keda-log"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    samples = [json.loads(line) for line in Path(a.timeline).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = json.loads(Path(a.report).read_text(encoding="utf-8")) if a.report else None
    keda = pw.parse_keda_log(Path(a.keda_log).read_text(encoding="utf-8", errors="replace")) if a.keda_log else None
    rows = worker_phases(samples, report, keda)
    text = json.dumps(rows, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    for r in rows:
        print(f"== {r['pod']}")
        for k in PHASE_ORDER:
            if k in r["phases"]:
                p = r["phases"][k]
                print(f"  {p['label']:<22} {p['t']:.3f}  [{p['source']}]")
        print("  단계 간(s):", r["deltas_s"], "| 빠진 단계:", r["missing"])
    if not rows:
        print("타임라인에 Worker Pod 가 없다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
