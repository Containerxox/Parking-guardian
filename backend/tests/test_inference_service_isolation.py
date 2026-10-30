"""The inference service must start without the API's database/auth packages installed (design doc 3.2:
no DB, no secrets). A subprocess blocks those imports, so the check does not depend on what this venv has."""
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BLOCKED = ["flask_sqlalchemy", "sqlalchemy", "flask_migrate", "alembic", "flask_cors", "bcrypt", "jwt",
           "psycopg", "boto3"]


def test_service_starts_without_database_and_auth_packages():
    code = f"""
import sys
for name in {BLOCKED!r}:
    sys.modules[name] = None  # any import of it now raises ImportError
sys.path.insert(0, {str(BACKEND)!r})
from inference_service import create_app
service = create_app({{"TESTING": True, "INFERENCE_BACKEND": "mock"}})
assert service.test_client().get("/readyz").status_code == 200
"""
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
