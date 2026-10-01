"""Parking Guardian 장비 클라이언트 (Raspberry Pi + 카메라 모듈).

1분마다 사진을 찍어 서버로 보낸다. 판단은 전부 서버가 한다.

    촬영 -> 크기 줄이기 -> 서버 전송 -> 응답에 따라 LED 켜기/끄기 -> 다음 주기까지 대기

기존 image_send.py 와 달라진 점
- Pi 에서 AI 모델(torch, YOLOv5)을 돌리지 않는다. 차량이 있는지도 서버가 판단한다.
- 차량 부분만 잘라 보내지 않고 사진 전체를 보낸다. 서버는 가장 큰 앞유리 하나만 보므로
  옆 칸 차량이 위반으로 잡히지 않는다.
- 차량이 없어도 매번 보낸다. 서버가 "차량이 나갔다"와 "장비가 살아 있다"를 알 수 있다.
- 장비 인증 키(X-Device-Key)로 인증한다. 회사, 주차장, 시리얼 번호는 보내지 않는다.
- 오류가 나도 카메라를 끄지 않고 다음 주기에 다시 시도한다.
- 찍은 사진을 Pi 에 쌓아 두지 않는다.

설정은 환경변수로 한다 (device/README.md 참고).

    PG_SERVER_URL   서버 API 주소. 예: https://api.example.com/api/v1
    PG_DEVICE_KEY   장비 등록 때 한 번 표시되는 인증 키 (pgk_...)
    PG_ZONE_NAME    (선택) 이 카메라가 보는 구역 이름. 장비가 구역 하나에 연결돼 있으면 생략
    PG_INTERVAL     (선택) 촬영 주기(초). 기본 60
    PG_SEND_WIDTH   (선택) 전송할 사진의 최대 가로 픽셀. 이보다 크면 줄여서 보낸다. 기본 1280
    PG_JPEG_QUALITY (선택) 줄일 때 쓰는 JPEG 품질. 기본 92
    PG_LED_PIN      (선택) LED 가 연결된 BCM 핀 번호. 기본 4

PC 에서 카메라 없이 전송만 시험하기:

    python device/pi_client.py --image backend/uploads/sample_b.jpg --once
"""
from __future__ import annotations  # Raspberry Pi OS 의 Python 3.9 에서도 동작하도록

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime

import requests

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

    def __init__(self, led_pin: int):
        from picamera2 import Picamera2
        import RPi.GPIO as GPIO

        Picamera2.set_logging(Picamera2.ERROR)
        os.environ["LIBCAMERA_LOG_LEVELS"] = "3"

        self.gpio = GPIO
        self.led_pin = led_pin
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(led_pin, GPIO.OUT)

        # 카메라 설정은 기존 코드에서 쓰던 값 그대로다.
        self.camera = Picamera2()
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


def send_photo(server_url: str, device_key: str, jpeg: bytes, zone_name: str | None) -> dict | None:
    """사진을 서버로 보내고 응답 JSON 을 돌려준다. 실패하면 이유를 로그에 남기고 None 을 돌려준다."""
    now = datetime.now().astimezone()  # 시간대 정보를 포함해 보낸다. 예: 2026-10-01T21:03:00+09:00
    form = {"detected_at": now.isoformat(timespec="seconds")}
    if zone_name:
        form["zone_name"] = zone_name
    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/device/detections",
            headers={"X-Device-Key": device_key},
            files={"file": (now.strftime("%Y%m%d_%H%M%S.jpg"), jpeg, "image/jpeg")},
            data=form,
            timeout=(5, 60),  # 연결 5초, 응답 60초. 서버의 첫 추론은 모델을 불러오느라 오래 걸린다
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
    parser.add_argument("--image", help="카메라 대신 이 사진 파일을 보낸다 (PC 에서 시험할 때)")
    parser.add_argument("--once", action="store_true", help="한 번만 실행하고 끝낸다")
    parser.add_argument("--count", type=int, default=0, help="이 횟수만큼 실행하고 끝낸다 (0 이면 계속)")
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
    log.info("서버: %s | 주기: %.0f초 | 장비 시리얼: %s", args.server, args.interval, get_pi_serial())
    hardware = FakeHardware(args.image) if args.image else Hardware(args.led_pin)
    done = 0
    try:
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
