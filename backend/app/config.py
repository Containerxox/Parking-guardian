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
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", _default_database_url())
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Images ---
    IMAGE_STORAGE_DIR = os.environ.get("IMAGE_STORAGE_DIR", str(BASE_DIR / "storage"))
    IMAGE_URL_TTL_SECONDS = int(os.environ.get("IMAGE_URL_TTL_SECONDS", "300"))
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
    NOTIFICATION_CHANNELS = ("IN_APP", "EMAIL")
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "console")  # console | ses (later phase)
