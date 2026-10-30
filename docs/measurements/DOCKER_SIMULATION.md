# Docker 환경 + PC 시뮬레이터 통합 검증 (2026-10-25)

Docker Compose 로 띄운 실제 서비스(PostgreSQL, Flask + 실제 모델, React/nginx)에 PC 시뮬레이터(`device/pc_event_test.py`)가 **실제 사진 → YOLO26s 점유 판정 → 이벤트 전송** 을 보내고, 서버의 **실제 AI 판정 → 사건 저장 → 알림 → 관리자 처리 → 출차 종료** 까지 확인했다. Mock 판정은 쓰지 않았다.

- 환경: `docker compose up -d --build`, `INFERENCE_BACKEND=yolo`(기본), `OCR_ENABLED` 미설정(=false). 기존 DB 와 volume 은 그대로 두었다(기존 위반 1~3번 유지).
- 시뮬레이터 변경: `--interactive` 옵션 추가(아래). 자동 실행 방식은 그대로다. 판정·사건·DB 코드는 건드리지 않았다.
- 결과 JSON 과 로그는 세션 임시 폴더에만 있다. 아래 표가 그 내용이다.

## 1. 컨테이너 상태

| 컨테이너 | 상태 | 포트 |
|---|---|---|
| `parking-guardian-db-1` (postgres:16-alpine) | Up (healthy) | 127.0.0.1:5432 |
| `parking-guardian-backend-1` (gunicorn + YOLOv5 모델) | Up (healthy), `/readyz` = `{"database":"ok","status":"ok"}` | 127.0.0.1:5000 |
| `parking-guardian-frontend-1` (nginx) | Up, `GET /` = 200, `/api/` → backend | 127.0.0.1:8080 |

첫 사진 요청에서 모델 로딩 24.4초(백엔드 로그 `YOLOv5 models loaded in 24.4s`). 그다음부터 사진 1장 0.4~1.3초(1280 폭 기준).

## 2. 사진 판정 결과 — **서버 스티커 판정이 라벨과 반대다**

요청에서 준 라벨은 d = 정상, e = 위반, f = 빈 자리였다. **장비 쪽 점유 판정(YOLO26s + ROI)은 라벨대로 나왔지만, 서버 쪽 스티커 판정은 d 와 e 가 뒤집혔다.** 모델이나 기준값은 바꾸지 않았다(사용자 원칙). 아래는 측정값이다.

### 2-1. 장비(Pi 역할): YOLO26s 480px + ROI 기본값 (`roi_preview.py`)

| 사진 | 크기 | 탐지된 차 | 인정 | 제외 사유 | 점유 |
|---|---|---|---|---|---|
| sample_d (주황 차, 스티커 있음) | 3456x4608 | 2 | #0 conf 0.93, 면적 32%, 겹침 54% | 왼쪽 옆 차: 면적 2.7%, ROI 밖, 가장자리 잘림 폭 16% | **있음** |
| sample_e (회색 SUV, 스티커 없음) | 3024x4032 | 3 | #2 conf 0.84, 면적 39%, 겹침 50% | 좌·우 옆 칸 차: 겹침 22% / 10%, 잘림 폭 24% / 19% | **있음** |
| sample_f (빈 자리, 왼쪽에 옆 차) | 4284x5712 | 5 | 없음 | 옆 차(conf 0.91): 아랫변 ROI 밖, 겹침 1%, 잘림 폭 19%. 나머지 4개는 담장 너머 차(conf 0.10~0.38, 면적 ≤2.7%) | **없음** |

세 장 모두 세로 사진(3:4)인데 ROI 는 정규화 좌표라 그대로 적용됐다. **실제 빈 자리 사진(sample_f)에서 "없음" 판정이 처음으로 확인됐다**(이전 PC_VERIFICATION 에서는 흐린 합성 프레임뿐이었다). 옆 칸 차량은 가장자리 잘림·겹침 규칙으로 제외됐다.

### 2-2. 서버(Flask): 앞유리 `default.pt` + 스티커 `last.pt` (같은 코드·모델을 로컬에서 직접 실행, 원본과 1280 폭 전송본 둘 다)

| 사진 | 전송 형태 | 앞유리 conf | 스티커 | 서버 verdict | 라벨 |
|---|---|---|---|---|---|
| sample_d | 원본 3.7 MB | 0.952 | **없음** | **violation** | 정상(스티커 있음) |
| sample_d | 1280 폭 346 KB | 0.953 | **없음** | **violation** | |
| sample_e | 원본 2.9 MB | 0.956 | 1개 conf 0.725 (앞유리 위쪽 가운데) | **permitted** | 위반(스티커 없음) |
| sample_e | 1280 폭 472 KB | 0.956 | 1개 conf 0.579 (기준 0.5) | **permitted** | |
| sample_f | 원본 9.4 MB | 앞유리 없음 | | no_vehicle | 빈 자리 ✔ |
| sample_f | 1280 폭 1 MB | 앞유리 없음 | | no_vehicle | ✔ |

- sample_d 의 둥근 스티커(앞유리 왼쪽 아래)를 스티커 모델이 잡지 못한다. 원본 크기에서도 못 잡으므로 전송 축소 탓이 아니다.
- sample_e 는 앞유리 위쪽 가운데의 작은 빨간 물체를 스티커로 본다(0.58~0.73). 1280 폭에서는 기준 0.5 를 간신히 넘는다.
- 스티커 모델은 학습 초반 체크포인트라는 점이 CLAUDE.md 에 이미 적혀 있다. 이번 결과는 그 한계가 실제 사진에서 드러난 것이다. 개선은 사용자 우선순위가 아니라 하지 않았다.

**그래서 시나리오 검증은 서버가 실제로 위반으로 보는 sample_d 를 위반 경로에, permitted 로 보는 sample_e 를 정상 경로에 썼다.** 라벨과 반대라는 점을 표마다 적어 둔다.

## 3. 자동 시나리오 (10배속, 컨테이너 서버 5000, 장비 `RPI-A-GANGNAM-01` / 강남 주차장 A-01 / 담당 kim@a.com)

### 3-1. 정상 차량 경로 (sample_e. 서버 판정 permitted) → 시나리오 1

```powershell
backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5000/api/v1 --key pgk_dev_rpi-a-gangnam-01 --timeline "backend/uploads/sample_f.jpg:60,backend/uploads/sample_e.jpg:700,backend/uploads/sample_f.jpg:90" --speed 10
```

| 시뮬 초 | 장비 상태 변화 | event | 서버 verdict | result | 비고 |
|---|---|---|---|---|---|
| 12.3 | 비점유 확정(부팅 뒤 3프레임) | 사진 없음 | | | Heartbeat 만 |
| 72.7 | 점유 확정(3프레임) | `arrived` sample_e | permitted (스티커 1) | `permitted` | |
| 672.8 | 주차 유지 10분 | `refresh` sample_e | permitted | `permitted` | |
| 787.8 | 비점유 확정(6프레임) | `left` sample_f | no_vehicle | `no_vehicle` | 닫을 사건 없음 |

프레임 168장, 탐지 평균 239 ms, Heartbeat 15회. **위반 없음, 알림 없음**(DB 위반 수 변화 없음).

### 3-2. 위반 차량 경로 (sample_d. 서버 판정 violation) → 시나리오 2, 3, 4

```powershell
backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5000/api/v1 --key pgk_dev_rpi-a-gangnam-01 --timeline "backend/uploads/sample_f.jpg:60,backend/uploads/sample_d.jpg:700,backend/uploads/sample_f.jpg:90" --speed 10
```

| 시뮬 초 | event | 서버 verdict | result | 시나리오 |
|---|---|---|---|---|
| 71.7 | `arrived` sample_d | violation (스티커 0) | `violation_pending` | 2 |
| 131.9 | `confirm` sample_d (60초 뒤 새 사진) | violation | `violation` 201, violation_id 4 | 2: 위반 확정, 알림 생성 |
| 670.0 | `refresh` sample_d | violation | `violation_ongoing` | 3 |
| 791.6 | `left` sample_f | no_vehicle | `no_vehicle` + `episode_ended` | 4: 1회로 종료 |

DB(PostgreSQL): `violations` 4번 status NOTIFIED, detection_count 3, end_reason `VEHICLE_LEFT`, image_status UPLOADED, plate_number 없음(OCR 꺼짐). `violation_history` DETECTED → NOTIFIED → VEHICLE_LEFT. `notifications` admin 2(kim@a.com) IN_APP SENT + EMAIL SENT(이메일 실제 발송은 미구현, 행만 생김). `machines` 1번 카운터 0으로 복귀.

## 4. 수동 모드 (`--interactive`)

### 동작

- `result=violation`(확정) 응답 직후에만 멈춘다. `violation_pending` 에서는 멈추지 않는다.
- 멈춘 동안 시뮬레이션 시계를 세우고, **실제 시간 60초(`PG_HEARTBEAT_INTERVAL`)마다 Heartbeat 를 보낸다.** 서버의 끊김 판정(10분)에 걸리지 않는다. Flask, PostgreSQL, 프론트엔드는 그대로 돈다.
- Enter 를 누르면 타임라인의 **다음 항목(빈 자리)으로 건너뛰어** 6프레임 비점유 확인 뒤 `left` 를 보낸다. 건너뛴 시간만큼 refresh 기한도 미룬다(안 그러면 재개 직후 빈 자리 사진이 `refresh` 로 나간다. 1차 실행에서 실제로 그랬고 고쳤다).
- 구현은 `pc_event_test.py` 안에서만: `SimClock.pause/resume`, `wait_for_enter`(stdin 읽는 스레드 + Heartbeat 루프), `TimelineCamera.current_step_end`. 비동기 시스템이나 큐는 없다.
- 함께 고친 것: 사진 요청을 기다리는 동안도 시계를 세운다. 컨테이너 서버의 첫 추론(모델 로딩 24초)이 10배속에서 240초를 건너뛰어 타임라인이 끝나 버렸기 때문이다. 요청 실제 소요는 `server_ms` 로 기록한다. 이벤트 기록에 서버 `verdict`, `sticker_count` 를 추가했다.

### 실행 기록 (2회. Enter 는 파일로 흉내)

| 실행 | arrived | confirm | 대기 중 | Enter 뒤 | 관리자 처리 | 최종 |
|---|---|---|---|---|---|---|
| 1차 (violation 5) | 72.3초 pending | 132.3초 violation | Heartbeat 1회(실제 60초 뒤), 알림 읽음·확인·처리완료 | (고치기 전) `refresh` sample_f → no_vehicle, 그 뒤 `left` → 종료 | ACKNOWLEDGED → RESOLVED | status **RESOLVED**, end_reason **VEHICLE_LEFT** |
| 2차 (violation 6) | 80.9초 pending | 160.5초 violation | Heartbeat 1회, 확인만 | `left` 만 → 종료 | ACKNOWLEDGED 만 | status **IN_PROGRESS**, end_reason **VEHICLE_LEFT** |

두 실행이 보여 주는 것: **관리자 처리 상태(status)와 사건 종료(ended_at, end_reason)는 서로 독립이다.** 처리완료를 해도 사건은 열려 있다가 출차로 닫히고(5번), 처리를 안 끝내도 출차로 닫힌다(6번). 이력에는 둘 다 시간순으로 남는다.

## 5. 프론트엔드 검증 — API 로만 확인, 화면은 열지 못했다

이 세션에는 브라우저 자동화 도구가 없어서 **React 앱이 호출하는 API 를 프론트엔드 컨테이너의 nginx(8080 `/api/`)를 거쳐 같은 순서로 호출**했다. 아래는 그 결과이고, 실제 화면 렌더링은 사용자가 아래 가이드로 확인한다.

| 기능 | 호출 | 결과 |
|---|---|---|
| 관리자 로그인 | `POST /auth/login` kim@a.com | 200, COMPANY_ADMIN |
| 신규 알림 | `GET /notifications?unread=true` | 위반 5번 알림(IN_APP, SENT, 강남 주차장 A-01) 포함, unread_count 2 |
| 위반 목록 | `GET /violations?status=open` | 5번 포함 |
| 위반 상세 + 이력 | `GET /violations/5` | NOTIFIED, detection_count 2, history DETECTED·NOTIFIED |
| 증거 사진 | `GET /violations/5/image-url` → 서명 URL | 200 image/jpeg 354,466 bytes. **장비가 보낸 바이트와 SHA-256 동일**(재압축 없음) |
| 알림 읽음 | `POST /notifications/11/read` | 200 |
| 확인 | `POST /violations/5/acknowledge` | 200 IN_PROGRESS |
| 처리완료 | `POST /violations/5/resolve` | 200 RESOLVED, ended_at 은 여전히 null |
| 출차 종료 | (시뮬레이터 `left`) | ended_at 기록, end_reason VEHICLE_LEFT, status RESOLVED 유지 |

## 6. 사용 가이드 (직접 재현)

1. **Docker Compose 실행** (Docker Desktop 켠 뒤)

   ```powershell
   cd C:\parking_guardian\capston
   docker compose up -d --build
   docker compose ps          # db, backend 가 healthy 가 될 때까지
   ```

2. **관리자 웹페이지 접속·로그인**: `http://127.0.0.1:8080`, `kim@a.com` / `Passw0rd!` (강남 주차장 담당). 알림 종과 위반 목록을 열어 둔다.

3. **PC 시뮬레이터 수동 모드 실행** (다른 터미널). 서버가 위반으로 판정하는 사진은 현재 모델 기준 **sample_d**(또는 sample_b)다. sample_e 를 넣으면 `permitted` 가 나와 멈추지 않는다(2절).

   ```powershell
   cd C:\parking_guardian\capston
   backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5000/api/v1 --key pgk_dev_rpi-a-gangnam-01 --interactive --timeline "backend/uploads/sample_f.jpg:60,backend/uploads/sample_d.jpg:700,backend/uploads/sample_f.jpg:90" --speed 10
   ```

   첫 실행은 YOLO26s 로딩 몇 초 + 서버 첫 추론 25~35초가 더 걸린다. 10배속이라 `arrived` 까지 약 10초, `confirm` 은 그 6초 뒤다. 실제 시간 그대로 보려면 `--speed 1`(confirm 까지 60초).

4. **위반 확정 후 관리자 화면 확인**: 터미널에 `위반이 확정되었습니다 (violation_id=N). ... Enter 키를 누르세요.` 가 뜨면 브라우저에서 새 알림 → 위반 상세(사진, 이력 DETECTED·NOTIFIED) → 확인 → 처리완료를 해 본다. 대기 중 60초마다 `(대기 중 Heartbeat 전송)` 이 찍힌다.

5. **Enter 로 출차 진행**: 빈 자리 사진 6프레임(10배속 3초) 뒤 `left` 가 가고 `서버가 이전 위반 사건을 종료했습니다` 가 찍힌다.

6. **사건 종료 결과 확인**: 위반 상세를 새로 고치면 이력 마지막에 `VEHICLE_LEFT`(차량이 주차 구역을 벗어난 것을 확인) 가 붙고, 처리 상태는 4단계에서 한 그대로(처리완료면 RESOLVED, 안 했으면 IN_PROGRESS/NOTIFIED)다. 컨테이너는 계속 떠 있다. 끝낼 때 `docker compose down`(데이터 유지).

## 7. 오류와 미검증

- **스티커 모델 오판**(2-2절): sample_d 스티커 미탐지, sample_e 오탐. 사용자 라벨과 반대. 모델 개선 없이는 e 로 위반 경로를 재현할 수 없다.
- 1차 수동 실행에서 Enter 뒤 빈 자리 사진이 `refresh` 로 먼저 나갔다(서버는 no_vehicle 로 받아 clear_count 1, 사건은 유지). refresh 기한을 미루도록 고쳤고 2차에서 재현되지 않았다.
- 첫 실행에서 서버 모델 로딩(24초)이 시뮬레이션 시계를 240초 건너뛰게 해 타임라인이 끝났다. 요청 중 시계를 세우도록 고쳤다.
- 10배속에서는 프레임 처리(탐지 0.2~0.3초 = 시뮬 2~3초)가 시계를 따라가느라 `confirm` 이 60초보다 뒤(2차 80초)에 나갈 수 있다. 실제 Pi 는 1배속이라 해당 없다.
- 브라우저 화면 자체는 열어 보지 못했다(5절). 이메일 알림은 행만 생기고 발송은 미구현(기존 사실).
- 출력이 파이프(파일 저장)로 갈 때는 버퍼링 때문에 멈춤 안내가 늦게 보일 수 있다. 터미널에서 직접 실행하면 바로 보인다. 필요하면 `python -u`.
- 실제 Raspberry Pi, Pi 카메라 사진, NCNN 속도는 여전히 미검증(`PC_VERIFICATION.md`).
