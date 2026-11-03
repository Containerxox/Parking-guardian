import json, shutil, tempfile, requests
from pathlib import Path
from harness import *
from outbox import Outbox, OutboxFull, OutboxHealth
guard()

root = Path(tempfile.mkdtemp(prefix="pg-outbox-")) / "outbox"
ob = Outbox(root, time.time, max_items=4)
health = OutboxHealth(root / "health.json", time.time)
s = new_session()
dc("stop", "api")
seq = 0
stored, refused = [], []
def put(event, reserve=True):
    global seq
    seq += 1
    it = item(s, seq, event)
    try:
        ob.put(it, reserve_for_left=reserve); stored.append((seq, event))
    except OutboxFull as e:
        health.record_failure(event, seq, "full_" + e.reason, str(e), left_of_known_session=(event == "left"))
        refused.append((seq, event, e.reason))
# API is down: nothing can be sent, the outbox fills up
for ev in ("arrived", "confirm", "refresh", "confirm", "confirm", "confirm"):
    put(ev)
put("left", reserve=False)   # the reserved slot is for exactly this
print("F8 stored", stored, "refused", refused)
try:
    send(ob.items()[0]); print("unexpected send")
except requests.RequestException as e:
    print("   API down ->", type(e).__name__)
snap = json.loads((root / "health.json").read_text(encoding="utf-8"))
print("   health.json", {k: snap[k] for k in snap if k in ("failures", "left_of_known_session_refused", "given_up")})
# "restart" the device process: health survives
h2 = OutboxHealth(root / "health.json", time.time)
print("   after restart: total failures kept:", h2.snapshot().get("failures") == snap.get("failures"))
dc("start", "api")
wait(lambda: requests.get("http://127.0.0.1:15000/readyz", timeout=2).status_code == 200, 90, what="api")
expected = len(ob.items())
for it in ob.items():       # drain oldest first, remove on accept/duplicate (what the runner does)
    r = send(it)
    assert r.kind in ("accepted", "duplicate"), r
    ob.remove(it)
wait(lambda: len(job_rows(s)) == expected and all(r[2] in ("done", "stale") for r in job_rows(s)), 150, what="drained")
print("   drained:", [(r[0], r[1], r[2]) for r in job_rows(s)], "outbox left:", len(ob.items()))
shutil.rmtree(root.parent, ignore_errors=True)
