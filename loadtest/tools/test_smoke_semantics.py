"""스모크·부하 테스트가 서버에 일으키는 "의미 있는 변화"를 실제 백엔드 코드(접수 API + 큐 + 판정 Worker + 사건 서비스)로 로컬에서 확인한다.
SQLite 메모리 DB, 가짜 큐, 가짜 추론(사진 바이트로 판정) — AWS·클러스터·실제 DB 에 접속하지 않는다. 백엔드의 시험 하네스를 그대로 가져다 쓴다.

확인하려는 것
  1. 허가 판정 사진을 refresh 로 연속 전송해도(첫 장 arrived, 이후 refresh) 전부 접수·판정되고, 위반·알림·확인 명령이 생기지 않는다.
  2. 활성 사건(ended_at IS NULL)이 남은 장비는 — 상태가 RESOLVED 여도 — 부하 사진이 그 사건을 종료시킨다(무신호 10분 초과 또는 허가 3연속).
     RPI-E2E-TEST-01 의 위반 #5 가 정확히 이 상태다. 그래서 그 장비를 부하·스모크에 쓰면 #5 가 바뀐다.
"""
import sys
import uuid
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402

# 백엔드 시험 하네스(픽스처)를 가져온다. 가져온 이름이 이 모듈의 픽스처가 된다.
from tests.conftest import _app_loggers_enabled, app, auth, client, ids  # noqa: E402,F401
from tests.test_worker import (GANGNAM, PANGYO, ScriptedInference, app_overrides, clock, drain, env, send, worker,  # noqa: E402,F401
                               _machine, _silence)

from app.extensions import db  # noqa: E402
from app.models import (DetectionJob, DetectionResult, DeviceCommand, DeviceSession, EndReason, JobStatus, Machine, Notification,  # noqa: E402
                        Violation, ViolationHistory, ViolationStatus, utcnow)

HQ = "RPI-B-HQ-01"
LOGIS = "RPI-B-LOGIS-01"


def counts(app):
    with app.app_context():
        t = lambda model: db.session.scalar(select(func.count()).select_from(model))  # noqa: E731
        return {"violations": t(Violation), "notifications": t(Notification), "history": t(ViolationHistory), "commands": t(DeviceCommand),
                "jobs": t(DetectionJob), "results": t(DetectionResult), "sessions": t(DeviceSession)}


def test_ten_permitted_photos_with_consecutive_refresh_events_are_all_judged_without_side_effects(app, send, worker):
    before = counts(app)
    serials, per_device = [PANGYO, HQ, LOGIS], [4, 3, 3]            # 사진 10장: 스모크와 같은 4/3/3 분배
    session = {s: uuid.uuid4() for s in serials}
    for i in range(max(per_device)):                                # k6 처럼 장비를 번갈아 가며 보낸다
        for d, serial in enumerate(serials):
            if i < per_device[d]:
                send(serial=serial, event="arrived" if i == 0 else "refresh", session=session[serial], seq=i + 1, verdict="permitted")
    assert drain(app, worker) == ["done"] * 10                      # 접수 10건 모두 정상 판정(stale·rejected 없음)
    after = counts(app)
    assert after["jobs"] - before["jobs"] == 10 and after["results"] - before["results"] == 10 and after["sessions"] - before["sessions"] == 3
    # 위반·알림·이력·확인 명령은 하나도 생기지 않는다
    for key in ("violations", "notifications", "history", "commands"):
        assert after[key] == before[key], key
    with app.app_context():
        jobs = db.session.scalars(select(DetectionJob).order_by(DetectionJob.received_at)).all()
        assert {j.status for j in jobs} == {JobStatus.DONE}
        assert all(r.applied and r.verdict == "permitted" for r in db.session.scalars(select(DetectionResult)))
        for serial, n in zip(serials, per_device):                   # 장비별 마지막 반영 seq = 보낸 사진 수, 현재 세션 = 이 실행의 세션
            machine = db.session.scalars(select(Machine).where(Machine.serial_number == serial)).one()
            assert machine.last_applied_seq == n and str(machine.current_session_id) == str(session[serial])
            assert machine.pending_violation_count == 0
            seqs = [j.seq for j in jobs if j.machine_id == machine.id]
            assert seqs == list(range(1, n + 1))                      # 장비별 접수 순서 = seq 순서


def test_warmup_session_then_measured_session_on_the_same_device_is_all_applied(app, send, worker):
    """scenarios_p2.js 의 워밍업(별도 세션, seq 1부터) 뒤에 측정 세션(새 세션, seq 1부터)이 와도 전부 반영된다. 이전 세션의 판정이 끝나기 전에 와도 마찬가지."""
    before = counts(app)
    warm, measured = uuid.uuid4(), uuid.uuid4()
    send(serial=HQ, event="arrived", session=warm, seq=1, verdict="permitted")
    assert drain(app, worker) == ["done"]                                       # 워밍업이 먼저 끝난 경우
    for n in (1, 2, 3):
        send(serial=HQ, event="arrived" if n == 1 else "refresh", session=measured, seq=n, verdict="permitted")
    assert drain(app, worker) == ["done"] * 3
    other_warm, other_measured = uuid.uuid4(), uuid.uuid4()                      # 워밍업이 끝나기 전에 측정이 시작된 경우(대기열에 둘 다 있다)
    send(serial=LOGIS, event="arrived", session=other_warm, seq=1, verdict="permitted")
    for n in (1, 2):
        send(serial=LOGIS, event="arrived" if n == 1 else "refresh", session=other_measured, seq=n, verdict="permitted")
    assert drain(app, worker) == ["done"] * 3
    after = counts(app)
    assert after["jobs"] - before["jobs"] == 7 and after["sessions"] - before["sessions"] == 4
    assert after["violations"] == before["violations"] and after["notifications"] == before["notifications"]


def test_refresh_as_the_first_photo_of_a_session_is_accepted_too(app, send, worker):
    send(serial=HQ, event="refresh", session=uuid.uuid4(), seq=1, verdict="permitted")
    assert drain(app, worker) == ["done"]


def _make_resolved_but_not_ended_episode(app, send, worker):
    """RPI-E2E-TEST-01 의 위반 #5 와 같은 상태: 상태 RESOLVED, ended_at 은 비어 있다(사건 종료와 관리자 처리는 별개)."""
    a = uuid.uuid4()
    send(serial=GANGNAM, session=a, seq=1, event="arrived", verdict="violation")
    send(serial=GANGNAM, session=a, seq=2, event="confirm", verdict="violation")
    drain(app, worker)
    with app.app_context():
        v = db.session.scalars(select(Violation).where(Violation.machine_id == _machine().id, Violation.created_at >= utcnow() - timedelta(hours=1))).one()
        v.status = ViolationStatus.RESOLVED
        db.session.commit()
        assert v.ended_at is None
        return v.id, db.session.scalar(select(func.count()).select_from(ViolationHistory).where(ViolationHistory.violation_id == v.id))


def test_load_photos_from_a_device_with_an_unended_resolved_episode_close_it_after_ten_minutes_of_silence(app, send, worker):
    vid, history0 = _make_resolved_but_not_ended_episode(app, send, worker)
    _silence(app, 30)                                                # 마지막 신호가 30분 전(실제 #5 도 07:07 이후 무신호)
    send(serial=GANGNAM, session=uuid.uuid4(), seq=1, event="arrived", verdict="permitted")
    assert drain(app, worker) == ["done"]
    with app.app_context():
        v = db.session.get(Violation, vid)
        assert v.ended_at is not None and v.end_reason == EndReason.SIGNAL_LOST          # 첫 사진 하나로 기존 위반 행이 바뀐다
        assert v.status == ViolationStatus.RESOLVED
        assert db.session.scalar(select(func.count()).select_from(ViolationHistory).where(ViolationHistory.violation_id == vid)) == history0 + 1


def test_three_consecutive_permitted_photos_close_the_episode_even_without_silence(app, send, worker):
    vid, history0 = _make_resolved_but_not_ended_episode(app, send, worker)
    session = uuid.uuid4()
    for n in (1, 2):
        send(serial=GANGNAM, session=session, seq=n, event="arrived" if n == 1 else "refresh", verdict="permitted")
    drain(app, worker)
    with app.app_context():
        assert db.session.get(Violation, vid).ended_at is None                          # 2장까지는 그대로
    send(serial=GANGNAM, session=session, seq=3, event="refresh", verdict="permitted")
    drain(app, worker)
    with app.app_context():
        v = db.session.get(Violation, vid)
        assert v.ended_at is not None and v.end_reason == EndReason.STICKER_DETECTED     # 3장째에 종료
        assert db.session.scalar(select(func.count()).select_from(ViolationHistory).where(ViolationHistory.violation_id == vid)) == history0 + 1


def test_a_device_without_an_active_episode_is_untouched_by_any_number_of_permitted_photos(app, send, worker):
    before = counts(app)
    session = uuid.uuid4()
    for n in range(1, 8):
        send(serial=LOGIS, session=session, seq=n, event="arrived" if n == 1 else "refresh", verdict="permitted")
    assert drain(app, worker) == ["done"] * 7
    after = counts(app)
    assert (after["violations"], after["history"], after["notifications"]) == (before["violations"], before["history"], before["notifications"])
