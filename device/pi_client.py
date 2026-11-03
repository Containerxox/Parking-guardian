"""Parking Guardian 장비 클라이언트 (Raspberry Pi + 카메라 모듈).

두 가지 모드가 있다 (PG_MODE). 위반인지는 어느 모드든 서버가 판단한다.

event (기본, 실제 운영 모드. AI_FINAL_DESIGN.md, docs/adr/0014): Pi 가 주차면 한 칸의 점유를 판정해서
상황이 바뀔 때만 사진을 보낸다. Pi 는 "언제 보낼지"만 정하고 "위반인지"는 서버가 정한다.

    5초마다 저해상도 프레임 -> YOLO 차량 탐지 -> ROI 점유 판정 (vehicle_detector.py)
      비점유 -> 점유 (3프레임 연속):  arrived 사진 전송. 응답이 violation_pending 이면 60초 뒤 confirm 사진
      점유 중:                        60초마다 Heartbeat, 10분마다 refresh 사진
      점유 -> 비점유 (6프레임 연속):  left 사진 전송 (빈 자리)
      비점유 중:                      60초마다 Heartbeat 만. 사진 없음
    전송에 실패하면 로그만 남긴다. 재시도 큐는 없다 (MVP). 주차 중이면 다음 refresh 때 사진이 다시 간다.
    (PG_PROTOCOL=async 는 Phase 2: 로컬 Outbox 에 저장하고 같은 내용으로 재시도한다. AsyncEventRunner, outbox.py, 아래 설정 참고)

interval (비교·성능 측정용 보조 모드, 지금까지의 방식): 1분마다 사진을 찍어 서버로 보낸다. 차가 없어도 보낸다.
운영에는 쓰지 않고, 이벤트 모드와의 Before/After 비교(backend/scripts/measure.py, docs/measurements/)를 위해 남겨 둔다.

    촬영 -> 크기 줄이기 -> 서버 전송 -> 응답에 따라 LED 켜기/끄기 -> 다음 주기까지 대기

기존 image_send.py 와 달라진 점
- Pi 에서 AI 모델(torch, YOLOv5)을 돌리지 않는다. 차량이 있는지도 서버가 판단한다.
- 차량 부분만 잘라 보내지 않고 사진 전체를 보낸다. 서버는 가장 큰 앞유리 하나만 보므로
  옆 칸 차량이 위반으로 잡히지 않는다.
- 차량이 없어도 매번 보낸다. 서버가 "차량이 나갔다"와 "장비가 살아 있다"를 알 수 있다.
  (event 모드에서는 매분 Heartbeat 가 "살아 있다"를 맡는다. 서버의 연결 끊김 판정(EPISODE_GAP_MINUTES, 10분)은
  마지막 사진과 마지막 Heartbeat 중 더 최근 시각을 기준으로 하므로, 주차 중 사진이 10분마다만 가도 끊김이 아니다.)
- 장비 인증 키(X-Device-Key)로 인증한다. 회사, 주차장, 시리얼 번호는 보내지 않는다.
- 오류가 나도 카메라를 끄지 않고 다음 주기에 다시 시도한다.
- 찍은 사진을 Pi 에 쌓아 두지 않는다.

설정은 환경변수로 한다 (device/README.md 참고).

    PG_SERVER_URL   서버 API 주소. 예: https://api.example.com/api/v1
    PG_DEVICE_KEY   장비 등록 때 한 번 표시되는 인증 키 (pgk_...)
    PG_ZONE_NAME    (선택) 이 카메라가 보는 구역 이름. 장비가 구역 하나에 연결돼 있으면 생략
    PG_INTERVAL     (선택) interval 모드의 촬영 주기(초). 기본 60
    PG_SEND_WIDTH   (선택) 전송할 사진의 최대 가로 픽셀. 이보다 크면 줄여서 보낸다. 기본 1280
    PG_JPEG_QUALITY (선택) 줄일 때 쓰는 JPEG 품질. 기본 92
    PG_LED_PIN      (선택) LED 가 연결된 BCM 핀 번호. 기본 4

    PG_MODE               event | interval. 기본 event (interval 은 비교·측정용)
    PG_PROTOCOL           (event) sync | async. 기본 sync (Phase 1: POST /device/detections, 판정을 응답으로 받음).
                          async 는 Phase 2: Outbox 에 저장 후 POST /device/detection-jobs, confirm 은 Heartbeat 명령으로 받는다.
                          서버가 QUEUE_BACKEND 를 켜고 Worker 가 돌 때만 쓴다.
    PG_OUTBOX_DIR         (async) 로컬 Outbox 디렉터리. 기본 ~/.parking_guardian/outbox
    PG_DETECT_INTERVAL    (event) 점유 판정 프레임 간격(초). 기본 5
    PG_HEARTBEAT_INTERVAL (event) Heartbeat 간격(초). 기본 60
    PG_REFRESH_INTERVAL   (event) 주차 중 확인 사진 간격(초). 기본 600
    PG_CONFIRM_DELAY      (event) violation_pending 응답 뒤 확인 사진까지의 시간(초). 기본 60
    PG_DETECTOR_MODEL     (event) 차량 탐지 모델. 기본 yolo26s_ncnn_model (폴더) 또는 yolo26s.pt
    PG_DETECTOR_IMGSZ     (event) 탐지 입력 크기. 기본 480
    PG_ROI, PG_OCC_*      (event) 주차면 ROI 와 판정 기준값. vehicle_detector.py 와 README 참고

PC 에서 카메라 없이 전송만 시험하기:

    python device/pi_client.py --image backend/uploads/sample_b.jpg --count 40 --detect-interval 0.5
    python device/pi_client.py --mode interval --image backend/uploads/sample_b.jpg --once
"""
from __future__ import annotations  # Raspberry Pi OS 의 Python 3.9 에서도 동작하도록

import argparse
import logging
import os
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # vehicle_detector 를 어디서 실행해도 찾도록

from outbox import (ExecutedCommands, Outbox, OutboxFull, OutboxHealth, OutboxItem,  # noqa: E402
                    classify_store_error)

log = logging.getLogger("pi_client")

CAPTURE_PATH = "/tmp/parking_guardian_capture.jpg"  # 매번 같은 파일을 덮어써서 저장 공간이 차지 않게 한다

# 서버가 돌려주는 result 값. docs/design/API.md 의 "장비용 API" 참고
VIOLATION_RESULTS = ("violation", "violation_ongoing")   # 위반 차량이 기록되어 있는 상태 -> LED 켬


def get_pi_serial() -> str:
    """Raspberry Pi 의 시리얼 번호. 서비스 운영자가 장비를 등록할 때 이 값을 쓴다."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.strip().startswith("Serial"):
                    return line.strip().split(":")[-1].strip()
    except OSError:
        pass
    return "unknown"


class Hardware:
    """카메라와 LED. Raspberry Pi 에서만 동작하는 라이브러리는 여기서만 불러온다."""

    def __init__(self, led_pin: int, lores: bool = False):
        from picamera2 import Picamera2
        import RPi.GPIO as GPIO

        Picamera2.set_logging(Picamera2.ERROR)
        os.environ["LIBCAMERA_LOG_LEVELS"] = "3"

        self.gpio = GPIO
        self.led_pin = led_pin
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(led_pin, GPIO.OUT)

        # 카메라 설정은 기존 코드에서 쓰던 값 그대로다. 이벤트 모드는 점유 판정용 저해상도(lores) 스트림을
        # 함께 켠다. 전송 사진은 두 모드 모두 main 스트림(3280x2464)에서 찍는다.
        self.camera = Picamera2()
        if lores:
            self.camera.configure(self.camera.create_preview_configuration(
                main={"size": (3280, 2464)}, lores={"size": (640, 480), "format": "YUV420"}))
        else:
            self.camera.configure(self.camera.create_preview_configuration({"size": (3280, 2464)}))
        self.camera.set_controls({
            "AeEnable": True,
            "AnalogueGain": 8.0,
            "ExposureTime": 10000,
            "AwbEnable": True,
            "Contrast": 1.5,
            "Brightness": 0.1,
        })
        time.sleep(0.5)
        self.camera.start()
        self.set_led(False)

    def capture(self) -> str:
        self.camera.capture_file(CAPTURE_PATH)
        return CAPTURE_PATH

    def capture_frame(self):
        """점유 판정용 저해상도 BGR 프레임 (640x480). YUV420 -> BGR 변환은 Pi 에서 확인이 필요하다."""
        import cv2

        yuv = self.camera.capture_array("lores")
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

    def set_led(self, on: bool) -> None:
        self.gpio.output(self.led_pin, bool(on))

    def close(self) -> None:
        try:
            self.set_led(False)
            self.camera.stop()
        finally:
            self.gpio.cleanup()


class FakeHardware:
    """--image 로 실행할 때 쓴다. 카메라 대신 주어진 파일을 쓰고 LED 는 로그로만 남긴다."""

    def __init__(self, image_path: str):
        self.image_path = image_path

    def capture(self) -> str:
        return self.image_path

    def capture_frame(self):
        import cv2

        frame = cv2.imread(self.image_path)
        if frame is None:
            raise RuntimeError(f"사진을 읽을 수 없습니다: {self.image_path}")
        return cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))

    def set_led(self, on: bool) -> None:
        log.info("LED %s", "ON" if on else "OFF")

    def close(self) -> None:
        pass


def load_jpeg(path: str, max_width: int, jpeg_quality: int = 92) -> bytes:
    """사진을 읽어 가로가 max_width 를 넘으면 비율을 유지해 줄이고 JPEG 바이트로 돌려준다.

    원본(3280x2464)을 1분마다 그대로 보내면 하루에 몇 GB 가 된다. 줄여 보내면 전송량이 크게 준다.
    다만 스티커는 사진에서 아주 작은 부분이라 너무 줄이거나 강하게 압축하면 탐지가 안 될 수 있다.
    실제 카메라 사진으로 확인하면서 PG_SEND_WIDTH 와 PG_JPEG_QUALITY 를 조정한다.
    """
    with open(path, "rb") as f:
        original = f.read()
    try:
        import cv2
    except ImportError:  # OpenCV 가 없으면 줄이지 않고 원본을 보낸다
        return original

    image = cv2.imread(path)
    if image is None:
        raise RuntimeError(f"사진을 읽을 수 없습니다: {path}")
    height, width = image.shape[:2]
    if width <= max_width:
        # 줄일 필요가 없으면 원본 바이트를 그대로 보낸다. 다시 압축하기만 해도 작은 스티커의
        # 탐지 신뢰도가 크게 떨어지는 것을 확인했다 (같은 사진에서 0.64 -> 0.29).
        return original

    scale = max_width / width
    image = cv2.resize(image, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise RuntimeError("JPEG 변환에 실패했습니다.")
    return encoded.tobytes()


def send_photo(server_url: str, device_key: str, jpeg: bytes, zone_name: str | None,
               event: str | None = None, timeout=(5, 60)) -> dict | None:
    """사진을 서버로 보내고 응답 JSON 을 돌려준다. 실패하면 이유를 로그에 남기고 None 을 돌려준다.

    event 는 이벤트 모드에서 사진을 보내는 이유(arrived / confirm / refresh / left). interval 모드는 None.
    """
    now = datetime.now().astimezone()  # 시간대 정보를 포함해 보낸다. 예: 2026-10-01T21:03:00+09:00
    form = {"detected_at": now.isoformat(timespec="seconds")}
    if zone_name:
        form["zone_name"] = zone_name
    if event:
        form["event"] = event
    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/device/detections",
            headers={"X-Device-Key": device_key},
            files={"file": (now.strftime("%Y%m%d_%H%M%S.jpg"), jpeg, "image/jpeg")},
            data=form,
            timeout=timeout,  # 연결 5초, 응답 60초. 서버의 첫 추론은 모델을 불러오느라 오래 걸린다
        )
    except requests.RequestException as exc:
        log.warning("전송 실패 (다음 주기에 다시 시도): %s", exc)
        return None

    if response.status_code in (200, 201):
        try:
            return response.json()
        except ValueError:
            log.warning("서버 응답을 해석할 수 없습니다: %s", response.text[:200])
            return None

    try:
        message = response.json().get("error", {}).get("message", "")
    except ValueError:
        message = response.text[:200]
    if response.status_code == 401:
        log.error("인증 실패(401): PG_DEVICE_KEY 를 확인하세요. 키를 재발급했다면 새 키로 바꿔야 합니다. %s", message)
    elif response.status_code == 403:
        log.error("거부됨(403): 장비, 주차장 또는 회사가 비활성 상태입니다. %s", message)
    elif response.status_code == 503:
        log.warning("서버의 AI 추론을 사용할 수 없습니다(503). 다음 주기에 다시 시도합니다. %s", message)
    else:
        log.warning("서버 오류(%s): %s", response.status_code, message)
    return None


def send_heartbeat(server_url: str, device_key: str) -> bool:
    """장비가 살아 있음을 알린다. 이벤트 모드에서 매분 보낸다. 실패하면 로그만 남긴다."""
    try:
        response = requests.post(f"{server_url.rstrip('/')}/device/heartbeat",
                                 headers={"X-Device-Key": device_key}, timeout=(5, 15))
    except requests.RequestException as exc:
        log.warning("Heartbeat 전송 실패: %s", exc)
        return False
    if response.status_code != 200:
        log.warning("Heartbeat 서버 오류(%s)", response.status_code)
        return False
    return True


# --- Phase 2 비동기 프로토콜 (ADR 0019, 설계 6.10). PG_PROTOCOL=async 일 때만 쓴다. 기본(sync)은 위의 Phase 1 그대로 ---

RETRYABLE_STATUS = (404, 408, 425, 429)   # 404: 서버에 아직 비동기 접수가 켜져 있지 않음(QUEUE_BACKEND=none). 사진은 지키고 기다린다
MIN_RETRY_DELAY = 0.5                     # full jitter 가 0 에 가까워도 바쁜 반복이 되지 않도록
MAX_BACKOFF = 60.0
GIVE_UP_AFTER = {"arrived": 1800.0, "left": 1800.0, "refresh": 600.0, "confirm": 600.0}   # 설계 6.10: 촬영 뒤 초


@dataclass
class SendOutcome:
    """접수 요청 한 번의 결과.  accepted(202) | duplicate(200) 는 성공, retry 는 같은 내용으로 다시, drop 은 포기."""

    kind: str
    status: int | None = None
    retry_after: float | None = None
    detail: str = ""


def classify_job_response(status: int, body, retry_after=None) -> SendOutcome:
    """POST /device/detection-jobs 응답을 해석한다 (docs/design/API.md).

    202 새로 접수, 200 같은 event_id 가 이미 접수됨(응답 유실 뒤 재시도) -> 둘 다 성공.
    네트워크 오류·5xx·429·408·425·404 는 재시도, 그 밖의 4xx(400, 401, 403, 409, 413 ...)는 다시 보내도 같으므로 포기."""
    code = ""
    if isinstance(body, dict):
        error = body.get("error")
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
    if status == 202:
        return SendOutcome("accepted", status)
    if status == 200:
        return SendOutcome("duplicate", status)
    delay = None
    try:
        delay = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        delay = None   # HTTP 날짜 형식 등은 무시하고 백오프를 쓴다
    if status >= 500 or status in RETRYABLE_STATUS:
        return SendOutcome("retry", status, retry_after=delay, detail=code)
    return SendOutcome("drop", status, detail=code)


def post_job(server_url: str, device_key: str, item, timeout=(5, 60)) -> SendOutcome:
    """Outbox 항목 하나를 보낸다. 네트워크 오류(requests.RequestException)는 그대로 올려 보내며 호출한 쪽이 재시도한다."""
    form = {k: str(v) for k, v in item.metadata().items() if v is not None}
    response = requests.post(f"{server_url.rstrip('/')}/device/detection-jobs", headers={"X-Device-Key": device_key},
                             files={"file": (f"{item.event_id}.jpg", item.jpeg, "image/jpeg")}, data=form,
                             timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = None
    return classify_job_response(response.status_code, body, response.headers.get("Retry-After"))


def post_heartbeat(server_url: str, device_key: str, body: dict) -> dict | None:
    """Phase 2 Heartbeat: session_id, occupied, last_seq, acked_commands 를 보내고 응답 JSON 을 돌려준다.
    실패하면 None (다음 주기가 곧 재시도다. ack 는 성공할 때까지 계속 보낸다)."""
    try:
        response = requests.post(f"{server_url.rstrip('/')}/device/heartbeat", json=body,
                                 headers={"X-Device-Key": device_key}, timeout=(5, 15))
    except requests.RequestException as exc:
        log.warning("Heartbeat 전송 실패: %s", exc)
        return None
    if response.status_code != 200:
        log.warning("Heartbeat 서버 오류(%s)", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError:
        log.warning("Heartbeat 응답을 해석할 수 없습니다: %s", response.text[:200])
        return None
    return data if isinstance(data, dict) else None


def parse_protocol(env=os.environ) -> str:
    value = (env.get("PG_PROTOCOL") or "sync").strip().lower()
    if value not in ("sync", "async"):
        raise SystemExit(f"PG_PROTOCOL 은 sync 또는 async 입니다: {value!r}")
    return value


@dataclass
class EventSettings:
    """이벤트 모드의 시간 설정(초). 전부 환경변수로 바꿀 수 있다."""

    detect_interval: float = 5.0
    heartbeat_interval: float = 60.0
    refresh_interval: float = 600.0
    confirm_delay: float = 60.0

    @classmethod
    def from_env(cls, env=os.environ) -> "EventSettings":
        return cls(
            detect_interval=float(env.get("PG_DETECT_INTERVAL", cls.detect_interval)),
            heartbeat_interval=float(env.get("PG_HEARTBEAT_INTERVAL", cls.heartbeat_interval)),
            refresh_interval=float(env.get("PG_REFRESH_INTERVAL", cls.refresh_interval)),
            confirm_delay=float(env.get("PG_CONFIRM_DELAY", cls.confirm_delay)),
        )


class EventModeRunner:
    """이벤트 모드의 상태 기계. 카메라, 탐지기, 전송 함수를 바깥에서 받아서 테스트와 측정에서도 그대로 쓴다.

    camera:    capture_frame() -> 점유 판정용 BGR 프레임, capture() -> 전송할 JPEG bytes, set_led(bool)
    detector:  process_frame(frame) 을 부르면 detector.tracker.state 가 현재 점유 판단(True/False/None)이 된다
               (vehicle_detector.VehicleDetector 또는 같은 모양의 대역)
    send_photo(jpeg, event) -> 서버 응답 dict 또는 None.  send_heartbeat() -> bool
    clock / sleep 은 테스트에서 가짜 시간을 넣기 위한 것이다.
    """

    def __init__(self, camera, detector, settings: EventSettings, send_photo, send_heartbeat,
                 clock=time.monotonic, sleep=time.sleep):
        self.camera = camera
        self.detector = detector
        self.settings = settings
        self.send_photo = send_photo
        self.send_heartbeat = send_heartbeat
        self.clock = clock
        self.sleep = sleep

        now = clock()
        self.occupied: bool | None = None   # 장비가 마지막으로 서버에 알린 점유 상태. None = 부팅 직후
        self.next_frame = now
        self.next_heartbeat = now
        self.confirm_due: float | None = None
        self.refresh_due: float | None = None
        self.photos_sent = 0
        self.last_result: str | None = None

    def step(self) -> None:
        """기한이 된 일을 하고 다음 기한까지 잔다. main 은 이것을 반복해서 부른다."""
        now = self.clock()
        s = self.settings

        if now >= self.next_heartbeat:
            self.send_heartbeat()
            self.next_heartbeat = now + s.heartbeat_interval

        if self.confirm_due is not None and now >= self.confirm_due:
            self.confirm_due = None
            self._send("confirm")

        if self.occupied and self.refresh_due is not None and now >= self.refresh_due:
            self.refresh_due = now + s.refresh_interval
            self._send("refresh")

        if now >= self.next_frame:
            self.next_frame = now + s.detect_interval
            self.detector.process_frame(self.camera.capture_frame())
            state = self.detector.tracker.state
            if state is not None and state != self.occupied:
                self._occupancy_changed(state, now)

        due = [self.next_frame, self.next_heartbeat]
        if self.confirm_due is not None:
            due.append(self.confirm_due)
        if self.occupied and self.refresh_due is not None:
            due.append(self.refresh_due)
        wait = min(due) - self.clock()
        if wait > 0:
            self.sleep(wait)

    def _occupancy_changed(self, occupied: bool, now: float) -> None:
        was_booting = self.occupied is None
        self.occupied = occupied
        if occupied:
            log.info("차량 진입 확정 -> arrived 사진")
            self.refresh_due = now + self.settings.refresh_interval
            self._send("arrived")
        else:
            self.confirm_due = None     # 떠난 차의 확인 사진은 찍지 않는다
            self.refresh_due = None
            if was_booting:
                log.info("부팅 직후 빈 자리. 사진 없이 Heartbeat 만 보낸다.")
            else:
                log.info("차량 출차 확정 -> left 사진")
                self._send("left")

    def _send(self, event: str) -> None:
        try:
            jpeg = self.camera.capture()
        except Exception:
            log.exception("촬영에 실패했습니다 (event=%s).", event)
            return
        body = self.send_photo(jpeg, event)
        if body is None:
            log.warning("%s 사진 전송 실패. 재시도하지 않는다 (주차 중이면 다음 refresh 때 다시 보낸다).", event)
            return
        self.photos_sent += 1
        result = body.get("result")
        self.last_result = result
        inference = body.get("inference") or {}
        log.info("%s -> result=%s violation_id=%s stickers=%s %sms (%d KB 전송)", event, result,
                 body.get("violation_id"), inference.get("sticker_count"), inference.get("elapsed_ms"),
                 len(jpeg) // 1024)
        if body.get("episode_ended"):
            log.info("서버가 이전 위반 사건을 종료했습니다.")
        if result == "violation_pending" and self.occupied:
            # 서버의 첫 판정이 위반. 2회 확정을 위해 새 사진을 한 장 더 찍어 보낸다 (같은 사진 재전송이 아니다).
            self.confirm_due = self.clock() + self.settings.confirm_delay
        self.camera.set_led(result in VIOLATION_RESULTS)


class AsyncEventRunner(EventModeRunner):
    """Phase 2 비동기 프로토콜 상태 기계 (ADR 0019 결정 8·9, 설계 6.9·6.10). EventModeRunner 와 점유 판정·전송 시점은 같고
    전송 방식만 다르다: 서버는 판정을 바로 돌려주지 않으므로 응답으로 confirm 을 예약하지 않는다.

    사진 전송: 촬영 -> Outbox(디스크)에 먼저 저장 -> 순서대로 전송. Outbox 가 가득 차면 오래된 refresh 만 정리하고, 그래도
    자리가 없으면 새 사진을 저장 실패로 거절한다(_send 가 False, store_failures + 1, 오류 로그. 기존 항목은 그대로).
    같은 event_id·session_id·seq·event·command_id·사진으로만 재시도한다. 202·200 은 성공, 네트워크 오류·5xx·429 는 full jitter 백오프(최대 60초, Retry-After 존중), 409 등은 포기하고
    dead 에 보관한다. 앞 항목이 막히면 뒤 항목은 기다린다(순서). 오래되면 포기한다(refresh·confirm 10분, arrived·left 30분).

    Heartbeat: {session_id, occupied, last_seq, acked_commands} 를 보내고 응답의 commands 를 해석한다.
      - confirm 명령은 command_id 가 새롭고 지금 세션·점유 상태이며 만료 전일 때만 실행한다. 실행 시각은
        max(due_at, 받은 시각): 서버 시계와의 차이는 (due_at - server_time) 로 로컬 시간으로 옮긴다.
      - 실행하면 command_id 를 단 confirm 사진을 Outbox 에 넣고 command_id 를 파일에 기록한다. 이 사진이 접수되면 서버가
        명령을 fulfilled 로 바꾼다. 같은 명령이 또 와도(응답 유실, 재시작 뒤) 다시 찍지 않는다.
      - ack(acked_commands) 는 "이 명령은 실행하지 않는다" 일 때만 보낸다: 다른 세션, 만료, 출차, 점유 아님, 알 수 없는 종류.
        이미 실행한 명령은 절대 ack 하지 않는다 (서버가 cancelled 로 바꿔 fulfilled 를 덮지 못하도록).
        Heartbeat 가 성공하기 전까지 ack 는 계속 보낸다.
      - violation_active 로 LED 를 켜고 끈다 (응답에 판정이 없으므로).

    camera / detector / clock / sleep 은 EventModeRunner 와 같다. wall 은 벽시계(초), rng 는 백오프 지터다(테스트용 주입).
    """

    def __init__(self, camera, detector, settings: EventSettings, outbox, send_job, send_heartbeat,
                 clock=time.monotonic, sleep=time.sleep, wall=time.time, rng=random.random, health=None):
        super().__init__(camera, detector, settings, send_photo=None, send_heartbeat=send_heartbeat,
                         clock=clock, sleep=sleep)
        self.outbox = outbox
        self.send_job = send_job
        self.wall = wall
        self.rng = rng
        self.executed = ExecutedCommands(outbox.root / "executed.json")
        self.session_id: str | None = None
        self.seq = 0
        self.pending_confirms: dict[str, tuple[float, float, str]] = {}   # command_id -> (실행 시각, 만료 시각, 세션)
        self.acks: list[str] = []                                          # 보낼 ack (성공할 때까지 유지, 순서 유지)
        self._retry: dict[str, tuple[int, float]] = {}                    # event_id -> (실패 횟수, 다음 시도 시각)
        self.store_failures = 0                                            # 이 프로세스에서 저장하지 못한 사진 수 (유실 건수)
        self.store_failures_full = 0                                       # 그중 논리 한도(건수·바이트) 때문
        self.store_failures_disk = 0                                       # 그중 실제 디스크 오류 때문
        self.last_store_error: str | None = None
        # session_known: 장비 로컬 기준이다. "지금 세션의 이벤트가 하나라도 로컬 Outbox 에 저장된 적이 있다" 일 뿐, 서버가 받았다는
        # 확인(ack)이 아니다. 저장한 뒤 전송이 한 번도 되지 않았거나 나이로 포기(given_up)되어도 True 로 남는다(세션이 바뀔 때까지).
        # 그래서 "서버가 이 세션을 알고 있을 수 있다" 쪽으로 기운 보수적 판단이다. 서버의 확인이 필요한 판단에는 쓰지 않는다.
        self.session_known = False
        self.health = health or OutboxHealth(outbox.root / "health.json", clock=wall)   # 재시작해도 남는 기록

    # --- 한 단계 ------------------------------------------------------------------------------------------

    def step(self) -> None:
        now = self.clock()
        s = self.settings

        if now >= self.next_heartbeat:
            self._heartbeat()
            self.next_heartbeat = now + s.heartbeat_interval

        self._run_due_confirms()

        if self.occupied and self.refresh_due is not None and now >= self.refresh_due:
            self.refresh_due = now + s.refresh_interval
            self._send("refresh")

        self._flush()

        if now >= self.next_frame:
            self.next_frame = now + s.detect_interval
            self.detector.process_frame(self.camera.capture_frame())
            state = self.detector.tracker.state
            if state is not None and state != self.occupied:
                self._occupancy_changed(state, now)

        due = [self.next_frame, self.next_heartbeat] + [d for d, _, _ in self.pending_confirms.values()]
        if self.occupied and self.refresh_due is not None:
            due.append(self.refresh_due)
        retry_at = self._next_retry()
        if retry_at is not None:
            due.append(retry_at)
        wait = min(due) - self.clock()
        if wait > 0:
            self.sleep(wait)

    def _occupancy_changed(self, occupied: bool, now: float) -> None:
        was_booting = self.occupied is None
        self.occupied = occupied
        if occupied:
            log.info("차량 진입 확정 -> arrived 사진")
            self.refresh_due = now + self.settings.refresh_interval
            self._send("arrived")
        else:
            self.refresh_due = None
            self._cancel_pending_confirms("차량이 나갔습니다")
            if was_booting:
                log.info("부팅 직후 빈 자리. 사진 없이 Heartbeat 만 보낸다.")
            else:
                log.info("차량 출차 확정 -> left 사진")
                self._send("left")

    # --- 촬영 -> Outbox ------------------------------------------------------------------------------------

    def _send(self, event: str, command_id: str | None = None) -> bool:
        """촬영해서 Outbox 에 넣고 전송을 시도한다. Outbox 에 들어갔으면 True (전송 성공과는 별개)."""
        try:
            jpeg = self.camera.capture()
        except Exception:
            log.exception("촬영에 실패했습니다 (event=%s).", event)
            return False
        if event == "arrived":
            self._cancel_pending_confirms("새 세션이 시작되었습니다")
            self.session_id, self.seq = str(uuid.uuid4()), 0
            self.session_known = False
        elif self.session_id is None:
            log.warning("진행 중인 세션이 없어 %s 사진을 보내지 않습니다.", event)
            return False
        self.seq += 1
        item = OutboxItem.new(self.session_id, self.seq, event, jpeg, self.wall, command_id)
        try:
            # `left` 만 열린 세션의 예약 칸을 쓴다. 나머지(arrived·confirm·refresh)는 저장한 뒤에도 그 칸이 남아야 받는다.
            self.outbox.put(item, reserve_for_left=(event != "left"))
        except (OutboxFull, OSError) as exc:
            # 저장하지 못한 사진은 저장된 것도, 보낸 것도 아니다: 순번을 쓰지 않고, 명령을 실행한 것으로 기록하지 않고,
            # photos_sent 도 올리지 않는다. 같은 일이 계속되면 사진이 유실되므로 눈에 띄게 오류로 남기고
            # (논리 한도인지 실제 디스크 문제인지 구분해) health.json 에 영속적으로 센다.
            self.seq -= 1
            kind = classify_store_error(exc)
            self.store_failures += 1
            if kind.startswith("full_"):
                self.store_failures_full += 1
            else:
                self.store_failures_disk += 1
            self.last_store_error = f"{event}: {kind}: {exc}"
            left_of_known_session = event == "left" and self.session_known
            if left_of_known_session:
                log.error("left 사진을 Outbox 에 저장하지 못했습니다(%s). 서버가 이 세션을 알고 있어 출차가 전해지지 않으면 "
                          "서버의 사건이 열린 채 남을 수 있습니다: %s", kind, exc)
            elif event == "left":
                log.warning("left 사진을 Outbox 에 저장하지 못했습니다(%s). 이 세션의 이벤트는 서버에 간 적이 없어 닫을 사건은 "
                            "없습니다: %s", kind, exc)
            else:
                log.error("%s 사진을 Outbox 에 저장하지 못했습니다(%s). 이 사진은 서버로 보내지지 않습니다 (유실): %s",
                          event, kind, exc)
            self.health.record_failure(event, self.seq + 1, kind, str(exc), left_of_known_session)
            return False
        self.session_known = True
        if command_id:
            self.executed.add(command_id)   # 사진이 디스크에 있다 = 실행했다. 재시작해도 다시 찍지 않는다
        self._flush()
        return True

    # --- 전송 ---------------------------------------------------------------------------------------------

    def _flush(self) -> None:
        wall = self.wall()
        items = self.outbox.items()
        for item in items:   # 막힌 항목 뒤에 있어도 오래되면 포기한다
            limit = GIVE_UP_AFTER.get(item.event, 600.0)
            if wall - item.created > limit:
                log.warning("%s 사진(seq %s)을 %d분 안에 보내지 못해 포기합니다.", item.event, item.seq, limit // 60)
                self.outbox.discard(item, f"gave up after {int(limit // 60)} min")
                self.health.record_given_up(item.event)
                self._retry.pop(item.event_id, None)
        now = self.clock()
        for item in self.outbox.items():
            state = self._retry.get(item.event_id)
            if state is not None and state[1] > now:
                break   # 앞 항목이 아직 기다리는 중: 뒤 항목이 앞지르지 않는다
            try:
                outcome = self.send_job(item)
            except OSError as exc:
                log.warning("%s 사진 전송 실패, 다시 시도합니다: %s", item.event, exc)
                self._backoff(item, None)
                break
            if outcome.kind in ("accepted", "duplicate"):
                self.outbox.remove(item)
                self._retry.pop(item.event_id, None)
                self.photos_sent += 1
                log.info("%s 접수됨 (seq %s, HTTP %s%s)", item.event, item.seq, outcome.status,
                         ", 이미 접수된 요청" if outcome.kind == "duplicate" else "")
            elif outcome.kind == "drop":
                reason = f"{outcome.status} {outcome.detail}".strip()
                log.error("서버가 %s 사진(seq %s)을 거부했습니다 (%s). 다시 보내지 않고 보관합니다.", item.event, item.seq, reason)
                self.outbox.discard(item, reason)
                self._retry.pop(item.event_id, None)
            else:
                log.warning("서버가 %s 사진을 받지 못했습니다 (HTTP %s). 다시 시도합니다.", item.event, outcome.status)
                self._backoff(item, outcome.retry_after)
                break

    def _backoff(self, item, retry_after: float | None) -> None:
        failures = self._retry.get(item.event_id, (0, 0.0))[0] + 1
        wait = self.rng() * min(MAX_BACKOFF, 2.0 * 2 ** failures)   # full jitter
        wait = max(wait, retry_after or 0.0, MIN_RETRY_DELAY)
        self._retry[item.event_id] = (failures, self.clock() + wait)

    def _next_retry(self) -> float | None:
        items = self.outbox.items()
        if not items:
            return None
        state = self._retry.get(items[0].event_id)
        return state[1] if state is not None else self.clock()

    # --- Heartbeat 와 명령 ----------------------------------------------------------------------------------

    def _heartbeat(self) -> None:
        body = {"session_id": self.session_id, "occupied": self.occupied, "last_seq": self.seq,
                "acked_commands": list(self.acks)}
        try:
            response = self.send_heartbeat(body)
        except OSError as exc:
            log.warning("Heartbeat 전송 실패: %s", exc)
            response = None
        if response is None:
            return   # ack 는 그대로 남아 다음 Heartbeat 에 다시 간다
        self.acks = [c for c in self.acks if c not in body["acked_commands"]]
        if "violation_active" in response:
            self.camera.set_led(bool(response["violation_active"]))
        self._apply_commands(response)

    @staticmethod
    def _parse_time(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _apply_commands(self, response: dict) -> None:
        commands = response.get("commands") or []
        server_time = self._parse_time(response.get("server_time"))
        now = self.clock()
        for command in commands:
            command_id = str(command.get("command_id", ""))
            if not command_id or command_id in self.executed or command_id in self.pending_confirms:
                continue   # 이미 실행했거나 예약했다: 다시 찍지 않고, 실행한 명령은 ack 하지 않는다
            if command.get("type") != "capture_confirm":
                log.warning("알 수 없는 명령 종류 %r: 실행하지 않습니다.", command.get("type"))
                self._ack(command_id)
                continue
            if str(command.get("session_id")) != self.session_id or not self.occupied:
                log.info("명령 %s 는 지금 세션·점유 상태와 맞지 않아 실행하지 않습니다.", command_id)
                self._ack(command_id)
                continue
            due_at, expires_at = self._parse_time(command.get("due_at")), self._parse_time(command.get("expires_at"))
            if server_time is None or due_at is None or expires_at is None:
                log.warning("명령 %s 의 시각을 해석할 수 없어 바로 실행합니다.", command_id)
                self.pending_confirms[command_id] = (now, now + 540.0, self.session_id)
                continue
            # 서버 시계를 로컬 시간으로 옮긴다: 로컬 시각 = 로컬 now + (명령 시각 - 서버 now). 장비 시계가 틀려도 간격은 맞다
            due = now + max(0.0, (due_at - server_time).total_seconds())
            expires = now + (expires_at - server_time).total_seconds()
            if expires <= now:
                log.info("명령 %s 는 이미 만료되어 실행하지 않습니다.", command_id)
                self._ack(command_id)
                continue
            self.pending_confirms[command_id] = (due, expires, self.session_id)

    def _ack(self, command_id: str) -> None:
        if command_id not in self.executed and command_id not in self.acks:
            self.acks.append(command_id)

    def _cancel_pending_confirms(self, why: str) -> None:
        for command_id in list(self.pending_confirms):
            log.info("예약된 confirm 명령 %s 를 취소합니다: %s", command_id, why)
            del self.pending_confirms[command_id]
            self._ack(command_id)

    def _run_due_confirms(self) -> None:
        now = self.clock()
        for command_id, (due, expires, session) in sorted(self.pending_confirms.items(), key=lambda kv: kv[1][0]):
            if now < due:
                continue
            del self.pending_confirms[command_id]
            if not self.occupied or session != self.session_id or now >= expires:
                log.info("명령 %s 를 실행하지 않습니다 (세션 변경, 출차 또는 만료).", command_id)
                self._ack(command_id)
                continue
            log.info("서버 명령에 따라 confirm 사진 (command %s)", command_id)
            self._send("confirm", command_id=command_id)


class DeviceCamera:
    """EventModeRunner 가 쓰는 카메라 어댑터: Hardware/FakeHardware 와 전송 설정을 묶는다."""

    def __init__(self, hardware, send_width: int, jpeg_quality: int):
        self.hardware = hardware
        self.send_width = send_width
        self.jpeg_quality = jpeg_quality

    def capture_frame(self):
        return self.hardware.capture_frame()

    def capture(self) -> bytes:
        return load_jpeg(self.hardware.capture(), self.send_width, self.jpeg_quality)

    def set_led(self, on: bool) -> None:
        self.hardware.set_led(on)


def run_event_mode(hardware, args, limit: int) -> None:
    """이벤트 모드 main loop. limit 은 보낼 사진 수 상한(0 이면 무한). PC 시험용."""
    from vehicle_detector import OccupancyConfig, VehicleDetector

    settings = EventSettings.from_env()
    if args.detect_interval is not None:
        settings.detect_interval = args.detect_interval
    detector = VehicleDetector(model_path=os.environ.get("PG_DETECTOR_MODEL", "yolo26s_ncnn_model"),
                               imgsz=int(os.environ.get("PG_DETECTOR_IMGSZ", "480")),
                               config=OccupancyConfig.from_env())
    camera = DeviceCamera(hardware, args.send_width, args.jpeg_quality)
    protocol = parse_protocol()
    if protocol == "async":
        outbox_dir = os.environ.get("PG_OUTBOX_DIR") or os.path.join(os.path.expanduser("~"), ".parking_guardian",
                                                                      "outbox")
        runner = AsyncEventRunner(
            camera, detector, settings, outbox=Outbox(outbox_dir, clock=time.time),
            send_job=lambda item: post_job(args.server, args.key, item),
            send_heartbeat=lambda body: post_heartbeat(args.server, args.key, body))
        log.info("비동기 프로토콜(Phase 2) | Outbox %s | 대기 중 %d건", outbox_dir, len(runner.outbox.items()))
    else:
        runner = EventModeRunner(
            camera, detector, settings,
            send_photo=lambda jpeg, event: send_photo(args.server, args.key, jpeg, args.zone, event=event),
            send_heartbeat=lambda: send_heartbeat(args.server, args.key))
    log.info("이벤트 모드 | 프레임 %.1f초 | Heartbeat %.0f초 | refresh %.0f초 | confirm %.0f초 | ROI %s",
             settings.detect_interval, settings.heartbeat_interval, settings.refresh_interval,
             settings.confirm_delay, detector.config.roi)
    steps = 0
    while True:
        try:
            runner.step()
        except Exception:
            log.exception("이번 단계에서 오류가 발생했습니다. 계속합니다.")
            time.sleep(settings.detect_interval)
        steps += 1
        if limit and steps >= limit:
            break


def run_cycle(hardware, args) -> None:
    """한 주기: 촬영 -> 전송 -> LED."""
    path = hardware.capture()
    jpeg = load_jpeg(path, args.send_width, args.jpeg_quality)
    body = send_photo(args.server, args.key, jpeg, args.zone)
    if body is None:
        return  # 서버와 통신하지 못했으면 LED 는 이전 상태로 둔다

    result = body.get("result")
    inference = body.get("inference") or {}
    log.info("result=%s violation_id=%s stickers=%s %sms (%d KB 전송)",
             result, body.get("violation_id"), inference.get("sticker_count"), inference.get("elapsed_ms"),
             len(jpeg) // 1024)
    if body.get("episode_ended"):
        log.info("서버가 이전 위반 사건을 종료했습니다 (차량이 나갔거나 스티커가 확인됨).")
    hardware.set_led(result in VIOLATION_RESULTS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parking Guardian 장비 클라이언트")
    parser.add_argument("--server", default=os.environ.get("PG_SERVER_URL", "http://127.0.0.1:5000/api/v1"),
                        help="서버 API 주소 (PG_SERVER_URL)")
    parser.add_argument("--key", default=os.environ.get("PG_DEVICE_KEY", ""), help="장비 인증 키 (PG_DEVICE_KEY)")
    parser.add_argument("--zone", default=os.environ.get("PG_ZONE_NAME") or None, help="구역 이름 (PG_ZONE_NAME)")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("PG_INTERVAL", "60")),
                        help="촬영 주기(초)")
    parser.add_argument("--send-width", type=int, default=int(os.environ.get("PG_SEND_WIDTH", "1280")),
                        help="전송할 사진의 가로 픽셀")
    parser.add_argument("--jpeg-quality", type=int, default=int(os.environ.get("PG_JPEG_QUALITY", "92")),
                        help="크기를 줄일 때 쓰는 JPEG 품질 (1-100)")
    parser.add_argument("--led-pin", type=int, default=int(os.environ.get("PG_LED_PIN", "4")), help="LED BCM 핀")
    parser.add_argument("--mode", choices=("event", "interval"), default=os.environ.get("PG_MODE", "event"),
                        help="event: 점유 변화 때만 전송 (기본, 운영) | interval: 1분마다 전송 (비교·측정용) (PG_MODE)")
    parser.add_argument("--detect-interval", type=float, default=None,
                        help="event 모드의 프레임 간격(초). 기본은 PG_DETECT_INTERVAL 또는 5")
    parser.add_argument("--image", help="카메라 대신 이 사진 파일을 보낸다 (PC 에서 시험할 때)")
    parser.add_argument("--once", action="store_true", help="한 번만 실행하고 끝낸다")
    parser.add_argument("--count", type=int, default=0,
                        help="이 횟수만큼 실행하고 끝낸다 (0 이면 계속). event 모드에서는 step 횟수")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if not args.key:
        log.error("장비 인증 키가 없습니다. PG_DEVICE_KEY 환경변수나 --key 로 지정하세요.")
        return 2

    def stop(signum, frame):  # systemd 가 서비스를 멈출 때(SIGTERM)도 카메라와 GPIO 를 정리한다
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)

    limit = 1 if args.once else args.count
    log.info("서버: %s | 모드: %s | 주기: %.0f초 | 장비 시리얼: %s", args.server, args.mode, args.interval,
             get_pi_serial())
    hardware = FakeHardware(args.image) if args.image else Hardware(args.led_pin, lores=args.mode == "event")
    done = 0
    try:
        if args.mode == "event":
            run_event_mode(hardware, args, limit)
            return 0
        while True:
            started = time.time()
            try:
                run_cycle(hardware, args)
            except Exception:
                # 한 번의 오류로 멈추지 않는다. 카메라도 끄지 않는다.
                log.exception("이번 주기에서 오류가 발생했습니다. 다음 주기에 다시 시도합니다.")

            done += 1
            if limit and done >= limit:
                break
            remaining = args.interval - (time.time() - started)
            if remaining > 0:
                time.sleep(remaining)
            else:
                log.warning("처리가 주기보다 %.1f초 더 걸렸습니다.", -remaining)
    except KeyboardInterrupt:
        log.info("종료합니다.")
    finally:
        hardware.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
