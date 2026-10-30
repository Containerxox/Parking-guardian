"""How the Docker build is laid out (design doc 9.6 step 3-1, 3-2). Static checks on the files only; the images
themselves are built and exercised by backend/scripts/check_inference_service.py and check_phase1_compose.py.

  prototype  the build the project always had (API + torch + YOLOv5 in one image). It stays the LAST stage, so
             `docker build` without --target and docker-compose.yml keep producing it.
  api        no torch, no YOLOv5, no OpenCV: the API talks to the inference service (INFERENCE_BACKEND=http).
  inference  the two models behind gunicorn_inference.conf.py; only what the service imports.
"""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DOCKERFILE = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
INFERENCE_REQUIREMENTS = (BACKEND / "requirements-inference.txt").read_text(encoding="utf-8")
PHASE1_COMPOSE = (BACKEND.parent / "docker-compose.phase1.yml").read_text(encoding="utf-8")


def stages():
    """{stage name: its text}, in file order."""
    parts = re.split(r"^(?=FROM )", DOCKERFILE, flags=re.MULTILINE)
    named = {}
    for part in parts:
        match = re.match(r"FROM \S+ AS (\S+)", part)
        if match:
            named[match.group(1)] = part
    return named


def requirement_names(text):
    names = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[; ]", line)[0].lower().replace("_", "-"))
    return names


def test_the_three_targets_exist_and_the_prototype_stays_the_default():
    found = stages()

    assert {"prototype", "api", "inference"} <= set(found)
    assert list(found)[-1] == "prototype"  # the last stage is what a plain `docker build` produces


def test_api_image_has_no_ai_dependencies():
    api = stages()["api"]

    for forbidden in ("torch", "yolov5", "libgl", "opencv"):
        assert forbidden not in api.lower(), forbidden
    assert "requirements.txt" in api


def test_api_image_keeps_the_migrations_and_the_prototype_start_command():
    api = stages()["api"]

    assert "flask db upgrade" in api and "MIGRATE_ON_START" in api
    assert "gunicorn -c gunicorn.conf.py wsgi:app" in api
    assert re.search(r"INFERENCE_BACKEND=http", api)  # this image has no models to run in-process


def test_prototype_image_is_still_the_all_in_one_build():
    prototype = stages()["prototype"]

    assert "torch" in DOCKERFILE and "yolov5" in DOCKERFILE.lower()
    assert "pip install -r requirements.txt" in prototype
    assert "flask db upgrade" in prototype and "gunicorn -c gunicorn.conf.py wsgi:app" in prototype
    assert "INFERENCE_BACKEND=http" not in prototype  # unchanged behaviour: models run in-process


def test_inference_image_runs_the_inference_service_only():
    inference = stages()["inference"]

    assert "torch" in inference
    assert "pip install -r requirements-inference.txt" in inference
    assert "pip install -r requirements.txt" not in inference
    assert "gunicorn -c gunicorn_inference.conf.py wsgi_inference:app" in inference
    assert "flask db upgrade" not in inference and "MIGRATE_ON_START" not in inference
    assert "EXPOSE 8000" in inference


def test_inference_image_copies_only_what_the_service_imports():
    inference = stages()["inference"]

    assert not re.search(r"^COPY \. \.", inference, flags=re.MULTILINE)  # no migrations, scripts or tests
    for needed in ("app", "inference_service", "wsgi_inference.py", "gunicorn_inference.conf.py"):
        assert needed in inference


def test_inference_requirements_leave_out_database_auth_and_cloud_packages():
    names = requirement_names(INFERENCE_REQUIREMENTS)

    for forbidden in ("flask-sqlalchemy", "sqlalchemy", "flask-migrate", "flask-cors", "bcrypt", "pyjwt",
                      "psycopg", "boto3", "pytest"):
        assert forbidden not in names, forbidden
    assert {"flask", "python-dotenv", "prometheus-client", "gunicorn"} <= names


def test_gunicorn_is_pinned_to_the_version_that_was_checked():
    assert re.search(r"^gunicorn==\d+\.\d+\.\d+\s*$", INFERENCE_REQUIREMENTS, flags=re.MULTILINE)


def test_the_phase1_compose_file_adds_the_inference_service_and_switches_the_api():
    assert "inference:" in PHASE1_COMPOSE and "target: inference" in PHASE1_COMPOSE
    assert "target: api" in PHASE1_COMPOSE
    assert "INFERENCE_BACKEND: http" in PHASE1_COMPOSE
    assert "INFERENCE_URL: http://inference:8000" in PHASE1_COMPOSE
    assert "/readyz" in PHASE1_COMPOSE  # the inference healthcheck
    assert "INFERENCE_FAULT_DELAY_MS" in PHASE1_COMPOSE  # for the delay drill
