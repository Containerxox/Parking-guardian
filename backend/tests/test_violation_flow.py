"""Device upload -> violation -> notification -> handling, plus failure isolation."""
import io

from app.cli import DEMO_PASSWORD

from .conftest import API, JPEG_BYTES

GANGNAM = "RPI-A-GANGNAM-01"   # assigned: kim
PANGYO = "RPI-A-PANGYO-01"     # assigned: kim, lee
SUWON = "RPI-A-SUWON-01"       # assigned: park


def _unread(client, auth, email):
    return client.get(f"{API}/notifications?unread=true", headers=auth(email)).get_json()


def test_login_me_refresh_logout(client):
    wrong = client.post(f"{API}/auth/login", json={"email": "kim@a.com", "password": "nope-nope"})
    assert wrong.status_code == 401 and wrong.get_json()["error"]["code"] == "invalid_credentials"

    login = client.post(f"{API}/auth/login", json={"email": "KIM@a.com", "password": DEMO_PASSWORD})
    assert login.status_code == 200
    body = login.get_json()
    assert body["admin"]["role"] == "COMPANY_ADMIN" and body["admin"]["company_name"] == "A회사"

    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.get_json()["admin"]["email"] == "kim@a.com"
    assert client.get(f"{API}/auth/me").status_code == 401

    refreshed = client.post(f"{API}/auth/refresh")  # the test client keeps the refresh cookie
    assert refreshed.status_code == 200 and refreshed.get_json()["access_token"]

    assert client.post(f"{API}/auth/logout").status_code == 200
    assert client.post(f"{API}/auth/refresh").status_code == 401


def test_device_needs_a_valid_key(client):
    files = {"file": (io.BytesIO(JPEG_BYTES), "violation.jpg")}
    assert client.post(f"{API}/device/detections", data=files, content_type="multipart/form-data").status_code == 401
    files = {"file": (io.BytesIO(JPEG_BYTES), "violation.jpg")}
    response = client.post(f"{API}/device/detections", data=files, content_type="multipart/form-data",
                           headers={"X-Device-Key": "pgk_wrong"})
    assert response.status_code == 401


def test_permitted_or_empty_space_stores_nothing(client, auth, upload):
    assert upload(GANGNAM, "permitted.jpg").get_json()["result"] == "permitted"
    assert upload(GANGNAM, "novehicle.jpg").get_json()["result"] == "no_vehicle"
    assert client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["total"] == 0
    # the upload still counts as a heartbeat
    machines = client.get(f"{API}/machines", headers=auth("kim@a.com")).get_json()["items"]
    assert next(m for m in machines if m["serial_number"] == GANGNAM)["online"] is True


def test_only_admins_assigned_to_the_lot_are_notified(client, auth, upload):
    response = upload(GANGNAM, zone_name="A-02", detected_at="2026-10-01 21:03:00")
    assert response.status_code == 201
    violation_id = response.get_json()["violation_id"]

    kim = _unread(client, auth, "kim@a.com")
    assert kim["unread_count"] == 1 and kim["items"][0]["violation_id"] == violation_id
    assert kim["items"][0]["zone_name"] == "A-02"
    for email in ("lee@a.com", "park@a.com", "choi@b.com"):
        assert _unread(client, auth, email)["unread_count"] == 0, email

    # viewing is company-wide: lee is not assigned to this lot but can still see the violation
    detail = client.get(f"{API}/violations/{violation_id}", headers=auth("lee@a.com")).get_json()
    assert detail["status"] == "NOTIFIED"
    assert detail["detected_at"] == "2026-10-01T12:03:00Z"  # 21:03 KST
    assert [h["action"] for h in detail["history"]] == ["DETECTED", "NOTIFIED"]

    # a lot with two assigned admins notifies both
    upload(PANGYO)
    assert _unread(client, auth, "kim@a.com")["unread_count"] == 2
    assert _unread(client, auth, "lee@a.com")["unread_count"] == 1

    # reading
    notification_id = _unread(client, auth, "lee@a.com")["items"][0]["id"]
    assert client.post(f"{API}/notifications/{notification_id}/read", headers=auth("kim@a.com")).status_code == 404
    assert client.post(f"{API}/notifications/{notification_id}/read", headers=auth("lee@a.com")).status_code == 200
    assert _unread(client, auth, "lee@a.com")["unread_count"] == 0
    assert client.post(f"{API}/notifications/read-all", headers=auth("kim@a.com")).get_json()["updated"] == 2


def test_handling_changes_status_and_keeps_the_record(client, auth, upload):
    violation_id = upload(SUWON).get_json()["violation_id"]
    park = auth("park@a.com")

    ack = client.post(f"{API}/violations/{violation_id}/acknowledge", headers=park)
    assert ack.status_code == 200 and ack.get_json()["status"] == "IN_PROGRESS"
    again = client.post(f"{API}/violations/{violation_id}/acknowledge", headers=park)
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "invalid_transition"

    done = client.post(f"{API}/violations/{violation_id}/resolve", headers=park,
                       json={"resolution_note": "안전신문고 신고 완료"})
    body = done.get_json()
    assert done.status_code == 200 and body["status"] == "RESOLVED"
    assert body["resolved_by_name"] == "박관리자" and body["resolved_at"] and body["resolution_note"]
    assert client.post(f"{API}/violations/{violation_id}/resolve", headers=park, json={}).status_code == 409

    # not deleted: it moved from the open list to the resolved list
    assert client.get(f"{API}/violations?status=open", headers=park).get_json()["total"] == 0
    resolved = client.get(f"{API}/violations?status=resolved", headers=park).get_json()
    assert [v["id"] for v in resolved["items"]] == [violation_id]

    # reopen is SUPER_ADMIN only and is recorded
    assert client.post(f"{API}/violations/{violation_id}/reopen", headers=park,
                       json={"message": "실수"}).status_code == 403
    reopened = client.post(f"{API}/violations/{violation_id}/reopen", headers=auth("super@pg.local"),
                           json={"message": "잘못 처리 완료됨"})
    assert reopened.status_code == 200 and reopened.get_json()["status"] == "IN_PROGRESS"
    assert reopened.get_json()["resolved_at"] is None

    history = client.get(f"{API}/violations/{violation_id}", headers=park).get_json()["history"]
    assert [h["action"] for h in history] == ["DETECTED", "NOTIFIED", "ACKNOWLEDGED", "RESOLVED", "REOPENED"]
    assert history[-1]["admin_name"] == "서비스 운영자" and history[-1]["previous_status"] == "RESOLVED"


def test_image_is_served_only_through_a_signed_url(client, auth, upload):
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    assert client.get(f"{API}/violations/{violation_id}/image-url").status_code == 401

    info = client.get(f"{API}/violations/{violation_id}/image-url", headers=auth("kim@a.com")).get_json()
    path = info["url"].split("localhost", 1)[-1]
    image = client.get(path)
    assert image.status_code == 200 and image.data == JPEG_BYTES and image.mimetype == "image/jpeg"
    assert client.get(f"{API}/images/not-a-token").status_code == 404


def test_notification_failure_does_not_fail_detection(app, client, auth, upload, monkeypatch):
    def boom(violation_id):
        raise RuntimeError("queue is down")

    monkeypatch.setattr(app.extensions["pg_publisher"], "publish_violation_detected", boom)
    response = upload(GANGNAM)
    assert response.status_code == 201

    detail = client.get(f"{API}/violations/{response.get_json()['violation_id']}", headers=auth("kim@a.com")).get_json()
    assert detail["status"] == "DETECTED" and detail["has_image"] is True
    assert [h["action"] for h in detail["history"]] == ["DETECTED", "NOTIFY_FAILED"]
    # the admin can still handle it without the notification
    assert client.post(f"{API}/violations/{detail['id']}/resolve", headers=auth("kim@a.com"), json={}).status_code == 200


def test_image_failure_keeps_the_violation(app, client, auth, upload, monkeypatch):
    def boom(key, data):
        raise OSError("disk full")

    monkeypatch.setattr(app.extensions["pg_storage"], "save", boom)
    response = upload(GANGNAM)
    assert response.status_code == 201

    detail = client.get(f"{API}/violations/{response.get_json()['violation_id']}", headers=auth("kim@a.com")).get_json()
    assert detail["image_status"] == "FAILED" and detail["has_image"] is False
    assert detail["status"] == "NOTIFIED"  # notification still went out
    assert "IMAGE_UPLOAD_FAILED" in [h["action"] for h in detail["history"]]


def test_inference_failure_returns_503_and_stores_nothing(app, client, auth, upload, monkeypatch):
    from app.adapters.inference import InferenceError

    def boom(image_bytes, filename=""):
        raise InferenceError("model not loaded")

    monkeypatch.setattr(app.extensions["pg_inference"], "analyze", boom)
    response = upload(GANGNAM)
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "inference_unavailable"
    assert client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["total"] == 0


def test_super_admin_manages_companies_and_admins(client, auth):
    root = auth("super@pg.local")
    created = client.post(f"{API}/companies", headers=root, json={"name": "C회사"})
    assert created.status_code == 201
    company_id = created.get_json()["id"]
    assert client.post(f"{API}/companies", headers=root, json={"name": "C회사"}).status_code == 409

    admin = client.post(f"{API}/admins", headers=root, json={
        "email": "new@c.com", "name": "신규", "password": "Passw0rd!", "company_id": company_id})
    assert admin.status_code == 201 and admin.get_json()["role"] == "COMPANY_ADMIN"

    # deactivating the company blocks its admins
    assert client.patch(f"{API}/companies/{company_id}", headers=root, json={"status": "INACTIVE"}).status_code == 200
    blocked = client.post(f"{API}/auth/login", json={"email": "new@c.com", "password": "Passw0rd!"})
    assert blocked.status_code == 403

    actions = [a["action"] for a in client.get(f"{API}/audit-logs", headers=root).get_json()["items"]]
    assert {"COMPANY_CREATED", "ADMIN_CREATED", "COMPANY_STATUS_CHANGED"} <= set(actions)


def test_machine_registration_returns_key_once_and_it_works(client, auth, ids):
    kim = auth("super@pg.local")  # registering devices is a service-operator task
    created = client.post(f"{API}/machines", headers=kim, json={
        "parking_lot_id": ids["lots"]["강남 주차장"], "serial_number": "RPI-NEW-01", "name": "Camera-02"})
    assert created.status_code == 201
    api_key = created.get_json()["api_key"]
    assert api_key.startswith("pgk_") and "api_key" not in created.get_json()["machine"]

    beat = client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": api_key})
    assert beat.status_code == 200

    machine_id = created.get_json()["machine"]["id"]
    rotated = client.post(f"{API}/machines/{machine_id}/rotate-key", headers=kim).get_json()["api_key"]
    assert client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": api_key}).status_code == 401
    assert client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": rotated}).status_code == 200


def test_assigned_filter_narrows_the_list_without_removing_company_wide_view(client, auth, upload):
    gangnam = upload(GANGNAM).get_json()["violation_id"]
    pangyo = upload(PANGYO).get_json()["violation_id"]
    suwon = upload(SUWON).get_json()["violation_id"]

    def listed(email, query=""):
        items = client.get(f"{API}/violations{query}", headers=auth(email)).get_json()["items"]
        return sorted(v["id"] for v in items)

    assert listed("lee@a.com", "?assigned=true") == [pangyo]
    assert listed("kim@a.com", "?assigned=true") == sorted([gangnam, pangyo])
    assert listed("park@a.com", "?assigned=true&status=open") == [suwon]
    # company-wide viewing is still there when the filter is not used
    assert listed("lee@a.com") == sorted([gangnam, pangyo, suwon])
    # the filter has no meaning for SUPER_ADMIN, who is assigned to nothing
    assert listed("super@pg.local", "?assigned=true") == sorted([gangnam, pangyo, suwon])
