# Parking Guardian Backend

Multi-Tenant 구조의 Flask API 서버다. 장비가 올린 사진을 AI로 판정하고, 위반이면 사건을 저장한 뒤 해당 주차장의 담당 관리자에게 알린다.

API 규격은 `docs/design/API.md` 를 따른다. 기존 단일 파일 서버는 비교용으로 `legacy/` 에 남겨 두었다.

## 구조

```text
backend/
├─ wsgi.py                 실행 Entry Point
├─ app/
│  ├─ __init__.py          create_app()
│  ├─ config.py            환경변수 기반 설정
│  ├─ models/              Table 11개 (companies, admins, parking_lots, admin_parking_lots, ...)
│  ├─ auth/                JWT, 비밀번호, require_auth / require_device, Tenant 범위 규칙
│  ├─ api/                 /api/v1 라우트
│  ├─ services/            Violation 상태 전이, 알림 대상 결정, Audit Log
│  ├─ adapters/            추론(YOLOv5 / mock), 이미지 저장(로컬), 알림 발행(동기)
│  └─ cli.py               flask seed
├─ migrations/             Alembic
├─ scripts/send_detection.py   장비(Raspberry Pi) 역할의 업로드 스크립트
├─ tests/                  pytest
├─ models/                 best.pt, last.pt (Git 제외)
└─ legacy/                 리팩터링 이전 코드
```

## 처음 한 번

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt

# AI 추론까지 쓰려면 (없으면 INFERENCE_BACKEND=mock 으로 실행)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
git clone https://github.com/ultralytics/yolov5 ..\yolov5
pip install -r ..\yolov5\requirements.txt
# models\best.pt (앞유리), models\last.pt (스티커) 를 넣는다

flask --app wsgi db upgrade     # Table 생성
flask --app wsgi seed           # 데모 데이터 입력, 계정과 장비 키가 출력된다
```

## 실행

```powershell
cd backend
.\venv\Scripts\Activate
python wsgi.py                  # http://127.0.0.1:5000
```

설정은 `backend/.env` 로 바꾼다. 항목은 `.env.example` 참고. 기본 DB는 `instance/parking_guardian.db` (SQLite) 이고, `DATABASE_URL` 을 주면 PostgreSQL을 쓴다.

## 데모 계정

비밀번호는 모두 `Passw0rd!` 다.

| Role | 이메일 | 회사 | 담당 주차장 |
|---|---|---|---|
| SUPER_ADMIN | super@pg.local | 없음 | 전체 조회 |
| COMPANY_ADMIN | kim@a.com | A회사 | 강남, 판교 |
| COMPANY_ADMIN | lee@a.com | A회사 | 판교 |
| COMPANY_ADMIN | park@a.com | A회사 | 수원 |
| COMPANY_ADMIN | choi@b.com | B회사 | 본사, 물류센터 |

## 장비 업로드 흉내

```powershell
python scripts\send_detection.py uploads\sample_b.jpg --repeat 2          # 강남 주차장 장비, 2번 연속이라 위반 확정
python scripts\send_detection.py uploads\sample_b.jpg --serial RPI-A-PANGYO-01 --zone B-01 --repeat 2
python scripts\send_detection.py --heartbeat
```

실제 장비용 스크립트는 `device/pi_client.py` 에 있다. PC에서 `--image` 옵션으로 같은 시험을 할 수 있다.

데모 장비 시리얼: `RPI-A-GANGNAM-01`, `RPI-A-PANGYO-01`, `RPI-A-SUWON-01`, `RPI-B-HQ-01`, `RPI-B-LOGIS-01`

장비는 1분마다 사진을 올리므로, 서버는 같은 장비의 연속된 판정을 하나의 사건으로 묶는다 (주차 한 번 = 위반 한 건). 규칙은 `app/services/detections.py` 에 있다.

| result | 의미 | 저장 |
|---|---|---|
| `no_vehicle` | 앞유리가 탐지되지 않음 | 없음. 진행 중인 사건이 있으면 3회 연속일 때 "차량 이탈"로 종료 |
| `permitted` | 앞유리 안에서 스티커가 탐지됨 | 위와 같음 |
| `violation_pending` | 위반 판정 1회째. 확인 중 | 없음 |
| `violation` | 2회 연속 위반으로 확정 | Violation, 이미지, 알림 1회 |
| `violation_ongoing` | 이미 기록된 차량이 계속 있음 | 기존 Violation의 마지막 확인 시각과 탐지 횟수만 갱신 |

횟수는 `.env` 의 `VIOLATION_CONFIRM_COUNT`, `VEHICLE_CLEAR_COUNT`, `EPISODE_GAP_MINUTES` 로 바꾼다.

샘플 3장 기준 실제 모델의 판정: `sample_a` 와 `sample_c` 는 스티커 있음, `sample_b` 는 스티커 없음(위반).

## 테스트

```powershell
python -m pytest tests -q
```

테스트는 메모리 SQLite와 mock 추론을 쓰므로 torch가 없어도 돈다. 회사 간 접근 차단, 담당자에게만 알림 전송, 상태 전이, 부분 실패(이미지 저장 실패, 알림 실패, 추론 실패)를 검증한다.

## DB Schema 변경

```powershell
flask --app wsgi db migrate -m "설명"
flask --app wsgi db upgrade
```
