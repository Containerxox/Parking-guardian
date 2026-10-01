"""Test fixtures: an isolated app on in-memory SQLite with mock inference and the demo data set."""
import io
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.cli import DEMO_PASSWORD, dev_api_key  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Admin, Company, ParkingLot  # noqa: E402

API = "/api/v1"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-content"


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "INFERENCE_BACKEND": "mock",
        "IMAGE_STORAGE_DIR": str(tmp_path / "storage"),
        "JWT_SECRET": "test-secret-with-enough-length-for-hs256",
        "BCRYPT_ROUNDS": 4,
        # Most tests care about what happens after a violation exists, so one sighting confirms it.
        # tests/test_episodes.py sets the real default (2) to test the confirmation rule itself.
        "VIOLATION_CONFIRM_COUNT": 1,
    })
    with app.app_context():
        db.create_all()
    result = app.test_cli_runner().invoke(args=["seed"])
    assert result.exit_code == 0, result.output
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def ids(app):
    """Ids of the seeded rows, looked up by name."""
    with app.app_context():
        companies = {c.name: c.id for c in db.session.scalars(select(Company))}
        admins = {a.email: a.id for a in db.session.scalars(select(Admin))}
        lots = {lot.name: lot.id for lot in db.session.scalars(select(ParkingLot))}
    return {"companies": companies, "admins": admins, "lots": lots}


@pytest.fixture
def auth(app):
    """auth("kim@a.com") -> Authorization header for that admin."""
    cache = {}

    def headers_for(email: str) -> dict:
        if email not in cache:
            response = app.test_client().post(f"{API}/auth/login", json={"email": email, "password": DEMO_PASSWORD})
            assert response.status_code == 200, response.get_json()
            cache[email] = {"Authorization": f"Bearer {response.get_json()['access_token']}"}
        return cache[email]

    return headers_for


@pytest.fixture
def upload(client):
    """upload("RPI-A-GANGNAM-01", "violation.jpg") -> response of POST /device/detections."""

    def send(serial: str, filename: str = "violation.jpg", **form):
        data = {"file": (io.BytesIO(JPEG_BYTES), filename), **form}
        return client.post(f"{API}/device/detections", data=data, content_type="multipart/form-data",
                           headers={"X-Device-Key": dev_api_key(serial)})

    return send
