"""Image storage port and the local-disk adapter.

The database stores only the object key. A later phase adds an S3 adapter with the same methods;
the key format is already the S3 key layout.
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


def build_storage(config) -> object:
    return LocalImageStorage(config["IMAGE_STORAGE_DIR"])
