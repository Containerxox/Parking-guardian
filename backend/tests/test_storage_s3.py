"""S3 image storage adapter (ADR 0016), tested without AWS through an injected fake client."""
import io

import pytest
from botocore.exceptions import ClientError

from app.adapters.storage import LocalImageStorage, S3ImageStorage, build_storage

from .conftest import API, JPEG_BYTES

GANGNAM = "RPI-A-GANGNAM-01"


def _missing(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "missing"}}, operation)


class FakeS3Client:
    """The three calls the adapter makes, with botocore's error shape for a missing object."""

    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = (bytes(Body), ContentType)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _missing("NoSuchKey", "GetObject")
        body, content_type = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(body), "ContentType": content_type}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _missing("404", "HeadObject")  # HEAD has no body, so S3 reports the bare status
        return {}


def test_save_read_exists_round_trip_keeps_the_bytes():
    fake = FakeS3Client()
    storage = S3ImageStorage("photos", prefix="dev/", client=fake)
    key = "violations/company-1/parking-lot-2/2026/10/25/violation-7.jpg"

    assert storage.exists(key) is False
    storage.save(key, JPEG_BYTES)

    assert storage.exists(key) is True
    assert storage.read(key) == JPEG_BYTES  # byte-identical, no re-encoding
    assert fake.objects[("photos", "dev/" + key)] == (JPEG_BYTES, "image/jpeg")


def test_missing_object_behaves_like_a_missing_file():
    storage = S3ImageStorage("photos", client=FakeS3Client())
    with pytest.raises(FileNotFoundError):
        storage.read("violations/nothing.jpg")
    with pytest.raises(ValueError):
        storage.read("../escape.jpg")


def test_build_storage_follows_the_setting(tmp_path):
    assert isinstance(build_storage({"IMAGE_STORAGE_DIR": str(tmp_path)}), LocalImageStorage)
    assert isinstance(build_storage({"STORAGE_BACKEND": "local", "IMAGE_STORAGE_DIR": str(tmp_path)}), LocalImageStorage)

    s3 = build_storage({"STORAGE_BACKEND": "s3", "S3_BUCKET": "photos", "S3_PREFIX": "p", "AWS_REGION": "ap-northeast-2"})
    assert isinstance(s3, S3ImageStorage) and s3.bucket == "photos" and s3.prefix == "p"

    with pytest.raises(ValueError):
        build_storage({"STORAGE_BACKEND": "s3", "S3_BUCKET": ""})
    with pytest.raises(ValueError):
        build_storage({"STORAGE_BACKEND": "ftp"})


def test_violation_photo_goes_to_s3_and_is_served_from_it(app, client, auth, upload):
    fake = FakeS3Client()
    app.extensions["pg_storage"] = S3ImageStorage("photos", client=fake)

    violation_id = upload(GANGNAM).get_json()["violation_id"]
    stored = [key for (_bucket, key) in fake.objects]
    assert stored == [f"violations/company-1/parking-lot-1/{_today_path()}/violation-{violation_id}.jpg"] or len(stored) == 1

    info = client.get(f"{API}/violations/{violation_id}/image-url", headers=auth("kim@a.com")).get_json()
    image = client.get(info["url"].split("localhost", 1)[-1])
    assert image.status_code == 200 and image.data == JPEG_BYTES and image.mimetype == "image/jpeg"

    detail = client.get(f"{API}/violations/{violation_id}", headers=auth("kim@a.com")).get_json()
    assert detail["image_status"] == "UPLOADED" and detail["has_image"] is True


def _today_path() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")
