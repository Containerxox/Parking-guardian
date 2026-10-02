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

## Docker Compose로 전체 실행

Python, Node.js, torch를 설치하지 않고 DB, Backend, Frontend를 한 번에 띄운다. Docker Desktop이 켜져 있어야 한다.

```powershell
cd C:\parking_guardian\capston
docker compose up -d --build     # 처음 빌드는 torch를 내려받느라 몇 분 걸린다
docker compose ps                # backend 가 healthy 가 되면 준비 완료
docker compose logs -f backend   # 데모 계정과 장비 키가 여기에 출력된다
docker compose down              # 정지. 데이터까지 지우려면 down -v
```

| 주소 | 내용 |
|---|---|
| http://127.0.0.1:8080 | 화면. `/api/` 로 시작하는 요청은 nginx가 Backend로 넘긴다 |
| http://127.0.0.1:5000 | Backend API 직접 접근. `scripts\send_detection.py` 가 기본으로 쓰는 주소 |

- 준비물: `backend\models\default.pt`, `backend\models\last.pt`, 저장소 루트의 `yolov5\` 폴더. 모델 없이 띄우려면 `$env:INFERENCE_BACKEND = "mock"` 을 주고 실행한다.
- 시작할 때 DB 마이그레이션과 데모 데이터 입력이 자동으로 된다.
- 컨테이너를 띄운 뒤 첫 사진은 모델을 읽느라 약 30초 걸린다. 그다음부터는 약 1초다.
- `python wsgi.py` 와 같은 5000번 포트를 쓴다. 둘을 같이 띄우려면 `$env:BACKEND_PORT = "5050"` 처럼 포트를 바꾼다. 화면 포트는 `FRONTEND_PORT`.
- 위반 사진은 `backend_storage` volume에 저장된다.

## PostgreSQL로 실행

운영 환경과 같은 DB로 돌려 보려면 저장소 루트의 `docker-compose.yml` 로 PostgreSQL 16을 띄운다.

```powershell
cd C:\parking_guardian\capston
docker compose up -d db          # 127.0.0.1:5432, 개발용 DB와 테스트용 DB가 함께 만들어진다

cd backend
$env:DATABASE_URL = "postgresql+psycopg://parking:parking_dev_password@127.0.0.1:5432/parking_guardian"
flask --app wsgi db upgrade
flask --app wsgi seed
python wsgi.py
```

- 주소는 `localhost` 가 아니라 `127.0.0.1` 로 쓴다. 이 PC에서 `localhost` 는 연결 한 번에 1분 넘게 걸렸다 (IPv6 주소를 먼저 시도하는데 컨테이너 포트는 IPv4에만 열려 있다).
- PostgreSQL 사용자 이름은 `pg_` 로 시작할 수 없다. 그래서 기본 사용자는 `parking` 이다.
- 계속 PostgreSQL을 쓰려면 `backend/.env` 에 `DATABASE_URL` 을 적어 둔다.
- 정지는 `docker compose down`, 데이터까지 지우려면 `docker compose down -v`.

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

테스트는 기본으로 메모리 SQLite와 mock 추론을 쓰므로 torch도 Docker도 없이 돈다. 같은 테스트를 PostgreSQL에서 돌리려면 `TEST_DATABASE_URL` 을 준다.

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://parking:parking_dev_password@127.0.0.1:5432/parking_guardian_test"
python -m pytest tests -q
```

SQLite는 일부 구문을 무시하므로 DB 제약이나 잠금에 관련된 변경은 PostgreSQL에서도 돌려야 한다. 실제로 장비 업로드의 행 잠금 구문이 SQLite에서는 통과하고 PostgreSQL에서만 실패했다.

회사 간 접근 차단, 담당자에게만 알림 전송, 상태 전이, 부분 실패(이미지 저장 실패, 알림 실패, 추론 실패)를 검증한다.

## DB Schema 변경

```powershell
flask --app wsgi db migrate -m "설명"
flask --app wsgi db upgrade
```
