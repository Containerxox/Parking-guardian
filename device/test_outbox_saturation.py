"""Outbox saturation policy (decision 2026-11-02): only `refresh` may ever be evicted to make room. If there is
no refresh to evict, the NEW event is refused with an explicit error: it is not stored, not counted as sent, never
reaches the server, and no existing item (arrived, confirm, left) is touched or reordered.

    python -m pytest device/test_outbox_saturation.py -q
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from outbox import Outbox, OutboxFull, OutboxItem  # noqa: E402
from pi_client import AsyncEventRunner  # noqa: E402
from test_async_mode import (SETTINGS, FakeCamera, FakeClock, FakeServer, ScriptedDetector, command,  # noqa: E402
                             run_until)


def fill(tmp_path, events, max_items=3, max_bytes=50 * 1024 * 1024):
    clock = FakeClock()
    outbox = Outbox(tmp_path / "outbox", clock=clock, max_items=max_items, max_bytes=max_bytes)
    session = str(uuid.uuid4())
    for seq, event in enumerate(events, start=1):
        outbox.put(OutboxItem.new(session, seq, event, b"\xff\xd8\xff" + event.encode() + b"-%d" % seq, clock))
        clock.sleep(1)
    return outbox, clock, session


def snapshot(outbox):
    return [(i.name, i.event_id, i.seq, i.event, i.jpeg) for i in outbox.items()]


def item_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "outbox" / "items").iterdir())


@pytest.mark.parametrize("new_event", ["refresh", "confirm", "arrived", "left"])
def test_full_outbox_without_a_refresh_refuses_the_new_event_and_touches_nothing(tmp_path, new_event):
    outbox, clock, session = fill(tmp_path, ["arrived", "confirm", "left"])
    before, files_before = snapshot(outbox), item_files(tmp_path)
    with pytest.raises(OutboxFull):
        outbox.put(OutboxItem.new(session, 9, new_event, b"\xff\xd8\xffnew", clock))
    assert snapshot(outbox) == before                      # same items, same bytes, same order
    assert item_files(tmp_path) == files_before            # no stray file
    assert outbox.dead_items() == []                       # nothing evicted; the refused photo is not "dead" either


def test_only_refresh_is_evicted_and_the_other_events_keep_their_order(tmp_path):
    outbox, clock, session = fill(tmp_path, ["arrived", "refresh", "confirm"])
    outbox.put(OutboxItem.new(session, 4, "left", b"\xff\xd8\xffleft", clock))
    assert [i.event for i in outbox.items()] == ["arrived", "confirm", "left"]
    assert [(d.event, d.reason) for d in outbox.dead_items()] == [("refresh", "outbox full")]


def test_the_oldest_refresh_goes_first(tmp_path):
    outbox, clock, session = fill(tmp_path, ["refresh", "arrived", "refresh"])
    outbox.put(OutboxItem.new(session, 4, "left", b"\xff\xd8\xffleft", clock))
    assert [(i.event, i.seq) for i in outbox.items()] == [("arrived", 2), ("refresh", 3), ("left", 4)]


def test_nothing_is_evicted_when_evicting_every_refresh_still_would_not_make_room(tmp_path):
    outbox, clock, session = fill(tmp_path, ["arrived", "refresh"], max_items=10, max_bytes=60)
    before = snapshot(outbox)
    with pytest.raises(OutboxFull):
        outbox.put(OutboxItem.new(session, 3, "left", b"\xff\xd8\xff" + b"x" * 200, clock))   # bigger than the cap
    assert snapshot(outbox) == before and outbox.dead_items() == []   # the refresh was NOT sacrificed for nothing


def test_a_refused_event_uses_no_slot_and_later_events_keep_the_order(tmp_path):
    outbox, clock, session = fill(tmp_path, ["arrived", "confirm", "left"])
    names = [i.name for i in outbox.items()]
    with pytest.raises(OutboxFull):
        outbox.put(OutboxItem.new(session, 4, "arrived", b"\xff\xd8\xffrefused", clock))
    outbox.remove(outbox.items()[0])                        # the head was delivered: there is room again
    outbox.put(OutboxItem.new(session, 5, "refresh", b"\xff\xd8\xffafter", clock))
    items = outbox.items()
    assert [i.event for i in items] == ["confirm", "left", "refresh"]
    assert [i.name for i in items][:2] == names[1:]         # earlier items untouched
    assert int(items[-1].name.split("-")[0]) == int(names[-1].split("-")[0]) + 1   # numbering continues, no gap


def test_a_disk_write_error_stores_nothing_leaves_no_temp_file_and_evicts_nothing(tmp_path, monkeypatch):
    import outbox as outbox_module

    outbox, clock, session = fill(tmp_path, ["arrived", "refresh", "confirm"])
    before = snapshot(outbox)

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(outbox_module.os, "fsync", disk_full)
    with pytest.raises(OSError):
        outbox.put(OutboxItem.new(session, 4, "left", b"\xff\xd8\xffleft", clock))   # would have evicted the refresh
    monkeypatch.undo()
    assert snapshot(outbox) == before and outbox.dead_items() == []
    assert list((tmp_path / "outbox" / "items").glob("*.tmp")) == []


def make_runner(tmp_path, frames, max_items):
    clock = FakeClock()
    camera, server = FakeCamera(), FakeServer(clock)
    outbox = Outbox(tmp_path / "outbox", clock=clock, max_items=max_items)
    runner = AsyncEventRunner(camera, ScriptedDetector(frames), SETTINGS, outbox=outbox, send_job=server.send_job,
                              send_heartbeat=server.heartbeat, clock=clock, sleep=clock.sleep, wall=clock,
                              rng=lambda: 0.5)
    return runner, clock, camera, server, outbox


def test_runner_reports_a_refused_photo_as_not_stored_and_never_sends_it(tmp_path, caplog):
    runner, clock, camera, server, outbox = make_runner(
        tmp_path, [True] * 4 + [False] * 6 + [True] * 1000, max_items=2)
    server.replies = [ConnectionError("down")] * 1000       # nothing can be delivered: arrived and left fill the outbox
    run_until(runner, clock, 100)                           # arrived (10 s), left (45 s); the second arrived (60 s) is refused
    assert [(i.event, i.seq) for i in outbox.items()] == [("arrived", 1), ("left", 2)]   # intact, in order
    assert runner.store_failures == 1
    assert any(r.levelname == "ERROR" and "저장하지 못했습니다" in r.getMessage() for r in caplog.records)
    assert runner.photos_sent == 0                          # a refused photo is not counted as sent
    assert runner.seq == 0                                  # and it did not use up a sequence number

    server.replies = []                                     # network back
    run_until(runner, clock, 400)
    event_of = {meta["event_id"]: meta["event"] for _, meta, _ in server.attempts}
    assert [event_of[event_id] for event_id in server.accepted] == ["arrived", "left"]   # accepted, in this order
    assert len(server.accepted) == 2                        # only the two stored photos ever reach the server
    assert b"\xff\xd8\xffjpeg-3" not in [jpeg for _, _, jpeg in server.attempts]   # the third capture was never sent


def test_a_confirm_refused_for_lack_of_room_is_not_executed_not_acked_and_is_retried(tmp_path):
    # max_items=2: the arrived takes one slot and keeps the other for its left, so a confirm (which may not use the
    # reserved slot) is refused while the arrived is still stuck in the outbox.
    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 1000, max_items=2)
    # With jitter 0.5 the attempts fail at 10, 12, 16, 24, 40, 70, 100, 130 s and succeed at 160 s: arrived is still
    # in the one-slot outbox when the command arrives (t=120).
    server.replies = [ConnectionError("down")] * 8
    run_until(runner, clock, 61)
    cmd = command(runner.session_id, offset_due=0, at=60)
    server.commands = [cmd]
    run_until(runner, clock, 125)
    assert runner.store_failures >= 1
    assert cmd["command_id"] not in runner.executed         # not executed: nothing was stored
    assert cmd["command_id"] not in runner.acks             # and not cancelled: the server keeps it for another try
    assert "confirm" not in [i.event for i in outbox.items()]

    run_until(runner, clock, 600)                           # arrived gets through, room again, the command comes again
    confirms = [meta for _, meta, _ in server.attempts if meta["event"] == "confirm"]
    assert confirms and {c["command_id"] for c in confirms} == {cmd["command_id"]}
    assert cmd["command_id"] in runner.executed
