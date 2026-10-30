"""Application settings. Every value comes from the environment with a local-dev default."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _default_database_url() -> str:
    return "sqlite:///" + (BASE_DIR / "instance" / "parking_guardian.db").as_posix()


def build_database_url(host: str, port: str = "5432", name: str = "parking_guardian", user: str = "parking",
                       password: str = "", sslmode: str = "require") -> str:
    """Assemble a PostgreSQL URL from separate parts.

    Used when the password comes from a secret store (RDS-managed password via Secrets Manager) and the
    host from somewhere else (Terraform output). Quoting here means a generated password may contain
    any character; nothing outside the app has to URL-encode it.
    """
    from urllib.parse import quote

    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    url = f"postgresql+psycopg://{auth}@{host}:{port}/{name}"
    if sslmode:
        url += f"?sslmode={sslmode}"
    return url


def _database_url() -> str:
    """DATABASE_URL wins. Otherwise DB_HOST + DB_* parts (EKS). Otherwise the local SQLite file."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("DB_HOST")
    if host:
        return build_database_url(
            host,
            port=os.environ.get("DB_PORT", "5432"),
            name=os.environ.get("DB_NAME", "parking_guardian"),
            user=os.environ.get("DB_USER", "parking"),
            password=os.environ.get("DB_PASSWORD", ""),
            sslmode=os.environ.get("DB_SSLMODE", "require"),
        )
    return _default_database_url()


class Config:
    # --- Security ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")
    JWT_SECRET = os.environ.get("JWT_SECRET", SECRET_KEY)
    ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
    REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "14"))
    REFRESH_COOKIE_NAME = "pg_refresh"
    COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "Lax")
    CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]

    # --- Database ---
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Images ---
    # local: files under IMAGE_STORAGE_DIR. s3: objects in S3_BUCKET (ADR 0016). See adapters/storage.py
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")  # local | s3
    S3_BUCKET = os.environ.get("S3_BUCKET", "")
    S3_PREFIX = os.environ.get("S3_PREFIX", "")
    AWS_REGION = os.environ.get("AWS_REGION")  # optional; boto3 also reads it from the environment
    IMAGE_STORAGE_DIR = os.environ.get("IMAGE_STORAGE_DIR", str(BASE_DIR / "storage"))
    IMAGE_URL_TTL_SECONDS = int(os.environ.get("IMAGE_URL_TTL_SECONDS", "300"))
    # Only sets violations.image_expires_at when a photo is stored. Nothing deletes photos or marks them
    # EXPIRED yet; that is planned for the S3 storage step.
    IMAGE_RETENTION_DAYS = int(os.environ.get("IMAGE_RETENTION_DAYS", "90"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024

    # --- AI inference ---
    INFERENCE_BACKEND = os.environ.get("INFERENCE_BACKEND", "yolo")  # yolo | mock
    YOLOV5_DIR = os.environ.get("YOLOV5_DIR", str(BASE_DIR.parent / "yolov5"))
    WINDOW_MODEL_PATH = os.environ.get("WINDOW_MODEL_PATH", str(BASE_DIR / "models" / "default.pt"))
    STICKER_MODEL_PATH = os.environ.get("STICKER_MODEL_PATH", str(BASE_DIR / "models" / "last.pt"))
    WINDOW_CLASS_NAME = os.environ.get("WINDOW_CLASS_NAME", "windshield,car_window")  # accepted names, comma separated
    WINDOW_CONF = float(os.environ.get("WINDOW_CONF", "0.25"))
    STICKER_CONF = float(os.environ.get("STICKER_CONF", "0.5"))
    INFERENCE_IMAGE_SIZE = int(os.environ.get("INFERENCE_IMAGE_SIZE", "640"))
    # Background model load: total attempts and the base wait between them (waits grow: delay x attempt).
    # After the last failure the inference service reports /healthz 503 so Kubernetes restarts the container.
    MODEL_LOAD_ATTEMPTS = int(os.environ.get("MODEL_LOAD_ATTEMPTS", "3"))
    MODEL_LOAD_RETRY_SECONDS = float(os.environ.get("MODEL_LOAD_RETRY_SECONDS", "5"))

    # Prometheus /metrics port (own HTTP server thread, started by gunicorn.conf.py). 0 disables it.
    METRICS_PORT = int(os.environ.get("METRICS_PORT", "9102"))
    # Licence plate OCR settings (OCR_*) were removed with the OCR step (ADR 0017).

    # --- Devices ---
    DEVICE_NAIVE_TZ = os.environ.get("DEVICE_NAIVE_TZ", "Asia/Seoul")
    MACHINE_ONLINE_SECONDS = int(os.environ.get("MACHINE_ONLINE_SECONDS", "300"))

    # --- Operations indicators ---
    # An open violation older than this counts as "left unattended" on the operator dashboard
    STALE_VIOLATION_HOURS = int(os.environ.get("STALE_VIOLATION_HOURS", "24"))

    # --- Violation episodes (one parked vehicle = one violation) ---
    # Consecutive violation verdicts needed before a violation is created
    VIOLATION_CONFIRM_COUNT = int(os.environ.get("VIOLATION_CONFIRM_COUNT", "2"))
    # Consecutive non-violation verdicts needed before the vehicle is considered gone
    VEHICLE_CLEAR_COUNT = int(os.environ.get("VEHICLE_CLEAR_COUNT", "3"))
    # Photos missing for longer than this close the open episode and restart counting
    EPISODE_GAP_MINUTES = int(os.environ.get("EPISODE_GAP_MINUTES", "10"))

    # --- Notifications ---
    # Not read anywhere yet: channels come from models.Channel.ALL and e-mail always goes to the
    # console logger (adapters/notifier.py). Kept as the place for the SES/SQS step to plug in.
    NOTIFICATION_CHANNELS = ("IN_APP", "EMAIL")
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "console")  # console | ses (later phase)
