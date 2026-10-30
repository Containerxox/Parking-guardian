# Parking Guardian backend images (multi-target). Built by docker-compose.yml, which passes the YOLOv5 source
# (../yolov5) as a second build context named "yolov5":
#
#   docker compose build backend                      -> prototype (the default, as before)
#   docker build --target api       -t pg-api ...     -> Flask API, NO torch / YOLOv5 / OpenCV
#   docker build --target inference -t pg-inference ... -> the two YOLOv5 models behind gunicorn
#
# prototype  API and models in one image. The project's original build; it stays the LAST stage, so a plain
#            `docker build` and docker-compose.yml keep producing exactly this.
# api        Phase 1 API: INFERENCE_BACKEND=http, asks the inference service. Same migrations, same start command.
# inference  Phase 1 inference service (inference_service/): no database, JWT or S3 packages, no migrations.
#
# The model weights (default.pt, last.pt) are never in an image. They are mounted at /models.
# Phase 1 stack: docker-compose.yml + docker-compose.phase1.yml.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1


# ---------------------------------------------------------------------------------------------------------
# AI dependencies (large and rarely changed, so they come first for layer caching). Shared by prototype and
# inference. Same layers, in the same order, as the single-stage Dockerfile this replaced.
# ---------------------------------------------------------------------------------------------------------
FROM base AS ai-deps

# Shared libraries OpenCV needs at import time
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU build of PyTorch, same versions as verified locally.
COPY --from=yolov5 requirements.txt /tmp/yolov5-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r /tmp/yolov5-requirements.txt
COPY --from=yolov5 . /opt/yolov5


# ---------------------------------------------------------------------------------------------------------
# api: no AI packages at all. YoloInference imports torch/cv2/numpy only inside its methods, and
# INFERENCE_BACKEND=http never calls it.
# ---------------------------------------------------------------------------------------------------------
FROM base AS api

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . .

# Run as a normal user. /data/storage is where violation photos are written (a volume).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/storage \
    && chown -R app:app /data
USER app

ENV FLASK_APP=wsgi \
    INFERENCE_BACKEND=http \
    IMAGE_STORAGE_DIR=/data/storage

EXPOSE 5000

# Same start command as the prototype (see there). gunicorn.conf.py reports the AI-call limit against the real
# thread count instead of loading models when the backend is http.
CMD if [ "${MIGRATE_ON_START:-1}" = "1" ]; then         flask db upgrade         && if [ "$SEED_DEMO_DATA" = "1" ]; then flask seed; fi;     fi     && exec gunicorn -c gunicorn.conf.py wsgi:app         --bind 0.0.0.0:5000         --workers "${WEB_CONCURRENCY:-1}"         --threads "${GUNICORN_THREADS:-4}"         --timeout "${GUNICORN_TIMEOUT:-120}"         --access-logfile -


# ---------------------------------------------------------------------------------------------------------
# inference: only what the service imports. No database, JWT, CORS, S3 or test packages, no migrations, and
# only the code it needs (app/ is imported for its config, metrics and adapters; inference_service/ is the app).
# ---------------------------------------------------------------------------------------------------------
FROM ai-deps AS inference

COPY requirements-inference.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-inference.txt

COPY app ./app
COPY inference_service ./inference_service
COPY wsgi_inference.py gunicorn_inference.conf.py ./

RUN useradd --create-home --uid 10001 app
USER app

ENV YOLOV5_DIR=/opt/yolov5 \
    WINDOW_MODEL_PATH=/models/default.pt \
    STICKER_MODEL_PATH=/models/last.pt \
    INFERENCE_BACKEND=yolo

EXPOSE 8000

# ONE gunicorn worker, enforced by gunicorn_inference.conf.py: the admission limit and the model copy live in a
# single process. wsgi_inference.py sets OMP_NUM_THREADS before torch is imported. `exec` makes gunicorn PID 1
# so it receives SIGTERM and finishes the photos in progress (graceful_timeout 30 s).
CMD exec gunicorn -c gunicorn_inference.conf.py wsgi_inference:app


# ---------------------------------------------------------------------------------------------------------
# prototype (LAST stage = the default): API + models in one image, exactly as before Phase 1.
# ---------------------------------------------------------------------------------------------------------
FROM ai-deps AS prototype

# --- API dependencies ---
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . .

# Run as a normal user. /data/storage is where violation photos are written (a volume).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/storage \
    && chown -R app:app /data
USER app

ENV FLASK_APP=wsgi \
    YOLOV5_DIR=/opt/yolov5 \
    WINDOW_MODEL_PATH=/models/default.pt \
    STICKER_MODEL_PATH=/models/last.pt \
    IMAGE_STORAGE_DIR=/data/storage

EXPOSE 5000

# 1. Apply database migrations and optionally create demo data -- unless MIGRATE_ON_START=0.
#    On Kubernetes a pre-sync Job runs these once per deploy, because two replicas (HPA) running
#    `flask db upgrade` at the same time would race on the alembic_version table. docker-compose keeps
#    the default (1): one container, migrate on start.
# 2. Start gunicorn. `exec` makes gunicorn PID 1 so it receives SIGTERM and shuts down gracefully.
#    One worker: each worker would load its own copy of the models (the sticker model is 370 MB).
#    gunicorn.conf.py starts loading the models right after the worker is up (warm-up for /readyz).
CMD if [ "${MIGRATE_ON_START:-1}" = "1" ]; then         flask db upgrade         && if [ "$SEED_DEMO_DATA" = "1" ]; then flask seed; fi;     fi     && exec gunicorn -c gunicorn.conf.py wsgi:app         --bind 0.0.0.0:5000         --workers "${WEB_CONCURRENCY:-1}"         --threads "${GUNICORN_THREADS:-4}"         --timeout "${GUNICORN_TIMEOUT:-120}"         --access-logfile -
