"""Company A must never see or change company B's data."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import AdminParkingLot

from .conftest import API

GANGNAM = "RPI-A-GANGNAM-01"   # company A
B_HQ = "RPI-B-HQ-01"           # company B


def _violation_id(upload, serial):
    response = upload(serial)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["violation_id"]


def test_company_admin_cannot_call_super_admin_api(client, auth):
    assert client.get(f"{API}/companies", headers=auth("kim@a.com")).status_code == 403
    assert client.get(f"{API}/audit-logs", headers=auth("kim@a.com")).status_code == 403
    response = client.post(f"{API}/admins", headers=auth("kim@a.com"),
                           json={"email": "x@a.com", "name": "x", "password": "Passw0rd!", "company_id": 1})
    assert response.status_code == 403


def test_lists_only_contain_own_company(client, auth, upload, ids):
    a_violation = _violation_id(upload, GANGNAM)
    b_violation = _violation_id(upload, B_HQ)

    a_items = client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["items"]
    b_items = client.get(f"{API}/violations", headers=auth("choi@b.com")).get_json()["items"]
    assert [v["id"] for v in a_items] == [a_violation]
    assert [v["id"] for v in b_items] == [b_violation]

    for path in ("parking-lots", "machines", "admins"):
        items = client.get(f"{API}/{path}", headers=auth("kim@a.com")).get_json()["items"]
        assert items, path
        assert {item["company_id"] for item in items} == {ids["companies"]["A회사"]}, path

    # SUPER_ADMIN sees both, and can narrow by company
    all_items = client.get(f"{API}/violations", headers=auth("super@pg.local")).get_json()["items"]
    assert {v["id"] for v in all_items} == {a_violation, b_violation}
    only_b = client.get(f"{API}/violations?company_id={ids['companies']['B회사']}",
                        headers=auth("super@pg.local")).get_json()["items"]
    assert [v["id"] for v in only_b] == [b_violation]


def test_naming_another_company_is_403(client, auth, ids):
    other = ids["companies"]["B회사"]
    kim = auth("kim@a.com")
    assert client.get(f"{API}/violations?company_id={other}", headers=kim).status_code == 403
    assert client.get(f"{API}/parking-lots?company_id={other}", headers=kim).status_code == 403
    assert client.get(f"{API}/machines?company_id={other}", headers=kim).status_code == 403
    assert client.get(f"{API}/admins?company_id={other}", headers=kim).status_code == 403
    assert client.get(f"{API}/companies/{other}", headers=kim).status_code == 403
    response = client.post(f"{API}/parking-lots", headers=kim,
                           json={"name": "침입 주차장", "address": "x", "company_id": other})
    assert response.status_code == 403


def test_other_company_resources_look_missing(client, auth, upload, ids):
    b_violation = _violation_id(upload, B_HQ)
    b_lot = ids["lots"]["본사 주차장"]
    kim = auth("kim@a.com")

    assert client.get(f"{API}/violations/{b_violation}", headers=kim).status_code == 404
    assert client.get(f"{API}/violations/{b_violation}/image-url", headers=kim).status_code == 404
    assert client.post(f"{API}/violations/{b_violation}/acknowledge", headers=kim).status_code == 404
    assert client.post(f"{API}/violations/{b_violation}/resolve", headers=kim, json={}).status_code == 404
    assert client.get(f"{API}/parking-lots/{b_lot}", headers=kim).status_code == 404

    # and the violation was not touched
    detail = client.get(f"{API}/violations/{b_violation}", headers=auth("choi@b.com")).get_json()
    assert detail["status"] == "NOTIFIED"


def test_assigning_another_companys_lot_is_rejected_by_api(client, auth, ids):
    """Even the SUPER_ADMIN cannot link an admin of company A to a lot of company B."""
    kim_id = ids["admins"]["kim@a.com"]
    root = auth("super@pg.local")
    response = client.put(f"{API}/admins/{kim_id}/parking-lots", headers=root,
                          json={"parking_lot_ids": [ids["lots"]["강남 주차장"], ids["lots"]["본사 주차장"]]})
    assert response.status_code == 400

    # the existing assignment is untouched
    admins = client.get(f"{API}/admins", headers=root).get_json()["items"]
    kim = next(a for a in admins if a["id"] == kim_id)
    assert kim["parking_lot_ids"] == sorted([ids["lots"]["강남 주차장"], ids["lots"]["판교 주차장"]])


def test_assigning_another_companys_lot_is_rejected_by_database(app, ids):
    """Even if application checks were bypassed, the composite foreign keys refuse the row."""
    with app.app_context():
        for claimed_company in ("A회사", "B회사"):
            db.session.add(AdminParkingLot(
                admin_id=ids["admins"]["lee@a.com"],          # company A admin
                parking_lot_id=ids["lots"]["본사 주차장"],      # company B lot
                company_id=ids["companies"][claimed_company],
            ))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


def test_admin_role_and_company_must_match(app, ids):
    from app.models import Admin, Role

    with app.app_context():
        db.session.add(Admin(email="bad@x.com", name="bad", password_hash="x", role=Role.SUPER_ADMIN,
                             company_id=ids["companies"]["A회사"]))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        db.session.add(Admin(email="bad2@x.com", name="bad", password_hash="x", role=Role.COMPANY_ADMIN,
                             company_id=None))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
