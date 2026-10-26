# Parking Guardian 장비 (Raspberry Pi)

Raspberry Pi와 카메라 모듈이 장애인 주차면 한 칸을 지켜보다가 **차가 들어오거나 나갈 때** 사진을 찍어 서버로 보내고, 매분 Heartbeat를 보낸다(기본 `PG_MODE=event`). 위반인지는 전부 서버가 판단한다.

```text
Pi: 5초마다 프레임 → 차량 탐지 → 주차면 점유 판정 → 바뀌면 사진 전송(arrived/confirm/refresh/left) → 응답에 따라 LED. 매분 Heartbeat
서버: 장비 인증 → AI 판정 → 같은 차량은 한 사건으로 묶기 → 저장과 알림
```

1분마다 무조건 사진을 보내는 예전 방식은 `PG_MODE=interval` 로 남아 있다. 운영용이 아니고, 이벤트 모드와의 성능 비교(Before/After 측정)를 위한 보조 모드다.

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

   스티커·앞유리·번호판 판정은 서버가 하지만, 이벤트 모드의 주차면 점유 판정(차량 탐지)은 Pi 에서 돈다. 그래서 `ultralytics`(torch 포함)와 차량 탐지 모델(`yolo26s_ncnn_model/` 폴더 또는 `yolo26s.pt`)이 필요하다("이벤트 모드"와 "모델 선택" 참고). `PG_MODE=interval` 로만 쓸 때는 둘 다 필요 없다.

## 실행

```bash
export PG_SERVER_URL="https://서버주소/api/v1"
export PG_DEVICE_KEY="pgk_..."
python3 pi_client.py
```

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `PG_SERVER_URL` | 서버 API 주소 | `http://127.0.0.1:5000/api/v1` |
| `PG_DEVICE_KEY` | 장비 인증 키. 필수. 실제 장비는 운영자 화면에서 등록할 때 한 번 표시되는 키. 시드로 만든 데모 장비는 서버에서 `flask device-key RPI-A-GANGNAM-01` 로 얻는다(서버에 `SEED_DEVICE_KEY_SECRET` 이 없으면 `pgk_dev_rpi-a-gangnam-01` 같은 고정값, 있으면 그 비밀로 유도한 값. 공개 서버에서는 후자) | 없음 |
| `PG_ZONE_NAME` | 이 카메라가 보는 구역 이름. 장비가 구역 하나에 연결돼 있으면 생략 | 없음 |
| `PG_INTERVAL` | `interval` 모드의 촬영 주기(초). `event` 모드는 쓰지 않는다 | 60 |
| `PG_SEND_WIDTH` | 전송할 사진의 최대 가로 픽셀. 이보다 큰 사진만 줄여서 보낸다 | 1280 |
| `PG_JPEG_QUALITY` | 줄일 때 쓰는 JPEG 품질 | 92 |
| `PG_LED_PIN` | LED가 연결된 BCM 핀 | 4 |
| `PG_MODE` | `event`(점유 변화 때만 전송, 아래 "이벤트 모드". **운영 기본**) 또는 `interval`(1분마다 전송. 비교·성능 측정용, 아래 "interval 모드") | `event` |

## 이벤트 모드 (`PG_MODE=event`, 기본)

Pi 가 주차면 한 칸의 점유를 판정해서 **상황이 바뀔 때만** 사진을 보낸다. 위반인지는 여전히 서버가 정한다(설계: 루트 `AI_FINAL_DESIGN.md` 10절 MVP, `docs/adr/0014`). 구현은 `pi_client.py` 의 `EventModeRunner` 이고 점유 판정은 아래 "주차면 점유 판정"의 `vehicle_detector.py` 다.

| 상황 | Pi 가 하는 일 |
|---|---|
| 5초마다 | 저해상도(640x480) 프레임으로 차량 탐지 + ROI 점유 판정 |
| 비점유 → 점유 (3프레임 연속) | `arrived` 사진 전송. 응답이 `violation_pending` 이면 60초 뒤 **새 사진**을 `confirm` 으로 전송 |
| 점유 중 | 60초마다 Heartbeat, 10분마다 `refresh` 사진 |
| 점유 → 비점유 (6프레임 연속) | 빈 자리 사진을 `left` 로 전송. 대기 중인 confirm 은 취소 |
| 비점유 중 | 60초마다 Heartbeat 만. 사진 없음 |
| 전송 실패 | 로그만 남긴다. 재시도 큐 없음(MVP). 주차 중이면 다음 refresh 때 사진이 다시 간다 |

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `PG_DETECT_INTERVAL` | 점유 판정 프레임 간격(초) | 5 |
| `PG_HEARTBEAT_INTERVAL` | Heartbeat 간격(초) | 60 |
| `PG_REFRESH_INTERVAL` | 주차 중 확인 사진 간격(초) | 600 |
| `PG_CONFIRM_DELAY` | `violation_pending` 응답 뒤 확인 사진까지(초) | 60 |
| `PG_DETECTOR_MODEL` | 차량 탐지 모델. NCNN 폴더 또는 `.pt` | `yolo26s_ncnn_model` |
| `PG_DETECTOR_IMGSZ` | 탐지 입력 크기 | 480 |
| `PG_ROI`, `PG_OCC_*` | 주차면 ROI 와 판정 기준값 | 아래 표 |

필요한 패키지: `ultralytics`(torch 포함), `opencv-python`. Pi 4 에서는 `yolo26s_ncnn_model/` 폴더를 작업 폴더에 둔다("모델 선택" 참고).

**Pi 4 에서 아직 확인하지 못한 것**: lores 스트림의 YUV420 → BGR 변환(`Hardware.capture_frame`), 탐지 속도와 메모리, 빈 자리와 옆 칸 차량 판정, 야간. PC 에서 확인한 것(상태 기계, 서버 연동, 샘플 3장의 ROI 판정, NCNN 내보내기)과 Pi 에서만 확인할 수 있는 것의 구분은 `docs/measurements/PC_VERIFICATION.md`.

## interval 모드 (`PG_MODE=interval`, 비교·측정용)

지금까지의 방식이다. 차가 있든 없든 `PG_INTERVAL`(60초)마다 사진을 찍어 보내고 Heartbeat 는 보내지 않는다(사진이 "살아 있다"를 겸한다). 점유 판정과 차량 탐지 모델이 필요 없다. 이벤트 모드가 서버 부하를 얼마나 줄이는지 같은 코드로 재는 것이 목적이고(`backend/scripts/measure.py --mode interval`, 결과 `docs/measurements/README.md`), 실제 장비 운영에는 쓰지 않는다. 삭제하지 않고 유지한다.

```bash
PG_MODE=interval python3 pi_client.py
```

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

카메라와 GPIO 없이 사진 한 장을 카메라 대신 쓸 수 있다. Backend를 띄운 뒤 실행한다.

이벤트 모드(기본)에서는 사진이 계속 "점유"로 판정되므로 `arrived` → (위반이면) `confirm` → `refresh` 순서가 나온다. 차량 탐지 모델(`PG_DETECTOR_MODEL`)이 필요하다.

```powershell
cd C:\parking_guardian\capston
backend\venv\Scripts\python device\pi_client.py --image backend\uploads\sample_b.jpg --count 30 --detect-interval 0.5 --key pgk_dev_rpi-a-gangnam-01
```

interval 모드는 모델 없이 전송 부분만 확인할 때 쓴다. `sample_b`는 스티커가 없는 차량이라 첫 번째는 `violation_pending`, 두 번째는 `violation`이 나온다.

```powershell
backend\venv\Scripts\python device\pi_client.py --mode interval --image backend\uploads\sample_b.jpg --count 2 --interval 1 --key pgk_dev_rpi-a-gangnam-01
```

테스트: `python -m pytest device -q` (점유 판정 10개 + 이벤트 모드 상태 기계 7개). 모델과 카메라 없이 돈다. 상태 기계를 실제 API에 붙인 통합 테스트는 `backend/tests/test_event_mode_integration.py`(점유는 가상, 판정은 Mock).

**Pi 없이 전체 경로 돌려 보기** (`pc_event_test.py`): 카메라 대신 사진 타임라인을 프레임으로 넣고 YOLO26s → ROI 판정 → 상태 기계 → 서버까지 실제 코드로 돈다. `empty` 는 실제 사진을 흐린 합성 프레임이라 "탐지 없음"만 흉내 낸다. 결과와 한계는 `docs/measurements/PC_VERIFICATION.md`.

```powershell
backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5001/api/v1 --timeline "empty:60,backend/uploads/sample_b.jpg:200,empty:90" --speed 10
```

실제 빈 자리 사진이 있으면 `empty` 대신 넣는다(`backend/uploads/sample_f.jpg`). 브라우저로 관리자 화면을 보면서 진행하려면 `--interactive`: 위반이 확정되면(`result=violation`) 멈추고 Enter 를 기다린다. 기다리는 동안 Heartbeat 는 실제 시간 60초마다 계속 보내고(서버의 10분 끊김 판정 방지), Enter 를 누르면 타임라인의 다음 항목(빈 자리)으로 건너뛰어 `left` 를 보낸다. 사진 요청을 기다리는 동안은 시뮬레이션 시계를 세운다(컨테이너 서버의 첫 추론은 모델 로딩으로 25초쯤 걸린다). 컨테이너 서버(5000)에 보내면 개발 DB 에 위반이 생긴다. 결과와 사용 가이드는 `docs/measurements/DOCKER_SIMULATION.md`.

```powershell
backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5000/api/v1 --key pgk_dev_rpi-a-gangnam-01 --interactive --timeline "backend/uploads/sample_f.jpg:60,backend/uploads/sample_d.jpg:700,backend/uploads/sample_f.jpg:90" --speed 10
```

주의: 현재 스티커 모델은 `sample_d`(스티커 있음)를 위반으로, `sample_e`(스티커 없음)를 정상으로 판정한다(라벨과 반대. `DOCKER_SIMULATION.md` 2절). 위반 경로를 보려면 `sample_d` 나 `sample_b` 를 쓴다.

## 주차면 점유 판정 (ROI)

`vehicle_detector.py` 는 Pi 에서 "지정된 장애인 주차면 한 칸에 차가 서 있는가"를 판정한다. `pi_client.py` 의 이벤트 모드가 이것을 쓴다(설계는 저장소 루트 `AI_FINAL_DESIGN.md` 와 `docs/adr/0014`). `roi_preview.py` 로 사진이나 카메라 프레임에 대해 판정을 확인할 수 있다.

YOLO 는 화면에 보이는 차를 전부 잡으므로(옆 칸, 뒤쪽 통로 포함) 박스마다 아래 규칙을 모두 통과해야 "그 칸에 있는 차"로 인정한다.

| 규칙 | 설정값 | 기본 | 거르는 것 |
|---|---|---|---|
| 아랫변 중심이 ROI 안 | `PG_ROI` | sample_c 기준 사다리꼴 | 다른 칸 바닥에 서 있는 차 |
| 박스∩ROI / 박스 면적 | `PG_OCC_OVERLAP_ENTER`, `PG_OCC_OVERLAP_EXIT` | 0.30 / 0.15 | 경계에 걸친 차, 뒤쪽 차. 나갈 때는 기준을 낮춰 흔들림을 막는다 |
| 박스 면적 / 화면 | `PG_OCC_MIN_AREA` | 0.05 | 멀리 있는 작은 차 |
| 좌우 끝에 잘린 좁은 박스 | `PG_OCC_EDGE_CLIP_MAX_WIDTH` | 0.35 | 옆 칸 차량(화면에 절반만 보임) |
| YOLO 신뢰도 | `PG_OCC_MIN_CONF` | 0.40 | 불확실한 탐지 |
| 연속 프레임 | `PG_OCC_ENTER_FRAMES`, `PG_OCC_EXIT_FRAMES` | 3 / 6 | 지나가는 차, 잠깐 가려짐 |

`PG_ROI` 는 0~1 로 정규화한 꼭짓점 목록 `x1,y1,x2,y2,...` (3개 이상, 시계 방향)이다. 저해상도 프레임과 원본 사진이 같은 4:3 이라 같은 값을 쓴다. 카메라 위치가 바뀌면 이 값만 바꾼다.

```text
PG_ROI=0.10,0.57,0.90,0.57,1.00,1.00,0.00,1.00     # sample_c: 왼쪽 위, 오른쪽 위, 오른쪽 아래, 왼쪽 아래
```

### 확인 도구

```powershell
# PC: 사진으로 판정 결과를 그려 본다. 초록 = 인정, 빨강 = 제외(사유 표시), 노란 점 = 아랫변 중심
backend\venv\Scripts\python device\roi_preview.py backend\uploads\sample_c.jpg --out roi.jpg
backend\venv\Scripts\python device\roi_preview.py photo.jpg --roi 0.12,0.58,0.88,0.58,1,1,0,1 --out roi.jpg --json roi.json
```

```bash
# Pi: 카메라 프레임 한 장으로 확인 (설치할 때 ROI 를 잡는 용도)
python3 roi_preview.py --camera --model yolo26s_ncnn_model --out /tmp/roi.jpg
```

규칙만 검사하는 테스트는 `python -m pytest device/test_occupancy.py -q` (모델 불필요).

### 모델 선택 (PC 측정, 2026-10-20)

sample_c 처럼 차가 화면의 2/3 를 채우는 각도에서는 nano 모델의 신뢰도가 입력 크기에 따라 0.1~0.5 로 흔들려 쓸 수 없었다. small 은 0.85~0.95 로 안정적이다. 그래서 기본은 `yolo26s`, 입력 480px 다.

| 모델 | 입력 | sample_c 주차 차량 신뢰도 | PC 1프레임 |
|---|---|---|---|
| yolo26n | 480 / 640 / 800 | 0.16 / 0.25 / 0.32 | 43 / 59 / 80 ms |
| yolo26n NCNN | 640 | 0.45 | 92 ms |
| yolo11n | 480 / 640 / 800 | 0.87 / 0.09 / 0.00 | 55 / 62 / 136 ms |
| yolov8n | 480 / 640 | 0.36 / 0.17 | 44 / 88 ms |
| **yolo26s** | **480** / 640 / 800 | **0.92** / 0.89 / 0.85 | **138** / 211 / 331 ms |
| **yolo26s NCNN** | **480** | **0.93** | **217 ms** |

Pi 4 에서의 속도는 아직 재지 않았다. 5초에 한 프레임이면 1초 안쪽이기만 하면 된다. Pi 에서 모델을 준비하려면 PC 에서 내보낸 `yolo26s_ncnn_model/` 폴더(37MB)를 복사하거나 Pi 에서 직접 내보낸다:

```python
from ultralytics import YOLO
YOLO("yolo26s.pt").export(format="ncnn", imgsz=480)
```

### 사진 한 장으로 확인하지 못한 것

- 빈 주차면. sample_c 각도로 찍은 빈 자리 사진이 없어 "없음" 판정은 확인하지 못했다.
- 옆 칸 차량 제외는 다른 각도의 sample_b 로만 확인했다. sample_c 각도에서는 옆 칸 차가 화면에 어떻게 잘려 보이는지 모른다.
- 기준값(0.30, 0.15, 5%, 35%)은 사진 3장에서 역산한 값이다. Pi 카메라 사진 수십 장(빈 자리, 정상 주차, 앞으로 당겨 댄 차, 경계에 걸친 차, 옆 칸만 찬 경우, 야간)으로 다시 정한다.
- Pi 4 의 lores 프레임 픽셀 형식(YUV420)과 변환 코드는 실제 장비에서 확인이 필요하다.

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

- 이 스크립트는 실제 Raspberry Pi에서 실행해 보지 못했다. 카메라와 GPIO 부분은 기존 코드의 설정을 그대로 옮긴 것이다. PC에서는 `--image` 모드로 전송, 응답 처리, 반복 동작만 확인했다. 이벤트 모드의 lores 스트림 설정과 YUV420 변환도 Pi 에서 확인이 필요하다.
- Pi 카메라로 찍은 사진에서 서버의 앞유리 모델이 잘 동작하는지는 실제 사진으로 확인이 필요하다.
