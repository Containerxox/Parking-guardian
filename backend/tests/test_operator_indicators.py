"""Operator dashboard indicators: left-unattended violations, silent machines, undelivered notifications."""
import io

from .conftest import API, JPEG_BYTES

GANGNAM = "RPI-A-GANGNAM-01"
PANGYO = "RPI-A-PANGYO-01"
SUWON = "RPI-A-SUWON-01"


def test_operator_indicators(app, client, auth, upload, ids, monkeypatch):
    root = auth("super@pg.local")

    def summary(email="super@pg.local"):
        return client.get(f"{API}/dashboard/summary", headers=auth(email)).get_json()

    start = summary()
    assert (start["stale_violations"], start["failed_notifications"]) == (0, 0)
    assert start["stale_violation_hours"] == 24
    assert start["offline_machines"] == 5  # no seeded machine has sent a signal yet

    # a machine that just uploaded is online; one taken out of service is not counted as offline
    upload(GANGNAM, "permitted.jpg")
    assert summary()["offline_machines"] == 4
    machines = client.get(f"{API}/machines", headers=root).get_json()["items"]
    suwon_machine = next(m for m in machines if m["serial_number"] == SUWON)
    client.patch(f"{API}/machines/{suwon_machine['id']}", headers=root, json={"status": "INACTIVE"})
    assert summary()["offline_machines"] == 3

    # left unattended: detected long ago and still open. A fresh one does not count.
    old = upload(GANGNAM, detected_at="2026-01-01 09:00:00").get_json()["violation_id"]
    upload(GANGNAM)
    assert summary()["stale_violations"] == 1
    assert summary("kim@a.com")["stale_violations"] == 1
    assert summary("choi@b.com")["stale_violations"] == 0

    # notification not delivered, case 1: publishing fails
    def boom(violation_id):
        raise RuntimeError("queue is down")

    with monkeypatch.context() as patch:
        patch.setattr(app.extensions["pg_publisher"], "publish_violation_detected", boom)
        failed = upload(PANGYO).get_json()["violation_id"]

    # case 2: a parking lot nobody is assigned to
    lot = client.post(f"{API}/parking-lots", headers=root, json={
        "name": "담당자 없는 주차장", "address": "x", "company_id": ids["companies"]["B회사"]}).get_json()
    key = client.post(f"{API}/machines", headers=root, json={
        "parking_lot_id": lot["id"], "serial_number": "RPI-B-NEW-01", "name": "Camera-01"}).get_json()["api_key"]
    unassigned = client.post(f"{API}/device/detections", headers={"X-Device-Key": key},
                             data={"file": (io.BytesIO(JPEG_BYTES), "violation.jpg")},
                             content_type="multipart/form-data")
    assert unassigned.status_code == 201

    assert summary()["failed_notifications"] == 2
    assert summary("kim@a.com")["failed_notifications"] == 1
    assert summary("choi@b.com")["failed_notifications"] == 1

    # handling a violation clears it from the indicators
    kim = auth("kim@a.com")
    assert client.post(f"{API}/violations/{old}/resolve", headers=kim, json={}).status_code == 200
    assert client.post(f"{API}/violations/{failed}/resolve", headers=kim, json={}).status_code == 200
    after = summary()
    assert (after["stale_violations"], after["failed_notifications"]) == (0, 1)
