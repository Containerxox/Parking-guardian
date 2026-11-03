"""P0-b: Wi-Fi 안정성 프로브 (기본 900초). 1초마다 새 HTTPS 연결로 readyz + 게이트웨이 ping, 5초마다 Wi-Fi 상태와 PC CPU·메모리.

    python loadtest/tools/p0b_run.py [초]      기본 900

기준 (wifi-network-plan.md 5절, 측정 전에 고정):
  N1(필수) 성공 100%(실패 ≤ 0.5%), TCP 연결 p99 ≤ 150 ms, 응답 p99 ≤ 300 ms, ping 손실 ≤ 1%, 채널·대역 변경/끊김 0
  N2(진단) 송신 PHY(참고 ≥ 54 Mbps), 신호(참고 ≥ 60%), 채널·대역 변경 — 단독으로 실패 판정하지 않는다
  P1(필수) PC 전체 CPU < 80% (k6 는 돌리지 않는다)
읽기만 한다: 인증 없는 GET /api/v1/readyz 와 로컬 ping. SSID/BSSID 는 기록하지 않는다.
"""
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def pct(a, q):
    a = sorted(a)
    return a[min(len(a) - 1, int(len(a) * q))] if a else None


def wifi_state():
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    st = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        low = k.lower()
        if low in ("ssid", "bssid") or low.startswith(("name", "description", "guid", "physical", "이름", "설명", "물리")):
            continue
        st[k] = v
    # 로캘 독립 추출
    def pick(*names):
        for k, v in st.items():
            if any(n.lower() in k.lower() for n in names):
                return v
        return None
    return {"state": pick("state", "상태"), "band": pick("band", "대역", "밴드"), "channel": pick("channel", "채널"),
            "radio": pick("radio type", "무선 종류"), "rx": pick("receive rate", "수신 속도"),
            "tx": pick("transmit rate", "전송 속도"), "signal": pick("signal", "신호")}


def num(s):
    m = re.search(r"[0-9.]+", s or "")
    return float(m.group(0)) if m else None


def main():
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    host = sh(["kubectl", "-n", "parking-guardian", "get", "ingress", "-o", "jsonpath={.items[0].spec.rules[0].host}"]).stdout.strip()
    url = f"https://{host}/api/v1/readyz"
    gw = sh(["powershell", "-NoProfile", "-Command",
             "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -First 1).NextHop"]).stdout.strip()
    folder = RESULTS / f"{dt.date.today().isoformat()}-p0b-wifi{sys.argv[2] if len(sys.argv) > 2 else ''}"
    if folder.exists():
        sys.exit(f"{folder} 가 이미 있다")
    folder.mkdir(parents=True)
    t0 = dt.datetime.now(dt.timezone.utc)
    print(f"[{t0:%H:%M:%S}Z] P0-b 시작 {dur}초", flush=True)

    mon = subprocess.Popen(["powershell", "-NoProfile", "-File", str(REPO / "loadtest/tools/monitor-pc.ps1"), "-Out",
                            str(folder / "pc-monitor.csv"), "-IntervalSec", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wifi_rows, wifi_stop = [], threading.Event()

    def wifi_loop():
        while not wifi_stop.is_set():
            s = wifi_state(); s["t"] = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"); wifi_rows.append(s)
            wifi_stop.wait(5)
    th = threading.Thread(target=wifi_loop, daemon=True); th.start()

    rows, consecutive_fail, aborted = [], 0, None
    start = time.time()
    n = 0
    while time.time() - start < dur:
        tick = start + n
        n += 1
        c = subprocess.Popen(["curl", "-s", "-o", os.devnull, "-m", "5", "-w", "%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} %{time_total}", url],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p = subprocess.Popen(["ping", "-n", "1", "-w", "1000", gw], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="cp949", errors="replace")
        co, _ = c.communicate(); po, _ = p.communicate()
        parts = co.split()
        ok = len(parts) == 5 and parts[0] == "200"
        pm = re.search(r"(?:time|시간)\s*[=<]\s*(\d+)\s*ms", po)
        rows.append({"t": dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), "http": parts[0] if parts else "000",
                     "dns_ms": float(parts[1]) * 1000 if len(parts) == 5 else None, "connect_ms": float(parts[2]) * 1000 if len(parts) == 5 else None,
                     "tls_ms": float(parts[3]) * 1000 if len(parts) == 5 else None, "total_ms": float(parts[4]) * 1000 if len(parts) == 5 else None,
                     "ok": ok, "ping_ms": int(pm.group(1)) if pm else None})
        consecutive_fail = 0 if ok else consecutive_fail + 1
        if consecutive_fail >= 10:
            aborted = "HTTPS 연속 10회 실패 — 중단"
            break
        time.sleep(max(0, tick + 1 - time.time()))
    wifi_stop.set(); th.join(timeout=8); mon.terminate()
    t1 = dt.datetime.now(dt.timezone.utc)

    # ---- 저장
    with open(folder / "samples.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with open(folder / "wifi.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "state", "band", "channel", "radio", "rx", "tx", "signal"]); w.writeheader(); w.writerows(wifi_rows)

    okr = [r for r in rows if r["ok"]]
    conn = [r["connect_ms"] for r in okr]; tot = [r["total_ms"] for r in okr]; pings = [r["ping_ms"] for r in rows]
    ping_ok = [x for x in pings if x is not None]
    fail = len(rows) - len(okr)
    ping_loss = (len(rows) - len(ping_ok)) / len(rows)
    chan = sorted({(w.get("band"), w.get("channel")) for w in wifi_rows})
    states = sorted({w.get("state") for w in wifi_rows})
    tx = [num(w["tx"]) for w in wifi_rows if num(w["tx"]) is not None]
    rx = [num(w["rx"]) for w in wifi_rows if num(w["rx"]) is not None]
    sig = [num(w["signal"]) for w in wifi_rows if num(w["signal"]) is not None]
    pcm = [r for r in csv.reader((folder / "pc-monitor.csv").read_text(encoding="utf-8-sig").splitlines()) if r and not r[0].startswith("#")][1:]
    cpu = [float(r[2]) for r in pcm]; mem = [float(r[3]) for r in pcm]
    res = {
        "start_utc": t0.isoformat(timespec="seconds"), "end_utc": t1.isoformat(timespec="seconds"), "samples": len(rows), "aborted": aborted,
        "https": {"success": len(okr), "fail": fail, "fail_rate": round(fail / len(rows), 4),
                  "connect_ms": {"p50": round(pct(conn, .5), 1), "p95": round(pct(conn, .95), 1), "p99": round(pct(conn, .99), 1), "max": round(max(conn), 1)} if conn else None,
                  "total_ms": {"p50": round(pct(tot, .5), 1), "p95": round(pct(tot, .95), 1), "p99": round(pct(tot, .99), 1), "max": round(max(tot), 1)} if tot else None,
                  "jitter_total_stdev_ms": round(statistics.pstdev(tot), 1) if len(tot) > 1 else None},
        "gateway_ping": {"loss": round(ping_loss, 4), "avg_ms": round(statistics.mean(ping_ok), 1) if ping_ok else None, "p99_ms": pct(ping_ok, .99), "max_ms": max(ping_ok) if ping_ok else None},
        "wifi": {"states_seen": states, "band_channel_seen": chan, "tx_phy_mbps": {"min": min(tx), "median": statistics.median(tx), "max": max(tx)} if tx else None,
                 "rx_phy_mbps": {"min": min(rx), "median": statistics.median(rx), "max": max(rx)} if rx else None,
                 "signal_pct": {"min": min(sig), "median": statistics.median(sig), "max": max(sig)} if sig else None, "wifi_samples": len(wifi_rows)},
        "pc": {"cpu_pct_avg": round(statistics.mean(cpu), 1) if cpu else 0, "cpu_pct_max": max(cpu) if cpu else 0, "free_mem_mb_min": min(mem) if mem else None, "samples": len(cpu)},
    }
    h, g, wf = res["https"], res["gateway_ping"], res["wifi"]
    N1 = {
        "성공률(실패 ≤ 0.5%)": h["fail_rate"] <= 0.005,
        "TCP 연결 p99 ≤ 150 ms": bool(h["connect_ms"]) and h["connect_ms"]["p99"] <= 150,
        "응답 p99 ≤ 300 ms": bool(h["total_ms"]) and h["total_ms"]["p99"] <= 300,
        "ping 손실 ≤ 1%": g["loss"] <= 0.01,
        "채널·대역 변경/끊김 0": len(chan) == 1 and all((x or "").lower() in ("connected", "연결됨") for x in states),
    }
    N2 = {"송신 PHY 최소 ≥ 54": bool(wf["tx_phy_mbps"]) and wf["tx_phy_mbps"]["min"] >= 54,
          "신호 최소 ≥ 60%": bool(wf["signal_pct"]) and wf["signal_pct"]["min"] >= 60}
    P1 = {"PC 전체 CPU 최대 < 80%": res["pc"]["cpu_pct_max"] < 80}
    res["N1"], res["N2_diagnostic"], res["P1"] = N1, N2, P1
    res["N1_pass"], res["P1_pass"] = all(N1.values()), all(P1.values())
    res["verdict"] = "통과" if (res["N1_pass"] and res["P1_pass"] and not aborted) else "실패"
    (folder / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    md = f"""# P0-b Wi-Fi 안정성 프로브 — {dt.date.today().isoformat()} (로컬 전용)
- 시간 {res['start_utc']} ~ {res['end_utc']} UTC, 표본 {res['samples']}개 (1초 간격, 새 HTTPS 연결 readyz + 게이트웨이 ping), Wi-Fi 상태 {wf['wifi_samples']}회(5초)
- 판정: **{res['verdict']}** (N1 {'통과' if res['N1_pass'] else '실패'}, P1 {'통과' if res['P1_pass'] else '실패'}; N2 는 진단 지표) {aborted or ''}
- HTTPS: 성공 {h['success']}/{res['samples']} (실패율 {h['fail_rate']:.2%}), 연결 ms {h['connect_ms']}, 응답 ms {h['total_ms']}, 지터(표준편차) {h['jitter_total_stdev_ms']} ms
- 게이트웨이 ping: 손실 {g['loss']:.2%}, 평균 {g['avg_ms']} ms, p99 {g['p99_ms']} ms, 최대 {g['max_ms']} ms
- Wi-Fi: 상태 {wf['states_seen']}, 대역/채널 {wf['band_channel_seen']}, 송신 PHY {wf['tx_phy_mbps']}, 수신 PHY {wf['rx_phy_mbps']}, 신호 % {wf['signal_pct']}
- PC: CPU 평균 {res['pc']['cpu_pct_avg']}% 최대 {res['pc']['cpu_pct_max']}%, 여유 메모리 최소 {res['pc']['free_mem_mb_min']} MB
- N1 항목: {N1}
- N2(진단): {N2}
- P1: {P1}
"""
    (folder / "conditions.md").write_text(md, encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(folder.iterdir()) if p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    for p in folder.iterdir():
        os.chmod(p, 0o444)
    print(md, flush=True)
    sys.exit(0 if res["verdict"] == "통과" else 2)


if __name__ == "__main__":
    main()
