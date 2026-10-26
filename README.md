# Parking Guardian

장애인 전용 주차구역의 불법주차를 탐지하는 서비스다. Raspberry Pi 카메라가 1분마다 주차 구역을 찍어 서버로 보내고, 서버가 앞유리와 장애인 스티커를 AI로 판정해 위반 차량을 기록하고 담당 관리자에게 알린다.

캡스톤 프로젝트로 만든 단일 서버를 여러 고객 회사가 함께 쓰는 Multi-Tenant 서비스로 리팩터링하는 중이다. 원본은 Git 태그 `legacy-capstone` 에 있다.

## 동작 흐름

```text
Raspberry Pi (1분마다 사진 전송, X-Device-Key 인증)
  → Backend: 장비 인증 → AI 판정 (앞유리 → 스티커) → 같은 차량의 연속 판정을 한 사건으로 묶기
  → 위반 확정 시: Violation 저장 → 사진 저장 → 담당 관리자에게 알림
  → 회사 관리자: 확인 → 현장 조치 → 처리 완료 (삭제하지 않고 상태와 이력으로 남김)
```

- 역할은 두 가지다. `SUPER_ADMIN`(서비스 운영자)은 회사, 주차장, 구역, 장비, 담당 배정을 관리한다. `COMPANY_ADMIN`(고객 회사 관리자)은 자기 회사의 위반을 조회하고 처리한다.
- 회사가 Tenant다. 다른 회사의 데이터는 조회되지 않는다.
- 알림은 그 주차장에 배정된 관리자에게만 간다.
- 주차 한 번 = 위반 한 건. 위반 판정 2회 연속이면 확정, 차량 없음 3회 연속이면 종료.

## 바로 실행 (Docker Compose)

Docker Desktop만 있으면 된다.

```powershell
docker compose up -d --build     # PostgreSQL, Backend, Frontend. 처음 빌드는 몇 분 걸린다
docker compose logs -f backend   # 데모 계정과 장비 키가 출력된다
docker compose down              # 정지. 데이터까지 지우려면 down -v
```

- 화면: http://127.0.0.1:8080
- API: http://127.0.0.1:5000/api/v1
- 데모 계정 비밀번호는 모두 `Passw0rd!`(로컬 기본값. 공개 서버는 `SEED_ADMIN_PASSWORD`, `SEED_DEVICE_KEY_SECRET` 으로 바꾼다, `backend/.env.example`). 서비스 운영자 `super@pg.local`, 회사 관리자 `kim@a.com`, `park@a.com`, `choi@b.com`.
- 실제 AI 판정을 쓰려면 모델 파일 `backend/models/default.pt`, `backend/models/last.pt` 와 저장소 루트의 `yolov5/` 폴더가 필요하다(둘 다 Git에 없다). 없으면 `INFERENCE_BACKEND=mock` 으로 띄우면 파일 이름으로 판정을 흉내 낸다.

장비 없이 업로드를 시험하려면:

```powershell
cd backend
python scripts\send_detection.py uploads\sample_b.jpg --repeat 2   # 스티커 없는 차량을 2번 보내 위반을 만든다
```

## 구조

```text
backend/     Flask API. 인증, Tenant 격리, AI 판정, 사건 묶기, 알림, 감사 로그. 자세한 실행 방법은 backend/README.md
frontend/    React 화면. 운영자용과 회사 관리자용. 모바일 폭 대응
device/      Raspberry Pi에서 돌리는 촬영·전송 스크립트. device/README.md
docker/      PostgreSQL 초기화 스크립트
docker-compose.yml
```

| 영역 | 기술 |
|---|---|
| Backend | Python 3.12, Flask, SQLAlchemy, Alembic, PyJWT, gunicorn |
| AI | YOLOv5 (PyTorch CPU). 앞유리 탐지 후 그 안에서 스티커 탐지 |
| DB | PostgreSQL 16 (Compose). 로컬 단독 실행은 SQLite |
| Frontend | React (Create React App), nginx |
| 장비 | Raspberry Pi, picamera2 |

## 개발

```powershell
cd backend
python -m pytest tests -q        # 31개. 메모리 SQLite와 mock 추론이라 torch도 Docker도 필요 없다
```

Docker 없이 Backend와 Frontend를 따로 띄우는 방법, PostgreSQL로 테스트하는 방법, 장비 설정은 `backend/README.md` 와 `device/README.md` 에 있다.

## 진행 상황

- 완료: 도메인과 DB 리팩터링, JWT 인증과 Tenant 격리, 사건 묶기, 상태와 이력, 감사 로그, 운영 지표, PostgreSQL, 컨테이너화, 모바일 대응
- 다음: 사진 저장을 S3로 교체, AI 추론을 별도 서비스로 분리, 알림을 큐와 작업자로 분리, AWS 배포, Terraform과 CI/CD
