# 촬영 방식 Before/After 측정 (2026-10-24)

ADR 0014(이벤트 기반 전송과 Heartbeat)의 근거다. 같은 시나리오를 두 장비 모드로 돌려 서버가 한 일을 비교했다. 측정 도구는 `backend/scripts/measure.py`, 원본 결과는 이 폴더의 `before-interval.json`, `after-event.json`(요청 한 건씩의 기록 포함)과 `.log`다.

## 조건

| 항목 | 값 |
|---|---|
| 서버 | `python wsgi.py`(Flask 개발 서버, 5001 포트), 실제 YOLOv5 앞유리·스티커 모델, SQLite, 이 PC(CPU) |
| 시나리오 | 4시간: 빈 자리 30분 → 정상 차량 60분 → 빈 자리 20분 → **위반 차량 60분** → 빈 자리 30분 → 정상 차량 30분 → 빈 자리 10분 |
| 시간 압축 | 30배속(시뮬레이션 1분 = 실제 2초). 실제 8분 |
| 사진 | 정상 `sample_a.jpg`(361 KB), 위반 `sample_b.jpg`(328 KB), 빈 자리는 `sample_a` 를 강하게 흐린 합성 사진(64 KB). 전송 폭 1280px(`PG_SEND_WIDTH` 기준) |
| Before | interval 모드: 1분마다 사진. 차가 없어도 보낸다(지금까지의 `pi_client.py`) |
| After | event 모드: `arrived` / `confirm` / `refresh`(10분) / `left` 사진 + 매분 Heartbeat. 점유 판정은 Pi의 YOLO 대신 시나리오가 준다(연속 프레임 규칙 3/6은 실제 `OccupancyTracker` 사용). 즉 **전송 규약의 효과**를 잰 것이지 Pi 탐지 정확도를 잰 것이 아니다 |
| OCR | 양쪽 모두 꺼짐(`OCR_ENABLED=false`). OCR 비용은 아래에 따로 적었다 |

## 결과 (4시간 시나리오, 측정값)

| 항목 | Before (interval) | After (event) | 변화 |
|---|---|---|---|
| 사진 수 | 240 | 22 (arrived 3, confirm 1, refresh 15, left 3) | −91% |
| 전송량 | 56.6 MB | 5.8 MB | −90% |
| Heartbeat | 0 | 240 (본문 없는 요청) | |
| 서버 추론 시간 합계 | 207.8 s | 12.3 s | −94% |
| 추론 1장 평균 / 중앙값 / p95 | 866 / 730 / 1694 ms | 557 / 562 / 793 ms | |
| 요청 지연 평균 / 최대 | 928 / 5446 ms | 606 / 1005 ms | |
| 서버 프로세스 CPU 시간 | 815 s | 64 s | −92% |
| 서버 RSS | 756~890 MB | 845~910 MB | 아래 주의 |
| 생성된 위반 / 알림 | 1 / 1 (종료 사유 VEHICLE_LEFT) | 1 / 1 (VEHICLE_LEFT) | 같음 |
| 위반 차량 도착 → 알림 | 1.4분 (시뮬레이션 시간) | 1.4분 | 같음 |

**하루 추정** (4시간 × 6. 측정값이 아니라 환산값이다): 사진 1,440장 → 132장, 340 MB → 35 MB, 추론 20.8분 → 1.2분. Heartbeat는 하루 1,440건이 새로 생긴다.

## 읽을 때 주의할 것

- **RSS가 After에서 더 높은 것은 방식 차이가 아니다.** 같은 서버 프로세스로 Before를 먼저 돌리고 After를 돌렸기 때문에 프로세스가 이미 커져 있었다. 메모리는 모델 로딩이 정하고 사진 수와는 무관하다.
- **추론 1장 시간이 After에서 짧은 것**은 Before에서 2초마다 사진이 들어와 요청이 겹쳤기 때문(최대 지연 5.4초)으로 보인다. 사진 한 장의 순수 추론 비용은 같다.
- **알림 지연이 같은 이유**: 양쪽 모두 "첫 위반 판정 → 60초 뒤 두 번째 사진 → 확정"이라 구조적으로 약 1~2분이다. After는 진입 확정에 15초(3프레임)가 더 들지만 Before는 다음 1분 주기를 기다리므로 비슷하다.
- After 기록에 `refresh` 직후 `left` 가 한 번씩 보인다(90.2분 refresh → 90.7분 left). 차가 떠나는 순간 refresh 주기가 겹친 것이다. 빈 자리 refresh 사진은 `no_vehicle` 로 미탐지 1회가 되고 바로 뒤 `left` 가 사건을 닫는다. 동작은 맞다.
- 빈 자리 사진은 합성이라 실제 빈 주차면 사진의 추론 시간과 다를 수 있다(모델은 앞유리를 찾지 못해 `no_vehicle`).
- Flask 개발 서버 1프로세스 기준이다. 컨테이너(gunicorn)에서는 절대값이 다를 수 있지만 두 방식의 비율은 같은 조건이면 유지될 것으로 본다(추정).
- **재지 않은 것**: Pi 4의 탐지 속도·메모리·정확도, 실제 카메라 사진, 장비 여러 대 동시 부하, 네트워크 비용.

## OCR 비용 (ADR 0015, 2026-10-24 이 PC에서 측정) — OCR 은 2026-10-25 제거됨(ADR 0017, `OCR_REMOVAL.md`). 아래는 제거 전 기록

`backend/app/adapters/ocr.py` 의 `EasyKoreanLpOcr` 를 샘플 3장에 직접 돌렸다. 위반 확정 1건당 한 번만 실행된다.

| 항목 | 값 |
|---|---|
| 모델 로딩 | 25.7초, RSS +428 MB (첫 위반 때 한 번) |
| 사진 1장 | 0.5~1.3초 (번호판 탐지 + 인식) |
| sample_a | `138버5401` 신뢰도 0.90. 정확 |
| sample_b | `50로0482` 신뢰도 0.77. 정확 |
| sample_c | `153보2726` 신뢰도 0.95. **틀림**(실제 `153부2726`, 번호판 부분을 잘라 눈으로 확인) |

sample_c 는 2026-10-20 검증(원본 파이프라인: 차량 탐지 → 차량 crop 안에서 번호판 탐지)에서는 `153부2726` 0.84 로 맞게 읽었다. MVP 구현은 차량 탐지 단계를 빼고 사진 전체에서 가장 큰 번호판을 읽는다. 이 차이와 crop 크기 변형(224x128) 때문으로 추정하지만 확인하지 않았다. 전처리를 네 가지로 바꿔 봐도(보간 방식, 원본 크기 유지) 같은 글자가 나왔다. **신뢰도가 높은 오독이 있으므로 번호판은 관리자가 사진으로 확인하는 보조 정보다.** 차량 crop 단계 복원과 여러 crop 투표는 `AI_FINAL_DESIGN.md` 12절(추후 개선)에 넣었다.

## 다시 재는 방법

```powershell
# 1. 측정 전용 서버 (별도 SQLite DB, 5001 포트). Docker 없이 backend/venv 로 실행
cd backend
$env:DATABASE_URL="sqlite:///C:/tmp/measure.db"; $env:IMAGE_STORAGE_DIR="C:/tmp/measure-storage"; $env:PORT="5001"
.\venv\Scripts\python -m flask --app wsgi db upgrade
.\venv\Scripts\python -m flask --app wsgi seed
.\venv\Scripts\python wsgi.py        # 다른 터미널에서. PID 를 적어 둔다

# 2. Before / After
.\venv\Scripts\python scripts\measure.py --mode interval --server-pid <PID> --out ..\docs\measurements\before-interval.json
.\venv\Scripts\python scripts\measure.py --mode event    --server-pid <PID> --out ..\docs\measurements\after-event.json
```

시나리오와 배속은 `--scenario`, `--speed` 로 바꾼다. 첫 요청(모델 로딩, 약 27초)은 측정에서 뺀다.

## 관련 보고서

- `PC_VERIFICATION.md`: Pi 없이 PC 에서 한 장비 기능 검증(2026-10-24)
- `DOCKER_SIMULATION.md`: Docker 환경 + PC 시뮬레이터 수동 모드 통합 검증, 사용 가이드(2026-10-25)
