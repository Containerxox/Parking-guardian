# Parking Guardian 장비 (Raspberry Pi)

Raspberry Pi와 카메라 모듈이 1분마다 주차 구역을 찍어 서버로 보낸다. 판단은 전부 서버가 한다.

```text
Pi: 촬영 → 크기 줄이기 → 서버 전송 → 응답에 따라 LED
서버: 장비 인증 → AI 판정 → 같은 차량은 한 사건으로 묶기 → 저장과 알림
```

## 준비

1. 서비스 운영자가 주차장 상세 화면에서 장비를 등록한다. 시리얼 번호는 아래 명령으로 확인한다.

   ```bash
   grep Serial /proc/cpuinfo
   ```

2. 등록하면 인증 키(`pgk_...`)가 한 번만 표시된다. 이 키를 Pi에 설정한다.

3. Pi에 필요한 패키지

   ```bash
   sudo apt install -y python3-picamera2 python3-opencv python3-requests
   ```

   Pi에서 AI 모델을 돌리지 않으므로 torch는 필요 없다.

## 실행

```bash
export PG_SERVER_URL="https://서버주소/api/v1"
export PG_DEVICE_KEY="pgk_..."
python3 pi_client.py
```

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `PG_SERVER_URL` | 서버 API 주소 | `http://127.0.0.1:5000/api/v1` |
| `PG_DEVICE_KEY` | 장비 인증 키. 필수 | 없음 |
| `PG_ZONE_NAME` | 이 카메라가 보는 구역 이름. 장비가 구역 하나에 연결돼 있으면 생략 | 없음 |
| `PG_INTERVAL` | 촬영 주기(초) | 60 |
| `PG_SEND_WIDTH` | 전송할 사진의 최대 가로 픽셀. 이보다 큰 사진만 줄여서 보낸다 | 1280 |
| `PG_JPEG_QUALITY` | 줄일 때 쓰는 JPEG 품질 | 92 |
| `PG_LED_PIN` | LED가 연결된 BCM 핀 | 4 |

## 사진 크기와 압축

스티커는 사진에서 아주 작은 부분이라 크기와 압축에 민감하다. 샘플 사진으로 측정한 결과는 다음과 같다.

| 사진 | 처리 | 스티커 신뢰도 | 판정 |
|---|---|---|---|
| `sample_a` (가로 1080, 야간) | 원본 그대로 | 0.64 | 정상 |
| `sample_a` | 크기 유지, 품질 95로 다시 압축 | 0.57 | 정상 |
| `sample_a` | 크기 유지, 품질 85로 다시 압축 | 0.29 | **위반으로 오판** |
| `sample_c` (가로 3024, 주간) | 원본 그대로 | 0.92 | 정상 |
| `sample_c` | 가로 1280, 품질 85 | 0.92 | 정상 |

그래서 이 스크립트는 줄일 필요가 없는 사진은 원본 바이트를 그대로 보내고, 줄일 때만 다시 압축한다. 실제 카메라 사진에서 스티커가 잘 잡히지 않으면 `PG_SEND_WIDTH` 를 키운다. 전송량은 늘어난다.

## 서버 응답과 LED

| `result` | 의미 | LED |
|---|---|---|
| `no_vehicle` | 차량 없음 | 끔 |
| `permitted` | 스티커가 있는 차량 | 끔 |
| `violation_pending` | 위반으로 보이지만 확인 중 (다음 사진에서 확정) | 끔 |
| `violation` | 위반 확정. 서버가 사건을 만들고 담당자에게 알림 | 켬 |
| `violation_ongoing` | 이미 기록된 차량이 계속 주차 중 | 켬 |

서버와 통신하지 못하면 LED는 이전 상태를 유지하고 다음 주기에 다시 시도한다.

## PC에서 시험하기

카메라와 GPIO 없이 전송 부분만 확인할 수 있다. Backend를 띄운 뒤 실행한다.

```powershell
cd C:\parking_guardian\capston
backend\venv\Scripts\python device\pi_client.py --image backend\uploads\sample_b.jpg --count 2 --interval 1 --key pgk_dev_rpi-a-gangnam-01
```

`sample_b`는 스티커가 없는 차량이라 첫 번째는 `violation_pending`, 두 번째는 `violation`이 나온다.

## 부팅 시 자동 실행 (systemd 예시)

`/etc/systemd/system/parking-guardian.service`

```ini
[Unit]
Description=Parking Guardian device client
After=network-online.target
Wants=network-online.target

[Service]
User=pi
Environment=PG_SERVER_URL=https://서버주소/api/v1
Environment=PG_DEVICE_KEY=pgk_...
ExecStart=/usr/bin/python3 /home/pi/pi_client.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now parking-guardian
journalctl -u parking-guardian -f
```

인증 키가 들어 있으므로 이 파일은 `sudo chmod 600` 으로 권한을 제한한다.

## 기존 코드(`image_send.py`)와 달라진 점

| 항목 | 기존 | 지금 |
|---|---|---|
| Pi에서 AI 실행 | YOLOv5m으로 차량 탐지 | 없음. 서버가 판단 |
| 보내는 사진 | 차량마다 잘라서 여러 장 | 사진 전체 한 장 (가로 1280으로 축소) |
| 차량이 없을 때 | 보내지 않음 | 보냄. 서버가 차량 이탈과 장비 연결 상태를 알 수 있음 |
| 주소 | `/api/upload` | `/api/v1/device/detections` |
| 인증 | 없음 | `X-Device-Key` 헤더 |
| 장비 식별 | 본문의 `serial`, `user_id` | 인증 키로 서버가 판단 |
| LED 기준 | 응답의 `stickers` 가 빈 배열 | 응답의 `result` 가 `violation` 또는 `violation_ongoing` |
| 오류 처리 | 카메라를 끄고 반복은 계속 | 카메라는 그대로 두고 다음 주기에 재시도 |
| 찍은 사진 | `/home/pi/pictures/` 에 계속 쌓임 | 임시 파일 하나를 덮어씀 |

## 확인되지 않은 것

- 이 스크립트는 실제 Raspberry Pi에서 실행해 보지 못했다. 카메라와 GPIO 부분은 기존 코드의 설정을 그대로 옮긴 것이다. PC에서는 `--image` 모드로 전송, 응답 처리, 반복 동작만 확인했다.
- Pi 카메라로 찍은 사진에서 서버의 앞유리 모델이 잘 동작하는지는 실제 사진으로 확인이 필요하다.
