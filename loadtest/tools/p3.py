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

[2026-11-04 보완 - Phase 2 비교 구성 · Application 개수 일반화. 원본: p3.py.bak-before-p2]
  * Application 목록은 하드코딩(5개) 대신 "백업에 있는 것"(없으면 지금 클러스터에 있는 것)을 쓴다. 지금은 keda 가 추가돼 6개다.
    백업의 Application 이 하나라도 없으면 FAIL, 백업에 없던 새 Application 은 INFO 로만 알린다. 기존 명령·옵션은 그대로 동작한다.
  * 필수 객체(inference HPA/Deployment 등)가 없으면 예외 대신 FAIL 로 보고한다(비교 구성에서 실수로 Phase 1 명령을 쓴 경우).
  * snapshot 은 relay·worker Deployment, ScaledObject worker, keda-hpa-worker 도 (있으면) 같이 저장한다.
  새 명령(Phase 2, 읽기 전용이 아니면 모두 --apply 가 있어야 변경한다):
    p3.py status --expect p2-baseline|p2-comparison|p2-fixed [--workers N]
        p2-baseline   공존 구성(Git 에 비교 구성 패치 없음): inference 2 + HPA 2/3, Worker 용 ScaledObject min 0 / max 1, 일시정지 없음, 자동 동기화 켜짐
        p2-comparison 비교 구성이 Git 에 반영된 상태(자동 동기화 켜짐): inference 없음, ScaledObject min 1 / max 3
        p2-fixed      비교 구성 + 측정 고정: root·parking-guardian 자동 동기화 꺼짐, backend HPA 2/2, (--workers N 이면) paused-replicas=N 이고 Worker N개 Ready
    p3.py p2-pause   [--apply]       p2-comparison 인지 확인 -> 백업 -> 자동 동기화 끄기 + backend HPA 2/2 -> p2-fixed 확인
    p3.py p2-workers N [--apply]     Worker 수 고정(ScaledObject 의 paused-replicas=N). 자동 동기화가 꺼져 있을 때만. --release 로 해제
    p3.py p2-restore --target comparison|baseline [--apply] [--observe SEC]
                                     일시정지 해제 + root.yaml 재적용(자동 동기화·HPA 는 Git 대로 복구) -> 목표 상태 확인.
                                     baseline 은 사용자가 Git 의 비교 구성 패치를 먼저 되돌린 뒤에만 의미가 있다(이 도구는 Git 을 건드리지 않는다).
  Worker 수와 KEDA·Argo 의 충돌: worker Deployment 의 replicas 는 KEDA(HPA keda-hpa-worker)가 정한다. `kubectl scale` 은 되돌려진다.
    Application 이 Deployment/worker 의 /spec/replicas 를 무시하므로(ignoreDifferences) Argo 는 KEDA 의 결정을 되돌리지 않는다. paused-replicas 어노테이션은
    Git 에 없는 값이라 자동 동기화가 켜져 있으면 되돌려질 수 있다(미확인) -> p2-workers 는 자동 동기화를 끈 상태에서만 실행한다.
    inference 가 사라진 비교 구성에서 inference 를 되살리려면 1000m 자리가 필요하다 -> p2-restore --target baseline 은 Worker 가 0~1 이 될 때까지 기다리지 않고
    Pending Pod 가 있으면 FAIL 로 보고만 한다(기다리다 멈추는 것을 막으려고 6분 제한).
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
APPS = ["root", "parking-guardian", "platform", "monitoring", "monitoring-config"]   # 2026-11-04 이전의 고정 목록(백업에 apps 정보가 없을 때 호환용)
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


def try_kj(*args):
    """없는 객체(NotFound)는 None, 그 밖의 오류는 그대로 올린다."""
    try:
        return kj(*args)
    except RuntimeError as exc:
        if "NotFound" in str(exc) or "not found" in str(exc):
            return None
        raise


def live_apps():
    return sorted(i["metadata"]["name"] for i in kj("-n", ARGO_NS, "get", "app")["items"])


def snap_apps(snap):
    """백업 폴더의 Application 목록: app-<이름>.json 파일들. 없으면(아주 오래된 백업) 옛 고정 목록."""
    names = sorted(f.name[4:-5] for f in pathlib.Path(snap).glob("app-*.json"))
    return names or list(APPS)


def apps_for(snap):
    return snap_apps(snap) if snap else live_apps()


# ------------------------------------------------------------------ 상태 읽기
def read_state(apps=None):
    s = {"apps": {a: try_kj("-n", ARGO_NS, "get", "app", a) for a in (apps or APPS)},
         "hpa": {h: try_kj("-n", APP_NS, "get", "hpa", h) for h in HPAS},
         "deploy": {d: try_kj("-n", APP_NS, "get", "deploy", d) for d in PODS_EXPECT},
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
P2_OBJECTS = (("deploy", "worker"), ("deploy", "relay"), ("scaledobject", "worker"), ("hpa", "keda-hpa-worker"))


def snapshot(out=None):
    st = read_state(live_apps())
    out = pathlib.Path(out) if out else EKS_DOCS / f"s1-p3-snapshot-{dt.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for a, o in st["apps"].items():
        if o is not None:
            files[f"app-{a}.json"] = strip(o)
    for h, o in st["hpa"].items():
        if o is not None:
            files[f"hpa-{h}.json"] = strip(o)
    for d, o in st["deploy"].items():
        if o is not None:
            files[f"deploy-{d}.json"] = strip(o)
    for kind, name in P2_OBJECTS:       # Phase 2 객체: 있으면 저장(없으면 건너뜀 - 예전 클러스터와 호환)
        o = try_kj("-n", APP_NS, "get", kind, name)
        if o is not None:
            files[f"{kind}-{name}.json"] = strip(o)
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
    apps = apps_for(snap)
    st = read_state(apps)
    res = []

    def add(name, ok, detail=""):
        res.append((name, bool(ok), detail))

    missing_apps = [a for a in apps if st["apps"][a] is None]
    add("백업의 Application 이 모두 있음" if snap else "Application 이 모두 있음", not missing_apps, ",".join(missing_apps))
    apps = [a for a in apps if a not in missing_apps]
    extra = [a for a in live_apps() if a not in apps and a not in missing_apps] if snap else []
    if extra:
        print(f"  INFO  백업에 없던 Application: {','.join(extra)} (판정에 넣지 않음)")
    missing = [f"hpa/{h}" for h, o in st["hpa"].items() if o is None] + [f"deploy/{d}" for d, o in st["deploy"].items() if o is None]
    if missing or "root" not in apps or CHILD not in apps:
        add("필수 객체가 있음 (Phase 1 상태 검사)", False, ",".join(missing) or "root/parking-guardian Application 없음")
        for name, good, detail in res:
            print(f"  {'PASS' if good else 'FAIL'}  {name}  {detail}")
        print("=> 실패 항목 있음 (비교 구성에서는 p2-* 상태 검사를 쓴다)")
        return False

    rows = pod_rows(st["pods"])
    by_app = {a: [r for r in rows.values() if r["app"] == a] for a in PODS_EXPECT}
    for a, n in PODS_EXPECT.items():
        ready = [r for r in by_app[a] if r["phase"] == "Running" and r["ready"]]
        add(f"Pod {a}: Running+Ready {n}개", len(ready) == n and len(by_app[a]) == n, f"{len(ready)}/{len(by_app[a])}")
    add("Pending/비정상 Pod 없음", all(r["phase"] == "Running" for r in rows.values()), "")
    eps = [e for sl in st["endpoints"] for e in sl.get("endpoints", [])]
    add("inference Service 엔드포인트 ready 2개", len([e for e in eps if e["conditions"].get("ready")]) == 2, f"{len(eps)}개")
    apps_spec = {a: st["apps"][a]["spec"].get("syncPolicy", {}).get("automated") for a in apps}
    sync = {a: (st["apps"][a]["status"]["sync"]["status"], st["apps"][a]["status"]["health"]["status"]) for a in apps}
    hp = {h: (st["hpa"][h]["spec"]["minReplicas"], st["hpa"][h]["spec"]["maxReplicas"],
              st["hpa"][h]["status"].get("currentReplicas"), st["hpa"][h]["status"].get("desiredReplicas")) for h in HPAS}

    if expect in ("baseline", "restored"):
        add("root 자동 동기화 켜짐 (prune+selfHeal)", apps_spec["root"] == AUTO, str(apps_spec["root"]))
        add("parking-guardian 자동 동기화 켜짐", apps_spec[CHILD] == AUTO, str(apps_spec[CHILD]))
        for h in HPAS:
            add(f"HPA {h}: min 2 / max 3", hp[h][:2] == (2, 3), str(hp[h]))
            add(f"HPA {h}: 현재=목표=2", hp[h][2] == 2 and hp[h][3] == 2, str(hp[h]))
        add(f"Application {len(apps)}개 모두 Synced/Healthy", all(v == ("Synced", "Healthy") for v in sync.values()), str(sync))
    if expect == "fixed":
        add("root 자동 동기화 꺼짐", apps_spec["root"] is None, str(apps_spec["root"]))
        add("parking-guardian 자동 동기화 꺼짐", apps_spec[CHILD] is None, str(apps_spec[CHILD]))
        for other in [a for a in apps if a not in ("root", CHILD)]:
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
            "2. 최대 6분 동안 15초마다 status --expect restored: root·자식 automated, HPA 2/3, 백업의 Application 전부 Synced/Healthy,",
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
    log("복구 " + ("완료" if ok else "실패 - 보고하고 사용자 판단을 기다린다"))
    return 0 if ok else 4


# ====================================================================== Phase 2 비교 구성 (2026-11-04 보완)
# 비교 구성 = Git(config 리포)에 comparison-mode 패치가 반영된 상태: inference 가 없고 Worker(KEDA) min 1 / max 3.
# 이 도구는 Git 을 바꾸지 않는다. 측정 고정(p2-pause, p2-workers)과 복구(p2-restore)만 한다.
PAUSE_KEY = "autoscaling.keda.sh/paused-replicas"
P2_PODS = ("backend", "frontend", "relay", "worker", "inference")
WORKER_MAX = 3          # 장비 3대 = FIFO 그룹 3개 -> 동시 처리 한계. 노드 자리도 3개(비교 구성)
SO_NAME = "worker"


def read_p2_state():
    def items(ns, kind):
        return {i["metadata"]["name"]: i for i in kj("-n", ns, "get", kind)["items"]}
    return {"apps": items(ARGO_NS, "app"), "hpa": items(APP_NS, "hpa"), "deploy": items(APP_NS, "deploy"),
            "so": items(APP_NS, "scaledobject"), "pods": kj("-n", APP_NS, "get", "pods")["items"]}


def p2_rows(pods):
    rows = {}
    for p in pods:
        app = p["metadata"].get("labels", {}).get("app.kubernetes.io/name", "")
        if app not in P2_PODS:
            continue
        cs = p["status"].get("containerStatuses", [])
        rows[p["metadata"]["name"]] = {"app": app, "phase": p["status"].get("phase"), "deleting": bool(p["metadata"].get("deletionTimestamp")),
                                       "ready": all(c.get("ready") for c in cs) if cs else False,
                                       "restarts": sum(c.get("restartCount", 0) for c in cs)}
    return rows


def evaluate_p2(expect, st, workers=None):
    """순수 판정: (이름, 통과, 설명) 목록. st 는 read_p2_state() 의 결과."""
    res = []

    def add(name, ok, detail=""):
        res.append((name, bool(ok), detail))
    apps, hpa, dep, so, rows = st["apps"], st["hpa"], st["deploy"], st["so"], p2_rows(st["pods"])
    live = [r for r in rows.values() if not r["deleting"]]
    ready = lambda a: [r for r in live if r["app"] == a and r["phase"] == "Running" and r["ready"]]  # noqa: E731
    count = lambda a: [r for r in live if r["app"] == a]  # noqa: E731
    add("root · parking-guardian Application 이 있음", "root" in apps and CHILD in apps, ",".join(sorted(apps)))
    if "root" not in apps or CHILD not in apps:
        return res
    auto = {a: o["spec"].get("syncPolicy", {}).get("automated") for a, o in apps.items()}
    sync = {a: (o["status"]["sync"]["status"], o["status"]["health"]["status"]) for a, o in apps.items()}
    add("모든 Application Healthy", all(v[1] == "Healthy" for v in sync.values()), str(sync))
    add("Pending/비정상 Pod 없음", all(r["phase"] in ("Running", "Succeeded") for r in live), "")
    for a, n in (("backend", 2), ("frontend", 1), ("relay", 1)):
        add(f"Pod {a}: Running+Ready {n}개", len(ready(a)) == n and len(count(a)) == n, f"{len(ready(a))}/{len(count(a))}")
    worker_so = so.get(SO_NAME)
    conds = {c["type"]: c["status"] for c in (worker_so or {}).get("status", {}).get("conditions", [])}
    add("ScaledObject worker 있음, Ready=True", worker_so is not None and conds.get("Ready") == "True", str(conds))
    ann = (worker_so or {}).get("metadata", {}).get("annotations", {}).get(PAUSE_KEY)
    smin, smax = (worker_so or {}).get("spec", {}).get("minReplicaCount"), (worker_so or {}).get("spec", {}).get("maxReplicaCount")
    wdep = dep.get("worker")
    add("worker Deployment 있음", wdep is not None, "")

    if expect == "p2-baseline":
        add("root·parking-guardian 자동 동기화 켜짐", auto["root"] == AUTO and auto[CHILD] == AUTO, f"{auto['root']} / {auto[CHILD]}")
        add("Application 전부 Synced", all(v[0] == "Synced" for v in sync.values()), str(sync))
        add("inference Deployment 있음, Pod 2개 Ready", "inference" in dep and len(ready("inference")) == 2 and len(count("inference")) == 2,
            f"{len(ready('inference'))}/{len(count('inference'))}")
        add("HPA inference min 2 / max 3", "inference" in hpa and (hpa["inference"]["spec"]["minReplicas"], hpa["inference"]["spec"]["maxReplicas"]) == (2, 3), "")
        add("HPA backend min 2 / max 3", "backend" in hpa and (hpa["backend"]["spec"]["minReplicas"], hpa["backend"]["spec"]["maxReplicas"]) == (2, 3), "")
        add("ScaledObject min 0 / max 1 (공존 구성)", (smin, smax) == (0, 1), f"{smin}/{smax}")
        add("일시정지 어노테이션 없음", ann is None, str(ann))
    elif expect == "p2-comparison":
        add("root·parking-guardian 자동 동기화 켜짐", auto["root"] == AUTO and auto[CHILD] == AUTO, f"{auto['root']} / {auto[CHILD]}")
        add("Application 전부 Synced", all(v[0] == "Synced" for v in sync.values()), str(sync))
        add("inference Deployment·HPA·Pod 없음 (비교 구성)", "inference" not in dep and "inference" not in hpa and not count("inference"),
            f"deploy={'inference' in dep} hpa={'inference' in hpa} pods={len(count('inference'))}")
        add("HPA backend min 2 / max 3", "backend" in hpa and (hpa["backend"]["spec"]["minReplicas"], hpa["backend"]["spec"]["maxReplicas"]) == (2, 3), "")
        add("ScaledObject min 1 / max 3 (비교 구성)", (smin, smax) == (1, WORKER_MAX), f"{smin}/{smax}")
        add("일시정지 어노테이션 없음", ann is None, str(ann))
        add("Worker Pod Ready 1~3개", 1 <= len(ready("worker")) <= WORKER_MAX, f"{len(ready('worker'))}/{len(count('worker'))}")
    elif expect == "p2-fixed":
        add("root·parking-guardian 자동 동기화 꺼짐", auto["root"] is None and auto[CHILD] is None, f"{auto['root']} / {auto[CHILD]}")
        others = [a for a in apps if a not in ("root", CHILD)]
        add("나머지 Application 자동 동기화는 그대로", all(auto[a] is not None for a in others), ",".join(a for a in others if auto[a] is None))
        add("inference Deployment·Pod 없음 (비교 구성)", "inference" not in dep and not count("inference"), "")
        add("HPA backend min 2 / max 2", "backend" in hpa and (hpa["backend"]["spec"]["minReplicas"], hpa["backend"]["spec"]["maxReplicas"]) == (2, 2), "")
        add("ScaledObject min 1 / max 3", (smin, smax) == (1, WORKER_MAX), f"{smin}/{smax}")
        if workers is not None:
            wrep = (wdep or {}).get("spec", {}).get("replicas")
            add(f"Worker 고정 {workers}개: paused-replicas={workers}", ann == str(workers), str(ann))
            add(f"worker Deployment replicas={workers}, Pod {workers}개 Ready", wrep == workers and len(ready("worker")) == workers and len(count("worker")) == workers,
                f"replicas={wrep} ready={len(ready('worker'))}/{len(count('worker'))}")
        add("parking-guardian 은 OutOfSync 여도 Healthy 면 정상(HPA 가 Git 과 다름)", sync[CHILD][1] == "Healthy", str(sync[CHILD]))
    else:
        raise ValueError(expect)
    return res


def check_p2(expect, workers=None):
    res = evaluate_p2(expect, read_p2_state(), workers)
    ok = True
    for name, good, detail in res:
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}  {detail}")
    print("=> " + ("전부 통과" if ok else "실패 항목 있음"))
    return ok


def p2_pause(apply):
    plan = ["0. 사전: status --expect p2-comparison 이 전부 PASS 여야 한다 (비교 구성이 Git 에 반영되어 있어야 함, 아니면 중단)",
            "1. snapshot 으로 백업 저장 (Phase 2 객체 포함)",
            "2. root 와 parking-guardian 의 spec.syncPolicy.automated 제거",
            "3. HPA backend 를 min=max=2 로 패치 (inference 는 없으므로 건드리지 않는다)",
            "4. 60초 뒤 status --expect p2-fixed 로 확인"]
    print("\n".join(plan))
    if not apply:
        print("\n(--apply 없음: 아무것도 변경하지 않았다)")
        return 0
    if not check_p2("p2-comparison"):
        log("사전 점검 실패: 변경하지 않고 종료")
        return 2
    snap = snapshot()
    log(f"백업: {snap}  <- 복구에 이 경로가 필요하다")
    log("root 자동 동기화 제거"); patch_app("root", {"spec": {"syncPolicy": {"automated": None}}})
    log("parking-guardian 자동 동기화 제거"); patch_app(CHILD, {"spec": {"syncPolicy": {"automated": None}}})
    log("HPA backend -> min=max=2"); patch_hpa("backend", 2, 2)
    log("60초 관찰"); time.sleep(60)
    ok = check_p2("p2-fixed")
    print(f"\nWorker 고정: python loadtest/tools/p3.py p2-workers N --apply   /   복구: p2-restore --target comparison --apply")
    return 0 if ok else 3


def p2_workers(n, apply, release, wait=240):
    if release:
        plan = [f"ScaledObject {SO_NAME} 의 {PAUSE_KEY} 어노테이션 제거 (KEDA 가 다시 큐 길이로 Worker 수를 정한다)"]
    else:
        plan = [f"0. 사전: root·parking-guardian 자동 동기화가 꺼져 있어야 한다(p2-pause 이후). 1 <= N <= {WORKER_MAX}",
                f"1. ScaledObject {SO_NAME} 에 {PAUSE_KEY}={n} 어노테이션 (KEDA 가 Worker 를 정확히 {n}개로 고정)",
                f"2. 최대 {wait}초 동안 worker Deployment replicas={n}, Pod {n}개 Ready 를 기다리고 p2-fixed --workers {n} 으로 확인"]
    print("\n".join(plan))
    if not apply:
        print("\n(--apply 없음: 아무것도 변경하지 않았다)")
        return 0
    if release:
        kube("-n", APP_NS, "annotate", "scaledobject", SO_NAME, PAUSE_KEY + "-")
        log("어노테이션 제거됨")
        return 0
    if not (1 <= n <= WORKER_MAX):
        log(f"N 은 1~{WORKER_MAX}")
        return 2
    apps = read_p2_state()["apps"]
    if any(apps.get(a, {}).get("spec", {}).get("syncPolicy", {}).get("automated") for a in ("root", CHILD)):
        log("중단: 자동 동기화가 켜져 있다. 어노테이션은 Git 에 없는 값이라 되돌려질 수 있다 - 먼저 p2-pause")
        return 2
    kube("-n", APP_NS, "annotate", "scaledobject", SO_NAME, f"{PAUSE_KEY}={n}", "--overwrite")
    deadline, ok = time.time() + wait, False
    while time.time() < deadline:
        time.sleep(15)
        log("검사"); ok = check_p2("p2-fixed", n)
        if ok:
            break
    log("Worker 고정 " + ("완료" if ok else "실패 - 보고하고 사용자 판단을 기다린다 (Pending 이면 CPU 자리 부족: R7)"))
    return 0 if ok else 4


def p2_restore(target, apply, observe, wait=360):
    plan = [f"1. ScaledObject {SO_NAME} 의 {PAUSE_KEY} 어노테이션이 있으면 제거",
            "2. kubectl apply -f <config>/argocd/root.yaml  (root 자동 동기화 복구 -> 자식 앱·HPA 는 Git 대로 돌아온다)",
            f"3. 최대 {wait}초 동안 15초마다 status --expect p2-{target} 로 확인",
            "4. --observe SEC: 복구 뒤 SEC 초 동안 1분마다 다시 검사"]
    if target == "baseline":
        plan.append("   (baseline 은 Git 의 비교 구성 패치를 사용자가 먼저 되돌려 푸시한 뒤에만 통과한다. inference 롤아웃에는 1000m 자리가 필요하다)")
    print("\n".join(plan))
    if not apply:
        print("\n(--apply 없음: 아무것도 변경하지 않았다)")
        return 0
    so = read_p2_state()["so"].get(SO_NAME)
    if so and PAUSE_KEY in so["metadata"].get("annotations", {}):
        kube("-n", APP_NS, "annotate", "scaledobject", SO_NAME, PAUSE_KEY + "-")
        log("일시정지 어노테이션 제거")
    log("root.yaml 적용"); print(kube("apply", "-f", str(CONFIG / "argocd" / "root.yaml")).strip())
    deadline, ok = time.time() + wait, False
    while time.time() < deadline:
        time.sleep(15)
        log("검사"); ok = check_p2(f"p2-{target}")
        if ok:
            break
    if ok and observe:
        end = time.time() + observe
        while ok and time.time() < end:
            time.sleep(60); log("관찰"); ok = check_p2(f"p2-{target}")
    log("복구 " + ("완료" if ok else "실패 - 보고하고 사용자 판단을 기다린다"))
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
    s = sub.add_parser("status"); s.add_argument("--expect", required=True, choices=["baseline", "fixed", "restored", "p2-baseline", "p2-comparison", "p2-fixed"]); s.add_argument("--snap")
    s.add_argument("--replaced", default=""); s.add_argument("--workers", type=int)
    s = sub.add_parser("p2-pause"); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("p2-workers"); s.add_argument("n", type=int, nargs="?", default=0); s.add_argument("--apply", action="store_true"); s.add_argument("--release", action="store_true")
    s = sub.add_parser("p2-restore"); s.add_argument("--target", required=True, choices=["comparison", "baseline"]); s.add_argument("--apply", action="store_true")
    s.add_argument("--observe", type=int, default=0)
    s = sub.add_parser("pause"); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("restore"); s.add_argument("--snap", required=True); s.add_argument("--apply", action="store_true")
    s.add_argument("--emergency", action="store_true"); s.add_argument("--observe", type=int, default=0); s.add_argument("--replaced", default="")
    a = ap.parse_args()
    if a.cmd == "snapshot":
        snapshot(a.out); return 0
    if a.cmd == "status":
        if a.expect.startswith("p2-"):
            return 0 if check_p2(a.expect, a.workers) else 1
        return 0 if check(a.expect, a.snap, parse_replaced(a.replaced)) else 1
    if a.cmd == "p2-pause":
        return p2_pause(a.apply)
    if a.cmd == "p2-workers":
        if not a.release and a.n == 0:
            sys.exit("p2-workers 는 N(1~%d) 또는 --release 가 필요합니다" % WORKER_MAX)
        return p2_workers(a.n, a.apply, a.release)
    if a.cmd == "p2-restore":
        return p2_restore(a.target, a.apply, a.observe)
    if a.cmd == "pause":
        return pause(a.apply)
    return restore(a.snap, a.apply, a.emergency, a.observe, parse_replaced(a.replaced))


if __name__ == "__main__":
    sys.exit(main())
