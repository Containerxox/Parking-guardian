# Parking Guardian backend (Flask API + in-process AI inference).
#
# Built by docker-compose.yml, which passes the YOLOv5 source (../yolov5) as a second
# build context named "yolov5":
#
#   docker compose build backend
#
# The model weights (default.pt, last.pt) are not in the image. They are mounted at /models.
# The AI parts (torch, YOLOv5) leave this image when inference becomes its own service.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Shared libraries OpenCV needs at import time
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- AI dependencies (large and rarely changed, so they come first for layer caching) ---
# CPU build of PyTorch, same versions as verified locally.
COPY --from=yolov5 requirements.txt /tmp/yolov5-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r /tmp/yolov5-requirements.txt
COPY --from=yolov5 . /opt/yolov5

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
