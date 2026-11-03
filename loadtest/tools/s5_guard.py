"""S5-a 안전장치: inference Deployment 를 정해진 시각에 2 Pod 로 되돌린다. 실행기와 따로 도는 독립 프로세스다.

    python loadtest/tools/s5_guard.py --now [--dry-run]                즉시 복구(수동 비상용). 복구 후 Ready Pod·Endpoint 확인
    python loadtest/tools/s5_guard.py --deadline-epoch <초> [--log 파일]  그 시각까지 기다렸다가 복구(실행기가 죽어도 동작)

하는 일은 하나다: inference 의 spec.replicas 가 0 이면 2 로 되돌린다(이미 2 이상이면 아무것도 하지 않는다). HPA·Argo·Deployment 템플릿은 건드리지 않는다.
--dry-run 은 kubectl --dry-run=server 라 클러스터는 바뀌지 않는다(권한·명령 검증용).
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time

NS, DEPLOY, WANT = "parking-guardian", "inference", 2


def kube(*args):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, encoding="utf-8")


def say(msg, logfile=None):
    line = f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}Z] {msg}"
    print(line, flush=True)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def spec_replicas():
    r = kube("-n", NS, "get", "deploy", DEPLOY, "-o", "jsonpath={.spec.replicas}")
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() != "" else None


def ready_endpoints():
    r = kube("-n", NS, "get", "endpointslices", "-l", f"kubernetes.io/service-name={DEPLOY}", "-o", "json")
    if r.returncode:
        return None
    return sum(1 for sl in json.loads(r.stdout)["items"] for e in (sl.get("endpoints") or []) if e.get("conditions", {}).get("ready"))


FAULT_ENV = "INFERENCE_FAULT_DELAY_MS"


def fault_env():
    """inference Deployment 에 지연 설정이 있으면 그 값, 없으면 "", 읽기 실패면 None."""
    r = kube("-n", NS, "get", "deploy", DEPLOY, "-o",
             "jsonpath={range .spec.template.spec.containers[0].env[?(@.name==\"" + FAULT_ENV + "\")]}{.value}{end}")
    return r.stdout.strip() if r.returncode == 0 else None


def clear_fault(dry_run=False, logfile=None, wait_s=300):
    """S5-b 비상 복구: 지연 설정을 없애고 inference 를 2 Pod 로 되돌린다.
    롤링 업데이트는 쓰지 않는다(노드 CPU 요청 여유가 inference 1 CPU 보다 작아 surge Pod 가 스케줄되지 않는다).
    순서: scale 0 → set env 제거 → scale 2 (S5-a 에서 검증한 방식). 설정이 이미 없으면 restore() 와 같다."""
    env = fault_env()
    if env is None:
        say("지연 설정을 읽지 못했습니다(kubectl 실패)", logfile)
        return False
    if env == "":
        return restore(dry_run, logfile, wait_s)
    dry = ["--dry-run=server"] if dry_run else []
    for args in (["scale", "deploy/" + DEPLOY, "--replicas=0"], ["set", "env", "deploy/" + DEPLOY, FAULT_ENV + "-"],
                 ["scale", "deploy/" + DEPLOY, "--replicas=" + str(WANT)]):
        r = kube("-n", NS, *args, *dry)
        say(" ".join(args) + (" (dry-run)" if dry_run else "") + ": rc=" + str(r.returncode) + " " + r.stderr.strip()[:150], logfile)
        if r.returncode:
            return False
    if dry_run:
        return True
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if fault_env() == "" and spec_replicas() == WANT and ready_endpoints() == WANT:
            say("지연 설정 제거·복구 확인: replicas=2, Ready Endpoint=2 (" + str(round(time.time() - t0)) + "s)", logfile)
            return True
        time.sleep(3)
    say("복구 미확인: env=" + str(fault_env()) + " replicas=" + str(spec_replicas()) + " endpoints=" + str(ready_endpoints()), logfile)
    return False


def restore(dry_run=False, logfile=None, wait_s=300):
    cur = spec_replicas()
    if cur is None:
        say("replicas 를 읽지 못했습니다(kubectl 실패)", logfile)
        return False
    if cur != 0:
        say(f"replicas={cur}: 0 이 아니므로 아무것도 하지 않습니다", logfile)
        return True
    cmd = ["-n", NS, "scale", f"deploy/{DEPLOY}", f"--replicas={WANT}"] + (["--dry-run=server"] if dry_run else [])
    r = kube(*cmd)
    say(f"scale -> {WANT}{' (dry-run)' if dry_run else ''}: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()[:200]}", logfile)
    if r.returncode or dry_run:
        return r.returncode == 0
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if spec_replicas() == WANT and ready_endpoints() == WANT:
            say(f"복구 확인: replicas={WANT}, Ready Endpoint={WANT} ({time.time() - t0:.0f}s)", logfile)
            return True
        time.sleep(3)
    say(f"복구 미확인: {wait_s}s 안에 Endpoint {WANT}개가 되지 않았습니다(replicas={spec_replicas()}, endpoints={ready_endpoints()})", logfile)
    return False


def status():
    for name in ("backend", "inference", "frontend"):
        r = kube("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=" + name, "-o", "json")
        print("[" + name + "]")
        for p in (json.loads(r.stdout)["items"] if r.returncode == 0 else []):
            cs = p["status"].get("containerStatuses", [])
            print("  ", p["metadata"]["name"], "ready=" + str(bool(cs) and all(c.get("ready") for c in cs)), "phase=" + str(p["status"].get("phase")),
                  "ip=" + str(p["status"].get("podIP")), "terminating=" + str(bool(p["metadata"].get("deletionTimestamp"))))
    print("inference 지연 설정(" + FAULT_ENV + ") =", repr(fault_env()), "('' = 없음)")
    for name in ("backend", "inference"):
        r = kube("-n", NS, "get", "endpointslices", "-l", "kubernetes.io/service-name=" + name, "-o", "json")
        n = sum(1 for sl in json.loads(r.stdout)["items"] for e in (sl.get("endpoints") or []) if e.get("conditions", {}).get("ready")) if r.returncode == 0 else None
        print(name + " Ready Endpoint =", n)
    t = subprocess.run(["aws", "elbv2", "describe-target-groups", "--query", "TargetGroups[?contains(TargetGroupName,`backend`)].TargetGroupArn", "--output", "text"],
                       capture_output=True, text=True, encoding="utf-8").stdout.strip()
    h = subprocess.run(["aws", "elbv2", "describe-target-health", "--target-group-arn", t, "--query",
                        "TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]", "--output", "text"],
                       capture_output=True, text=True, encoding="utf-8").stdout.strip()
    print("ALB backend 대상(ip state reason):")
    print(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--deadline-epoch", type=float)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clear-fault", action="store_true", help="S5-b: 지연 설정 제거 + inference 2 Pod 복구(롤링 없이 scale 0→env 제거→scale 2)")
    ap.add_argument("--status", action="store_true", help="backend/inference Pod, Endpoint, ALB 대상 상태를 읽기 전용으로 출력")
    ap.add_argument("--log")
    a = ap.parse_args()
    if a.status:
        status()
        return
    if not a.now and a.deadline_epoch is None:
        ap.error("--now 또는 --deadline-epoch 가 필요합니다")
    if not a.now:
        say(f"감시 시작: {dt.datetime.fromtimestamp(a.deadline_epoch, dt.timezone.utc):%H:%M:%S}Z 까지 기다립니다", a.log)
        while time.time() < a.deadline_epoch:
            time.sleep(2)
        say("기한 도달: 복구를 시도합니다", a.log)
    sys.exit(0 if (clear_fault if a.clear_fault else restore)(a.dry_run, a.log) else 2)


if __name__ == "__main__":
    main()
