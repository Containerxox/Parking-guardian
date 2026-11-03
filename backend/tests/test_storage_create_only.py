"""save_new(): write an object only if the key is still free (Phase 2 inbox, decision 2026-11-02).

The inbox key is derived from (machine, event_id) alone, so a second write to the same key is either a retry of
the same photo or a device bug. Either way the stored photo must never be replaced silently: save_new returns
False and the caller compares the bytes. Phase 1's save() (overwrite) is unchanged.
"""
import threading

import pytest
from botocore.exceptions import ClientError

from app.adapters.storage import LocalImageStorage, S3ImageStorage

KEY = "inbox/m-1/6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


def test_local_save_new_creates_once_and_never_replaces(tmp_path):
    storage = LocalImageStorage(str(tmp_path))
    assert storage.save_new(KEY, b"first") is True
    assert storage.save_new(KEY, b"second") is False
    assert storage.read(KEY) == b"first"
    assert [p.name for p in (tmp_path / "inbox" / "m-1").iterdir()] == [KEY.rsplit("/", 1)[1]]   # no temp files


def test_local_save_new_has_exactly_one_winner_under_concurrency(tmp_path):
    storage = LocalImageStorage(str(tmp_path))
    storage.save_new("inbox/m-1/warm", b"x")   # directory exists, so only the file write races
    results, barrier = [], threading.Barrier(8)

    def write(i):
        barrier.wait()
        results.append((storage.save_new(KEY, f"photo-{i}".encode()), i))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [i for created, i in results if created]
    assert len(winners) == 1
    assert storage.read(KEY) == f"photo-{winners[0]}".encode()   # whole content of the winner, never a mix


def test_local_save_still_overwrites_for_phase1(tmp_path):
    storage = LocalImageStorage(str(tmp_path))
    storage.save("violations/x.jpg", b"a")
    storage.save("violations/x.jpg", b"b")
    assert storage.read("violations/x.jpg") == b"b"


class ConditionalS3:
    """put_object with IfNoneMatch='*' as S3 implements it: 412 PreconditionFailed when the key exists."""

    def __init__(self, conflict_once=False):
        self.objects, self.calls, self.conflict_once = {}, [], conflict_once

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        key = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*":
            if self.conflict_once:
                self.conflict_once = False
                raise ClientError({"Error": {"Code": "ConditionalRequestConflict", "Message": "busy"}}, "PutObject")
            if key in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "exists"}}, "PutObject")
        self.objects[key] = kwargs["Body"]


def test_s3_save_new_uses_a_conditional_put():
    client = ConditionalS3()
    storage = S3ImageStorage("photos", prefix="dev/", client=client)
    assert storage.save_new(KEY, b"\xff\xd8\xffa") is True
    assert storage.save_new(KEY, b"\xff\xd8\xffb") is False
    assert client.objects[("photos", "dev/" + KEY)] == b"\xff\xd8\xffa"
    assert client.calls[0]["IfNoneMatch"] == "*" and client.calls[0]["ContentType"] == "image/jpeg"


def test_s3_concurrent_conditional_write_is_reported_as_taken():
    """S3 answers 409 ConditionalRequestConflict when another conditional write to the key is in flight;
    the caller then reads and compares, like any existing object."""
    storage = S3ImageStorage("photos", client=ConditionalS3(conflict_once=True))
    assert storage.save_new(KEY, b"x") is False


def test_s3_save_new_other_errors_propagate():
    class Broken:
        def put_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")

    with pytest.raises(ClientError):
        S3ImageStorage("photos", client=Broken()).save_new(KEY, b"x")
