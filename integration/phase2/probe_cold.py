import time, uuid, logging
from app import create_app, adapters
app = create_app()
logging.getLogger("app").setLevel(logging.WARNING)
def step(name, fn):
    t = time.time()
    try:
        fn(); r = "ok"
    except Exception as e:
        r = type(e).__name__
    print(f"{time.strftime('%H:%M:%S')} {name} {r} {time.time()-t:.1f}s", flush=True)
with app.app_context():
    q, st = adapters.queue(), adapters.storage()
    for i in range(8):
        step("s3 save", lambda: st.save(f"probe/{uuid.uuid4()}", b"x" * 1000))
        step("sqs send", lambda: q.send(uuid.uuid4(), 1, fast=True))
        time.sleep(3)
