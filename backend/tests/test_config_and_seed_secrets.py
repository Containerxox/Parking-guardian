"""Settings that only matter on a server: DB URL from parts, seed secrets, proxy headers."""
import importlib

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app import create_app
from app.cli import DEMO_PASSWORD, dev_api_key
from app.config import build_database_url
from app.extensions import db
from app.models import Admin, Machine

from .conftest import API, JPEG_BYTES, TEST_DATABASE_URL

GANGNAM = "RPI-A-GANGNAM-01"


def test_database_url_from_parts_survives_any_password():
    password = "p@ss:w/rd?#%&+ 한글"
    url = make_url(build_database_url("db.internal", "5432", "parking_guardian", "parking", password))
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db.internal" and url.port == 5432 and url.database == "parking_guardian"
    assert url.username == "parking" and url.password == password
    assert url.query == {"sslmode": "require"}


def test_env_precedence_database_url_then_db_parts(monkeypatch):
    import app.config as config_module

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "rds.internal")
    monkeypatch.setenv("DB_PASSWORD", "x:y")
    assert importlib.reload(config_module).Config.SQLALCHEMY_DATABASE_URI == \
        "postgresql+psycopg://parking:x%3Ay@rds.internal:5432/parking_guardian?sslmode=require"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert importlib.reload(config_module).Config.SQLALCHEMY_DATABASE_URI == "sqlite:///:memory:"

    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("DB_HOST")
    assert importlib.reload(config_module).Config.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///")


def _seeded_app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": TEST_DATABASE_URL,
        "INFERENCE_BACKEND": "mock",
        "IMAGE_STORAGE_DIR": str(tmp_path / "storage"),
        "JWT_SECRET": "test-secret-with-enough-length-for-hs256",
        "BCRYPT_ROUNDS": 4,
        "VIOLATION_CONFIRM_COUNT": 1,
    })
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app


def test_seed_secrets_set_initial_values_and_never_reset_them(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "Initial-Secret-42")
    monkeypatch.setenv("SEED_DEVICE_KEY_SECRET", "device-secret")
    app = _seeded_app(tmp_path)
    try:
        first = app.test_cli_runner().invoke(args=["seed"])
        assert first.exit_code == 0, first.output
        assert "Initial-Secret-42" not in first.output and "device-secret" not in first.output
        assert "pgk_" not in first.output  # derived keys are not printed either

        client = app.test_client()
        assert client.post(f"{API}/auth/login", json={"email": "kim@a.com", "password": "Initial-Secret-42"}).status_code == 200
        assert client.post(f"{API}/auth/login", json={"email": "kim@a.com", "password": DEMO_PASSWORD}).status_code == 401

        derived = dev_api_key(GANGNAM)
        assert derived.startswith("pgk_") and len(derived) == 36 and derived != f"pgk_dev_{GANGNAM.lower()}"
        shown = app.test_cli_runner().invoke(args=["device-key", GANGNAM])
        assert shown.output.strip() == derived

        def upload(key):
            import io
            return client.post(f"{API}/device/detections", data={"file": (io.BytesIO(JPEG_BYTES), "violation.jpg")},
                               content_type="multipart/form-data", headers={"X-Device-Key": key})

        assert upload(derived).status_code == 201
        assert upload(f"pgk_dev_{GANGNAM.lower()}").status_code == 401

        with app.app_context():
            hashes_before = {a.email: a.password_hash for a in db.session.scalars(select(Admin))}
            keys_before = {m.serial_number: m.api_key_hash for m in db.session.scalars(select(Machine))}

        # Re-seeding with different secrets must not touch existing rows.
        monkeypatch.setenv("SEED_ADMIN_PASSWORD", "Changed-Later")
        monkeypatch.setenv("SEED_DEVICE_KEY_SECRET", "other-secret")
        assert app.test_cli_runner().invoke(args=["seed"]).exit_code == 0
        with app.app_context():
            assert {a.email: a.password_hash for a in db.session.scalars(select(Admin))} == hashes_before
            assert {m.serial_number: m.api_key_hash for m in db.session.scalars(select(Machine))} == keys_before
        assert client.post(f"{API}/auth/login", json={"email": "kim@a.com", "password": "Initial-Secret-42"}).status_code == 200
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()


def test_without_seed_secrets_the_local_defaults_apply(tmp_path, monkeypatch):
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_DEVICE_KEY_SECRET", raising=False)
    app = _seeded_app(tmp_path)
    try:
        result = app.test_cli_runner().invoke(args=["seed"])
        assert result.exit_code == 0 and DEMO_PASSWORD in result.output and "pgk_dev_rpi-a-gangnam-01" in result.output
        assert dev_api_key(GANGNAM) == "pgk_dev_rpi-a-gangnam-01"
        assert app.test_cli_runner().invoke(args=["device-key", GANGNAM]).output.strip() == "pgk_dev_rpi-a-gangnam-01"
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()


def test_proxy_headers_decide_the_scheme_of_generated_urls(client, auth, upload):
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    headers = {**auth("kim@a.com"), "X-Forwarded-Proto": "https", "X-Forwarded-Host": "pg.example.com"}
    info = client.get(f"{API}/violations/{violation_id}/image-url", headers=headers).get_json()
    assert info["url"].startswith("https://pg.example.com/api/v1/images/")

    plain = client.get(f"{API}/violations/{violation_id}/image-url", headers=auth("kim@a.com")).get_json()
    assert plain["url"].startswith("http://localhost/")
