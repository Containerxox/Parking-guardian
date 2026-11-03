"""P3: Argo CD 자동 동기화 일시 중단 + backend/inference HPA 를 min=max=2 로 고정, 그리고 복구와 검증.

    python loadtest/tools/p3.py snapshot  [--out DIR]                읽기 전용: 현재 상태를 파일로 저장(백업)
    python loadtest/tools/p3.py status --expect baseline|fixed|restored [--snap DIR]
                                                                     읽기 전용: 기대 상태와 맞는지 항목별로 PASS/FAIL
    python loadtest/tools/p3.py pause   [--apply]                    중단 + 고정. --apply 가 없으면 할 일을 출력만 한다
    python loadtest/tools/p3.py restore --snap DIR [--apply] [--emergency] [--observe SEC] [--replaced 앱,앱]
                                                                     --replaced: 실험이 일부러 Pod 를 교체한 앱(backend,inference,frontend).
                                                                     그 앱은 UID 불변 대신 Ready 수·재시작 0·Endpoint(backend 는 ALB healthy) 로 판정하고,
                                                                     나머지 앱은 기존처럼 엄격하게(UID·재시작 불변) 본다. 관찰 단계는 시작 시점 상태를 기준으로
                                                                     새 Pod·재시작이 없는지 모든 앱에 엄격하게 본다.
                                                                     복구. --emergency 는 root.yaml 로 안 돌아올 때 직접 패치

변경하는 것은 (1) Argo Application root, parking-guardian 의 spec.syncPolicy.automated (2) HPA backend, inference 의 min/maxReplicas
뿐이다. Deployment, Service, Ingress, 이미지, Git, Terraform 은 건드리지 않는다. 승인 없이는 --apply 를 쓰지 않는다.
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO.parent / "parking-guardian-config"
EKS_DOCS = REPO / "docs" / "measurements" / "phase1" / "eks"
APP_NS, ARGO_NS = "parking-guardian", "argocd"
AUTO = {"prune": True, "selfHeal": True}
CHILD = "parking-guardian"
APPS = ["root", "parking-guardian", "platform", "monitoring", "monitoring-config"]
HPAS = ["backend", "inference"]
PODS_EXPECT = {"backend": 2, "inference": 2, "frontend": 1}


def kube(*args, data=None):
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, encoding="utf-8", input=data)
    if r.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}\n{r.stderr.strip()[:300]}")
    return r.stdout


def kj(*args):
    return json.loads(kube(*args, "-o", "json"))


def strip(obj):
    obj = json.loads(json.dumps(obj))
    obj.get("metadata", {}).pop("managedFields", None)
    return obj


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ 상태 읽기
def read_state():
    s = {"apps": {a: kj("-n", ARGO_NS, "get", "app", a) for a in APPS},
         "hpa": {h: kj("-n", APP_NS, "get", "hpa", h) for h in HPAS},
         "deploy": {d: kj("-n", APP_NS, "get", "deploy", d) for d in PODS_EXPECT},
         "pods": kj("-n", APP_NS, "get", "pods")["items"],
         "endpoints": kj("-n", APP_NS, "get", "endpointslices", "-l", "kubernetes.io/service-name=inference")["items"],
         "endpoints_backend": kj("-n", APP_NS, "get", "endpointslices", "-l", "kubernetes.io/service-name=backend")["items"]}
    return s


def pod_rows(pods):
    rows = {}
    for p in pods:
        name = p["metadata"]["name"]
        app = p["metadata"].get("labels", {}).get("app.kubernetes.io/name", "")
        if app not in PODS_EXPECT:
            continue
        cs = p["status"].get("containerStatuses", [])
        rows[name] = {"app": app, "uid": p["metadata"]["uid"], "phase": p["status"].get("phase"),
                      "ready": all(c.get("ready") for c in cs) if cs else False,
                      "restarts": sum(c.get("restartCount", 0) for c in cs), "node": p["spec"].get("nodeName")}
    return rows


# ------------------------------------------------------------------ 스냅샷
def snapshot(out=None):
    st = read_state()
    out = pathlib.Path(out) if out else EKS_DOCS / f"s1-p3-snapshot-{dt.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for a, o in st["apps"].items():
        files[f"app-{a}.json"] = strip(o)
    for h, o in st["hpa"].items():
        files[f"hpa-{h}.json"] = strip(o)
    for d, o in st["deploy"].items():
        files[f"deploy-{d}.json"] = strip(o)
    files["pods.json"] = pod_rows(st["pods"])
    files["meta.json"] = {"taken_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                          "config_repo_head": subprocess.run(["git", "-C", str(CONFIG), "rev-parse", "--short", "HEAD"],
                                                             capture_output=True, text=True).stdout.strip(),
                          "argo_revision": st["apps"][CHILD]["status"]["sync"].get("revision", "")[:7]}
    sums = []
    for name, obj in files.items():
        text = json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        (out / name).write_bytes(text.encode("utf-8"))  # LF 로 쓴다 (sha256sum -c 가 그대로 읽는다)
        sums.append(f"{hashlib.sha256(text.encode('utf-8')).hexdigest()} *{name}")
    (out / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    log(f"스냅샷 저장: {out} ({len(files)}개 파일)")
    return out


def ready_count(slices):
    return sum(1 for sl in slices for e in (sl.get("endpoints") or []) if e.get("conditions", {}).get("ready"))


def alb_backend_healthy():
    """ALB backend 대상 그룹의 healthy 대상 수. 읽기 실패면 None (AWS_PROFILE 필요)."""
    try:
        tg = subprocess.run(["aws", "elbv2", "describe-target-groups", "--query", "TargetGroups[?contains(TargetGroupName,`backend`)].TargetGroupArn",
                             "--output", "text"], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout.strip()
        r = subprocess.run(["aws", "elbv2", "describe-target-health", "--target-group-arn", tg, "--query",
                            "length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])", "--output", "text"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 상태 검사
def check(expect, snap=None, replaced=(), ref=None):
    st = read_state()
    res = []

    def add(name, ok, detail=""):
        res.append((name, bool(ok), detail))

    rows = pod_rows(st["pods"])
    by_app = {a: [r for r in rows.values() if r["app"] == a] for a in PODS_EXPECT}
    for a, n in PODS_EXPECT.items():
        ready = [r for r in by_app[a] if r["phase"] == "Running" and r["ready"]]
        add(f"Pod {a}: Running+Ready {n}개", len(ready) == n and len(by_app[a]) == n, f"{len(ready)}/{len(by_app[a])}")
    add("Pending/비정상 Pod 없음", all(r["phase"] == "Running" for r in rows.values()), "")
    eps = [e for sl in st["endpoints"] for e in sl.get("endpoints", [])]
    add("inference Service 엔드포인트 ready 2개", len([e for e in eps if e["conditions"].get("ready")]) == 2, f"{len(eps)}개")
    apps_spec = {a: st["apps"][a]["spec"].get("syncPolicy", {}).get("automated") for a in APPS}
    sync = {a: (st["apps"][a]["status"]["sync"]["status"], st["apps"][a]["status"]["health"]["status"]) for a in APPS}
    hp = {h: (st["hpa"][h]["spec"]["minReplicas"], st["hpa"][h]["spec"]["maxReplicas"],
              st["hpa"][h]["status"].get("currentReplicas"), st["hpa"][h]["status"].get("desiredReplicas")) for h in HPAS}

    if expect in ("baseline", "restored"):
        add("root 자동 동기화 켜짐 (prune+selfHeal)", apps_spec["root"] == AUTO, str(apps_spec["root"]))
        add("parking-guardian 자동 동기화 켜짐", apps_spec[CHILD] == AUTO, str(apps_spec[CHILD]))
        for h in HPAS:
            add(f"HPA {h}: min 2 / max 3", hp[h][:2] == (2, 3), str(hp[h]))
            add(f"HPA {h}: 현재=목표=2", hp[h][2] == 2 and hp[h][3] == 2, str(hp[h]))
        add("Application 5개 모두 Synced/Healthy", all(v == ("Synced", "Healthy") for v in sync.values()), str(sync))
    if expect == "fixed":
        add("root 자동 동기화 꺼짐", apps_spec["root"] is None, str(apps_spec["root"]))
        add("parking-guardian 자동 동기화 꺼짐", apps_spec[CHILD] is None, str(apps_spec[CHILD]))
        for other in ("platform", "monitoring", "monitoring-config"):
            add(f"{other} 자동 동기화는 그대로", apps_spec[other] is not None, str(apps_spec[other]))
        for h in HPAS:
            add(f"HPA {h}: min 2 / max 2", hp[h][:2] == (2, 2), str(hp[h]))
            add(f"HPA {h}: 현재=목표=2 (확장 없음)", hp[h][2] == 2 and hp[h][3] == 2, str(hp[h]))
        add("parking-guardian 은 OutOfSync 가 정상(HPA 가 Git 과 다름), Healthy", sync[CHILD][1] == "Healthy", str(sync[CHILD]))
    if snap:
        snap = pathlib.Path(snap)
        pods0 = json.loads((snap / "pods.json").read_text(encoding="utf-8"))
        rep_apps = set(replaced)
        fixed0 = {n: r for n, r in pods0.items() if r["app"] not in rep_apps}
        same = all(n in rows and rows[n]["uid"] == r["uid"] and rows[n]["restarts"] == r["restarts"] for n, r in fixed0.items())
        names_ok = {n for n, r in rows.items() if r["app"] not in rep_apps} == set(fixed0)
        if not rep_apps:
            add("백업 이후 Pod 가 그대로 (UID·재시작 횟수 같음, 새 Pod 없음)", same and names_ok, f"{len(rows)}개")
        else:
            add(f"교체 허용({','.join(sorted(rep_apps))}) 이외 앱의 Pod 는 백업과 같음 (UID·재시작 같음, 새 Pod 없음)", same and names_ok, f"{len(fixed0)}개")
            for a in sorted(rep_apps):
                mine = [r for r in rows.values() if r["app"] == a]
                add(f"교체된 {a} Pod: Running+Ready {PODS_EXPECT[a]}개, 재시작 0",
                    len(mine) == PODS_EXPECT[a] and all(r["phase"] == "Running" and r["ready"] and r["restarts"] == 0 for r in mine), f"{len(mine)}개")
            if "backend" in rep_apps:
                add("backend Service 엔드포인트 ready 2개", ready_count(st["endpoints_backend"]) == 2, str(ready_count(st["endpoints_backend"])))
                healthy = alb_backend_healthy()
                add("ALB backend 대상 healthy 2개", healthy == 2, str(healthy))
        if expect in ("baseline", "restored"):
            for a in ("root", CHILD):
                old = json.loads((snap / f"app-{a}.json").read_text(encoding="utf-8"))["spec"]
                add(f"Application {a} spec 이 백업과 같음", st["apps"][a]["spec"] == old, "")
            for h in HPAS:
                old = json.loads((snap / f"hpa-{h}.json").read_text(encoding="utf-8"))["spec"]
                add(f"HPA {h} spec 이 백업과 같음", st["hpa"][h]["spec"] == old, "")
        for d in PODS_EXPECT:
            old = json.loads((snap / f"deploy-{d}.json").read_text(encoding="utf-8"))["spec"]
            cur = st["deploy"][d]["spec"]
            same_t = cur["template"] == old["template"]
            add(f"Deployment {d} Pod 템플릿이 백업과 같음 (롤링 없음)", same_t, "")
    if ref is not None:
        stable = set(rows) == set(ref) and all(rows[n]["uid"] == r["uid"] and rows[n]["restarts"] == r["restarts"] for n, r in ref.items())
        add("관찰 시작 시점 대비 Pod 불변 (새 Pod·재시작 없음, 모든 앱)", stable, f"{len(rows)}개")
    ok = True
    for name, good, detail in res:
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}  {detail}")
    print("=> " + ("전부 통과" if ok else "실패 항목 있음"))
    return ok


# ------------------------------------------------------------------ 변경
def patch_app(name, body):
    kube("-n", ARGO_NS, "patch", "app", name, "--type", "merge", "-p", json.dumps(body))


def patch_hpa(name, mn, mx):
    kube("-n", APP_NS, "patch", "hpa", name, "--type", "merge", "-p", json.dumps({"spec": {"minReplicas": mn, "maxReplicas": mx}}))


def pause(apply):
    plan = ["0. 사전: status --expect baseline 이 전부 PASS 여야 한다 (아니면 중단)",
            "1. snapshot 으로 백업 저장",
            "2. root 의 spec.syncPolicy.automated 제거   (kubectl patch app root --type merge -p '{\"spec\":{\"syncPolicy\":{\"automated\":null}}}')",
            "3. parking-guardian 의 spec.syncPolicy.automated 제거 (같은 패치)",
            "4. HPA backend, inference 를 min=2, max=2 로 패치",
            "5. 60초 기다린 뒤 status --expect fixed 와 백업 비교 (Pod 불변)"]
    print("\n".join(plan))
    if not apply:
        print("\n(--apply 없음: 아무것도 변경하지 않았다)")
        return 0
    if not check("baseline"):
        log("사전 점검 실패: 변경하지 않고 종료")
        return 2
    snap = snapshot()
    log(f"백업: {snap}  <- 복구에 이 경로가 필요하다")
    log("root 자동 동기화 제거"); patch_app("root", {"spec": {"syncPolicy": {"automated": None}}})
    log("parking-guardian 자동 동기화 제거"); patch_app(CHILD, {"spec": {"syncPolicy": {"automated": None}}})
    for h in HPAS:
        log(f"HPA {h} -> min=max=2"); patch_hpa(h, 2, 2)
    log("60초 관찰"); time.sleep(60)
    ok = check("fixed", snap)
    print(f"\n복구 명령: python loadtest/tools/p3.py restore --snap \"{snap}\" --apply")
    return 0 if ok else 3


def restore(snap, apply, emergency, observe, replaced=()):
    plan = ["1. kubectl apply -f <config>/argocd/root.yaml  (root 자동 동기화 복구)",
            "2. 최대 6분 동안 15초마다 status --expect restored: root·자식 automated, HPA 2/3, Application 5개 Synced/Healthy,",
            "   백업과 spec 일치, Pod 불변 (--replaced 앱은 교체 Pod Ready·재시작 0·Endpoint 로 대신 판정)",
            "3. 실패하면 중단하고 보고. --emergency 일 때만 직접 패치(automated 복원, HPA 2/3)로 재시도",
            "4. --observe SEC: 복구 뒤 SEC 초 동안 1분마다 다시 검사 (OutOfSync·재시작·새 Pod 없는지)"]
    print("\n".join(plan))
    if not apply:
        print("\n(--apply 없음: 아무것도 변경하지 않았다)")
        return 0
    log("root.yaml 적용"); print(kube("apply", "-f", str(CONFIG / "argocd" / "root.yaml")).strip())
    deadline, ok = time.time() + 360, False
    while time.time() < deadline:
        time.sleep(15)
        log("검사"); ok = check("restored", snap, replaced)
        if ok:
            break
    if not ok and emergency:
        log("비상 복구: 직접 패치")
        patch_app("root", {"spec": {"syncPolicy": {"automated": AUTO}}})
        patch_app(CHILD, {"spec": {"syncPolicy": {"automated": AUTO}}})
        for h in HPAS:
            patch_hpa(h, 2, 3)
        time.sleep(60); ok = check("restored", snap, replaced)
    if ok and observe:
        ref = pod_rows(read_state()["pods"])  # 관찰 기준: 이 시점 이후 새 Pod·재시작이 없어야 한다
        end = time.time() + observe
        while ok and time.time() < end:
            time.sleep(60); log("관찰"); ok = check("restored", snap, replaced, ref)
    log("복구 " + ("완료" if ok else "실패 — 보고하고 사용자 판단을 기다린다"))
    return 0 if ok else 4


def parse_replaced(text):
    apps = tuple(x.strip() for x in text.split(",") if x.strip())
    bad = [x for x in apps if x not in PODS_EXPECT]
    if bad:
        sys.exit(f"--replaced 는 {', '.join(PODS_EXPECT)} 중에서만 고를 수 있습니다: {bad}")
    return apps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--out")
    s = sub.add_parser("status"); s.add_argument("--expect", required=True, choices=["baseline", "fixed", "restored"]); s.add_argument("--snap")
    s.add_argument("--replaced", default="")
    s = sub.add_parser("pause"); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("restore"); s.add_argument("--snap", required=True); s.add_argument("--apply", action="store_true")
    s.add_argument("--emergency", action="store_true"); s.add_argument("--observe", type=int, default=0); s.add_argument("--replaced", default="")
    a = ap.parse_args()
    if a.cmd == "snapshot":
        snapshot(a.out); return 0
    if a.cmd == "status":
        return 0 if check(a.expect, a.snap, parse_replaced(a.replaced)) else 1
    if a.cmd == "pause":
        return pause(a.apply)
    return restore(a.snap, a.apply, a.emergency, a.observe, parse_replaced(a.replaced))


if __name__ == "__main__":
    sys.exit(main())
