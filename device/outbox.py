"""장비의 로컬 Outbox (ADR 0019 결정 9, 설계 6.10). 사진과 메타데이터를 보내기 전에 디스크에 먼저 쓴다.

    <root>/items/<순번>-<event_id>.item   보낼 것. 첫 줄 JSON 머리글 + 줄바꿈 + JPEG 원본 바이트
    <root>/dead/                          포기했거나 읽을 수 없는 것 (조사용으로 최근 몇 개만 남긴다) + .reason
    <root>/executed.json                  이미 실행한 confirm 명령 ID (최근 50개)

규칙
- 쓰기는 임시 파일 -> fsync -> 이름 바꾸기. 전원이 나가도 반쯤 쓰인 .item 이 보낼 것으로 읽히지 않는다.
  남은 .tmp 는 시작할 때 지운다. 머리글에는 길이와 SHA-256 이 있어 잘린 파일은 읽을 수 없는 것으로 분류된다.
- 순번(파일 이름 앞 숫자)은 재시작해도 이어진다. 보내는 순서 = 순번 순서 = 만든 순서.
- 같은 event_id 로 몇 번을 재시도해도 사진 바이트와 메타데이터는 이 파일 그대로다 (다시 찍거나 다시 인코딩하지 않는다).
- 최대 max_items 건 / max_bytes. 자리가 모자라면 **가장 오래된 refresh 만** dead 로 옮겨 자리를 만든다(설계 6.10).
  arrived·confirm·left 는 용량 때문에 절대 지우지 않는다. 정리할 refresh 로도 자리가 안 나면 **새 항목을 OutboxFull 로
  거절한다**(사용자 결정 2026-11-02): 저장하지 않고, 아무것도 지우지 않고, 기존 항목의 내용·순서도 그대로다.
  호출한 쪽은 이를 저장 실패로 다뤄야 한다(보낸 것으로 기록하지 않는다).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pi_client.outbox")

MAX_ITEMS = 20
MAX_BYTES = 50 * 1024 * 1024
LEFT_RESERVE_BYTES = 2 * 1024 * 1024   # one photo's worth (the largest measured sent photo is ~1 MB) kept for a `left`
MAX_DEAD = 20
EXECUTED_KEEP = 50
_NAME = re.compile(r"^(\d+)-[0-9a-f-]+\.item$")


class OutboxFull(Exception):
    """새 항목을 넣을 자리가 없고 정리해도 되는 refresh 도 모자란다. 항목은 저장되지 않았고 기존 항목은 그대로다.
    reason: 'items'(건수 한도) | 'bytes'(바이트 한도). 실제 디스크 오류(OSError)와는 다른 것이다."""

    def __init__(self, message: str, reason: str = "items"):
        super().__init__(message)
        self.reason = reason


def classify_store_error(exc: BaseException) -> str:
    """저장 실패의 종류. 논리 한도(정책 값)와 실제 디스크 문제는 대응이 다르다: 앞은 한도 조정이나 단절 해소, 뒤는 디스크
    정리·교체. full_items | full_bytes | disk_full | disk_read_only | disk_error | error"""
    if isinstance(exc, OutboxFull):
        return f"full_{exc.reason}"
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return "disk_full"
        if exc.errno == errno.EROFS:
            return "disk_read_only"
        return "disk_error"
    return "error"


@dataclass
class OutboxItem:
    event_id: str
    session_id: str
    seq: int
    event: str
    jpeg: bytes
    detected_at: str            # 촬영 시각(장비 시계, 시간대 포함). 재시도해도 바뀌지 않는다
    created: float              # 만든 시각(벽시계 초). 포기 기준(나이)에 쓴다. 재시작해도 유지된다
    command_id: str | None = None
    name: str = field(default="", compare=False)

    @classmethod
    def new(cls, session_id: str, seq: int, event: str, jpeg: bytes, clock, command_id: str | None = None):
        now = clock()
        detected = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        return cls(event_id=str(uuid.uuid4()), session_id=str(session_id), seq=int(seq), event=event, jpeg=jpeg,
                   detected_at=detected, created=now, command_id=str(command_id) if command_id else None)

    def metadata(self) -> dict:
        """서버로 보내는 필드. 재시도마다 똑같다."""
        return {"event_id": self.event_id, "session_id": self.session_id, "seq": self.seq, "event": self.event,
                "command_id": self.command_id, "detected_at": self.detected_at}

    def _header(self) -> bytes:
        header = {**self.metadata(), "created": self.created, "jpeg_len": len(self.jpeg),
                  "sha256": hashlib.sha256(self.jpeg).hexdigest()}
        return json.dumps(header, separators=(",", ":")).encode("utf-8")


@dataclass
class DeadItem:
    name: str
    reason: str
    event: str | None = None
    seq: int | None = None
    event_id: str | None = None


def _parse(raw: bytes, name: str) -> OutboxItem:
    head, sep, jpeg = raw.partition(b"\n")
    if not sep:
        raise ValueError("no header")
    header = json.loads(head.decode("utf-8"))
    if len(jpeg) != header["jpeg_len"] or hashlib.sha256(jpeg).hexdigest() != header["sha256"]:
        raise ValueError("size or checksum mismatch")
    return OutboxItem(event_id=header["event_id"], session_id=header["session_id"], seq=int(header["seq"]),
                      event=header["event"], jpeg=jpeg, detected_at=header["detected_at"],
                      created=float(header["created"]), command_id=header.get("command_id"), name=name)


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)   # 디스크가 가득 차서 실패해도 쓰다 만 파일을 남기지 않는다
        raise


class Outbox:
    def __init__(self, root, clock, max_items: int = MAX_ITEMS, max_bytes: int = MAX_BYTES,
                 reserve_bytes: int = LEFT_RESERVE_BYTES):
        self.root = Path(root)
        self.clock = clock
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes   # one photo's worth kept free for a `left` (see put)
        self._items_dir = self.root / "items"
        self._dead_dir = self.root / "dead"
        self._items_dir.mkdir(parents=True, exist_ok=True)
        self._dead_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self._items_dir.glob("*.tmp"):   # 쓰다가 멈춘 것: 아직 보낼 것이 아니다
            leftover.unlink(missing_ok=True)
        self._cache: dict[str, OutboxItem] = {}
        numbers = [int(m.group(1)) for p in list(self._items_dir.iterdir()) + list(self._dead_dir.iterdir())
                   if (m := _NAME.match(p.name))]
        self._counter = max(numbers, default=0)

    # --- 읽기 ---------------------------------------------------------------------------------------

    def items(self) -> list[OutboxItem]:
        """보낼 것들을 만든 순서대로. 읽을 수 없는 파일은 dead 로 옮긴다."""
        names = sorted(p.name for p in self._items_dir.iterdir() if p.name.endswith(".item"))
        for stale in set(self._cache) - set(names):
            del self._cache[stale]
        result = []
        for name in names:
            item = self._cache.get(name)
            if item is None:
                try:
                    item = _parse((self._items_dir / name).read_bytes(), name)
                except (OSError, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
                    self._quarantine(name, "unreadable outbox file")
                    continue
                self._cache[name] = item
            result.append(item)
        return result

    def dead_items(self) -> list[DeadItem]:
        found = []
        for path in sorted(p for p in self._dead_dir.iterdir() if p.name.endswith(".item")):
            reason_file = path.with_name(path.name + ".reason")
            reason = reason_file.read_text(encoding="utf-8") if reason_file.exists() else "unknown"
            try:
                item = _parse(path.read_bytes(), path.name)
                found.append(DeadItem(path.name, reason, item.event, item.seq, item.event_id))
            except (OSError, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
                found.append(DeadItem(path.name, reason))
        return found

    # --- 쓰기 ---------------------------------------------------------------------------------------

    def put(self, item: OutboxItem, reserve_for_left: bool = False) -> OutboxItem:
        """디스크에 안전하게 저장한다. OutboxFull(자리 없음) 또는 OSError(쓰기 실패)면 아무것도 바뀌지 않는다:
        항목은 저장되지 않았고, refresh 도 지워지지 않았고, 순번도 쓰지 않았다.

        reserve_for_left (결정 2026-11-02, 방식 C): 열린 세션이 있는 동안 `left` 가 아닌 항목(arrived·confirm·refresh)은
        저장한 뒤에도 빈 칸 하나(와 사진 1장 분량의 바이트)가 남을 때만 받는다. 그 칸은 그 세션의 `left` 몫이다.
        그래서 arrived 는 자기 칸과 left 칸, 두 칸이 비어 있어야 한다. `left` 는 reserve_for_left=False 로 넣는다.
        (max_items 가 2 보다 작으면 arrived 를 받을 수 없다.)"""
        victims = self._plan_room(item, reserve_for_left)   # 자리를 만들 수 없으면 여기서 OutboxFull (아직 아무것도 안 건드림)
        name = f"{self._counter + 1:012d}-{item.event_id}.item"
        _atomic_write(self._items_dir / name, item._header() + b"\n" + item.jpeg)   # 실패하면 OSError, 아직 아무것도 안 지움
        self._counter += 1
        item.name = name
        self._cache[name] = item
        for victim in victims:                    # 새 항목이 디스크에 안전히 있을 때에만 오래된 refresh 를 정리한다
            self.discard(victim, "outbox full")
        return item

    def remove(self, item: OutboxItem) -> None:
        (self._items_dir / item.name).unlink(missing_ok=True)
        self._cache.pop(item.name, None)

    def discard(self, item: OutboxItem, reason: str) -> None:
        """포기: 지우지 않고 dead 로 옮겨 조사할 수 있게 둔다 (최근 MAX_DEAD 건)."""
        self._cache.pop(item.name, None)
        self._quarantine(item.name, reason)

    def _quarantine(self, name: str, reason: str) -> None:
        source = self._items_dir / name
        target = self._dead_dir / name
        try:
            os.replace(source, target)
            (self._dead_dir / (name + ".reason")).write_text(reason, encoding="utf-8")
        except OSError:
            log.exception("outbox: %s 를 dead 로 옮기지 못했습니다 (%s)", name, reason)
            return
        log.warning("outbox: %s 를 보내지 않고 보관합니다 (%s)", name, reason)
        dead = sorted(p for p in self._dead_dir.iterdir() if p.name.endswith(".item"))
        for old in dead[:-MAX_DEAD]:
            old.unlink(missing_ok=True)
            old.with_name(old.name + ".reason").unlink(missing_ok=True)

    def _plan_room(self, new: OutboxItem, reserve_for_left: bool = False) -> list[OutboxItem]:
        """새 항목이 들어가려면 지워야 하는 refresh 들(오래된 순). 지워도 자리가 안 나면 OutboxFull(reason=items|bytes).
        계획만 세우고 지우지 않는다: 불가능하다고 판단했을 때 refresh 를 헛되이 잃지 않도록. 정리할 수 있는 것은 refresh
        뿐이고, left 몫으로 남겨 둘 칸을 만들겠다고 다른 중요 이벤트를 지우는 일은 없다."""
        items = self.items()
        refreshes = [i for i in items if i.event == "refresh"]
        count = len(items) + 1 + (1 if reserve_for_left else 0)
        size = sum(len(i.jpeg) for i in items) + len(new.jpeg) + (self.reserve_bytes if reserve_for_left else 0)
        victims: list[OutboxItem] = []
        while count > self.max_items or size > self.max_bytes:
            if not refreshes:
                reason = "items" if count > self.max_items else "bytes"
                why = " (left 칸을 남겨 두어야 해서)" if reserve_for_left else ""
                raise OutboxFull(f"{new.event}(seq {new.seq}) 를 넣을 자리가 없습니다{why}: {len(items)}건 "
                                 f"{sum(len(i.jpeg) for i in items) // 1024} KB 가 차 있고 정리할 refresh 가 없습니다",
                                 reason=reason)
            victim = refreshes.pop(0)
            victims.append(victim)
            count -= 1
            size -= len(victim.jpeg)
        return victims


class ExecutedCommands:
    """이미 실행한 confirm 명령 ID (최근 EXECUTED_KEEP 개). 서버가 같은 명령을 다시 보내도(응답 유실, 재시작 뒤)
    두 번 촬영하지 않고, 실행한 명령을 취소(ack)하지도 않는다."""

    def __init__(self, path):
        self.path = Path(path)
        self._ids: list[str] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._ids = [str(v) for v in data][-EXECUTED_KEEP:]
        except (OSError, ValueError):
            pass

    def __contains__(self, command_id) -> bool:
        return str(command_id) in self._ids

    def add(self, command_id) -> None:
        command_id = str(command_id)
        if command_id in self._ids:
            return
        self._ids = (self._ids + [command_id])[-EXECUTED_KEEP:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(self._ids).encode("utf-8"))


class OutboxHealth:
    """outbox/health.json: 저장하지 못한 사진과 포기한 사진의 기록. 프로세스가 다시 시작해도 남는다(결정 2026-11-02).

    저장 실패(`record_failure`)는 종류별(full_items | full_bytes | disk_full | disk_read_only | disk_error), 이벤트별로 센다.
    논리 한도와 실제 디스크 문제는 대응이 다르므로 섞지 않는다. `left_of_known_session_refused` 는 서버가 아는 세션의
    left 를 저장하지 못한 횟수로, 서버의 사건이 열린 채 남을 수 있는 가장 중요한 지표다. `given_up` 은 나이 때문에 포기한
    항목 수다(left 의 30분 포기는 이번에 바꾸지 않았고 미해결 위험이라 보이게만 한다).

    쓰기는 임시 파일 + 이름 바꾸기이고 실패해도 예외를 올리지 않는다: 디스크가 가득 찬 바로 그 순간에는 이 파일도 못 쓸 수
    있다(그때는 메모리에 세고 다음 쓰기 때 함께 기록하며, 그 사이에 프로세스가 죽으면 그 횟수는 사라진다)."""

    VERSION = 1

    def __init__(self, path, clock):
        self.path = Path(path)
        self.clock = clock
        self.persist_error: str | None = None
        self.data = self._load()

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).astimezone().isoformat(timespec="seconds")

    def _fresh(self) -> dict:
        now = self._now()
        return {"version": self.VERSION, "since": now, "updated_at": now,
                "failures": {"total": 0, "by_kind": {}, "by_event": {}},
                "left_of_known_session_refused": 0, "given_up": {}, "last_failure": None}

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not (isinstance(data, dict) and isinstance(data.get("failures"), dict)
                    and isinstance(data.get("given_up"), dict)):
                raise ValueError("unexpected layout")
            return data
        except FileNotFoundError:
            return self._fresh()
        except (OSError, ValueError):
            log.warning("outbox/health.json 을 읽을 수 없어 새로 시작합니다.")
            return self._fresh()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

    def record_failure(self, event: str, seq: int, kind: str, detail: str, left_of_known_session: bool = False) -> None:
        failures = self.data["failures"]
        failures["total"] = failures.get("total", 0) + 1
        for bucket, key in (("by_kind", kind), ("by_event", event)):
            counts = failures.setdefault(bucket, {})
            counts[key] = counts.get(key, 0) + 1
        if left_of_known_session:
            self.data["left_of_known_session_refused"] = self.data.get("left_of_known_session_refused", 0) + 1
        self.data["last_failure"] = {"at": self._now(), "event": event, "seq": seq, "kind": kind,
                                     "detail": str(detail)[:300], "left_of_known_session": left_of_known_session}
        self._persist()

    def record_given_up(self, event: str) -> None:
        given_up = self.data.setdefault("given_up", {})
        given_up[event] = given_up.get(event, 0) + 1
        self._persist()

    def _persist(self) -> None:
        self.data["updated_at"] = self._now()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1).encode("utf-8"))
            self.persist_error = None
        except OSError as exc:
            self.persist_error = f"{type(exc).__name__}: {exc}"
            log.error("outbox/health.json 을 쓰지 못했습니다(메모리에는 세어 두었고 다음 쓰기 때 함께 기록합니다): %s",
                      self.persist_error)
