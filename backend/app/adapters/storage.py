"""Image storage port with two adapters: local disk (default) and S3 (ADR 0016).

The database stores only the object key, which is already laid out like an S3 key. Both adapters
expose the same three methods (save / read / exists) and raise FileNotFoundError for a missing key,
so the API code does not know which one is behind `adapters.storage()`.

    STORAGE_BACKEND=local   IMAGE_STORAGE_DIR           (python wsgi.py, docker compose)
    STORAGE_BACKEND=s3      S3_BUCKET [S3_PREFIX]       (EKS; credentials come from the environment,
                                                          e.g. EKS Pod Identity, never from config)
"""
from datetime import datetime
from pathlib import Path


def build_image_key(company_id: int, parking_lot_id: int, violation_id: int, detected_at: datetime,
                    extension: str = "jpg") -> str:
    return (f"violations/company-{company_id}/parking-lot-{parking_lot_id}/"
            f"{detected_at:%Y/%m/%d}/violation-{violation_id}.{extension}")


def sniff_image_type(data: bytes) -> tuple[str, str]:
    """Return (extension, mimetype) from the file's magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


class LocalImageStorage:
    name = "local"

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        path = (self.base_dir / key).resolve()
        if self.base_dir not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


_S3_MISSING_CODES = ("NoSuchKey", "NotFound", "404")


def _s3_error_code(exc: Exception) -> str | None:
    """botocore.exceptions.ClientError carries the S3 error code in exc.response["Error"]["Code"]."""
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code")


class S3ImageStorage:
    """Stores the photo bytes as-is (no re-encoding, ADR 0006) in one bucket.

    `client` is injectable for tests. Without it, boto3 is imported on first use so that the local
    and test configurations never need boto3 or AWS credentials.
    """
    name = "s3"

    def __init__(self, bucket: str, prefix: str = "", client=None, region: str | None = None):
        if not bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3  # imported lazily: only the s3 backend needs it
            self._client = boto3.client("s3", region_name=self._region) if self._region else boto3.client("s3")
        return self._client

    def _object_key(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid storage key")
        return f"{self.prefix}/{key}" if self.prefix else key

    def save(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._object_key(key), Body=data,
                               ContentType=sniff_image_type(data)[1])

    def read(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        except Exception as exc:
            if _s3_error_code(exc) in _S3_MISSING_CODES:
                raise FileNotFoundError(key) from exc
            raise
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._object_key(key))
        except Exception as exc:
            if _s3_error_code(exc) in _S3_MISSING_CODES:
                return False
            raise
        return True


def build_storage(config) -> object:
    backend = config.get("STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalImageStorage(config["IMAGE_STORAGE_DIR"])
    if backend == "s3":
        return S3ImageStorage(config.get("S3_BUCKET", ""), config.get("S3_PREFIX", ""),
                              region=config.get("AWS_REGION"))
    raise ValueError(f"unknown STORAGE_BACKEND: {backend!r} (expected local or s3)")
