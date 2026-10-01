"""Company admins may read the parking structure of their company but only SUPER_ADMIN may change it."""
from .conftest import API


def _snapshot(client, headers, lot_id):
    lot = client.get(f"{API}/parking-lots/{lot_id}", headers=headers).get_json()
    return {
        "name": lot["name"],
        "zones": [(z["zone_name"], z["machine_id"]) for z in lot["zones"]],
        "machines": [(m["serial_number"], m["name"], m["status"]) for m in lot["machines"]],
        "admins": sorted(a["id"] for a in lot["admins"]),
    }


def test_company_admin_can_read_every_lot_of_the_company(client, auth, ids):
    lee = auth("lee@a.com")  # assigned to 판교 only
    lots = client.get(f"{API}/parking-lots", headers=lee).get_json()["items"]
    assert {lot["name"] for lot in lots} == {"강남 주차장", "판교 주차장", "수원 주차장"}

    # detail of a lot lee is NOT assigned to: still readable, including who is responsible
    detail = client.get(f"{API}/parking-lots/{ids['lots']['강남 주차장']}", headers=lee)
    assert detail.status_code == 200
    body = detail.get_json()
    assert [a["email"] for a in body["admins"]] == ["kim@a.com"]
    assert body["zones"] and body["machines"]
    assert client.get(f"{API}/machines", headers=lee).get_json()["total"] == 3


def test_company_admin_cannot_change_parking_structure(client, auth, ids):
    kim = auth("kim@a.com")
    lot_id = ids["lots"]["강남 주차장"]  # kim's own assigned lot
    before = _snapshot(client, kim, lot_id)
    detail = client.get(f"{API}/parking-lots/{lot_id}", headers=kim).get_json()
    zone_id, machine_id = detail["zones"][0]["id"], detail["machines"][0]["id"]
    kim_id = ids["admins"]["kim@a.com"]

    attempts = [
        ("post", "/parking-lots", {"name": "새 주차장", "address": "어딘가"}),
        ("patch", f"/parking-lots/{lot_id}", {"name": "이름 변경"}),
        ("post", f"/parking-lots/{lot_id}/zones", {"zone_name": "Z-99"}),
        ("patch", f"/zones/{zone_id}", {"zone_name": "Z-00"}),
        ("post", "/machines", {"parking_lot_id": lot_id, "serial_number": "RPI-X", "name": "x"}),
        ("patch", f"/machines/{machine_id}", {"status": "INACTIVE"}),
        ("post", f"/machines/{machine_id}/rotate-key", None),
        ("put", f"/admins/{kim_id}/parking-lots", {"parking_lot_ids": []}),  # removing oneself from alerts
    ]
    for method, path, body in attempts:
        response = getattr(client, method)(f"{API}{path}", headers=kim, json=body)
        assert response.status_code == 403, (method, path, response.get_json())
        assert response.get_json()["error"]["code"] == "forbidden"

    assert _snapshot(client, kim, lot_id) == before
    assert client.get(f"{API}/parking-lots", headers=kim).get_json()["total"] == 3
    # the seeded device key still works, so nothing was rotated
    from app.cli import dev_api_key
    assert client.post(f"{API}/device/heartbeat",
                       headers={"X-Device-Key": dev_api_key("RPI-A-GANGNAM-01")}).status_code == 200


def test_super_admin_can_change_parking_structure(client, auth, ids):
    root = auth("super@pg.local")
    company_id = ids["companies"]["A회사"]

    lot = client.post(f"{API}/parking-lots", headers=root,
                      json={"name": "분당 주차장", "address": "경기 성남시", "company_id": company_id})
    assert lot.status_code == 201
    lot_id = lot.get_json()["id"]
    assert client.post(f"{API}/parking-lots", headers=root,
                       json={"name": "주소만", "address": "x"}).status_code == 400  # company_id required

    machine = client.post(f"{API}/machines", headers=root,
                          json={"parking_lot_id": lot_id, "serial_number": "RPI-A-BUNDANG-01", "name": "Camera-01"})
    assert machine.status_code == 201
    machine_id = machine.get_json()["machine"]["id"]
    zone = client.post(f"{API}/parking-lots/{lot_id}/zones", headers=root,
                       json={"zone_name": "E-01", "machine_id": machine_id})
    assert zone.status_code == 201
    assert client.patch(f"{API}/zones/{zone.get_json()['id']}", headers=root,
                        json={"location_description": "1층"}).status_code == 200
    assert client.patch(f"{API}/machines/{machine_id}", headers=root, json={"name": "Camera-A"}).status_code == 200
    assert client.patch(f"{API}/parking-lots/{lot_id}", headers=root, json={"address": "경기 성남시 분당구"}).status_code == 200

    lee_id = ids["admins"]["lee@a.com"]
    assigned = client.put(f"{API}/admins/{lee_id}/parking-lots", headers=root,
                          json={"parking_lot_ids": [ids["lots"]["판교 주차장"], lot_id]})
    assert assigned.status_code == 200 and lot_id in assigned.get_json()["parking_lot_ids"]

    # the company admin sees the result, and the new assignment drives notifications
    detail = client.get(f"{API}/parking-lots/{lot_id}", headers=auth("lee@a.com")).get_json()
    assert [a["email"] for a in detail["admins"]] == ["lee@a.com"]

    actions = {a["action"] for a in client.get(f"{API}/audit-logs", headers=root).get_json()["items"]}
    assert {"PARKING_LOT_CREATED", "MACHINE_REGISTERED", "PARKING_ZONE_CREATED", "ADMIN_ASSIGNMENT_CHANGED"} <= actions
