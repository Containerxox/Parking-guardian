# 단계 4: PC 디바이스 시뮬레이터와 회귀 테스트 (2026-10-30)

목적: Phase 1(API / inference 분리)이 Prototype과 **같은 위반 판정과 장비 이벤트 규칙**을 따르는지 확인한다. 장비 쪽 코드(`device/`)와 서버의 사건 규칙(`record_verdict`)은 이번 단계에서 한 줄도 바꾸지 않았다.

> 로컬 PC의 Docker Desktop에서 한 결과다. 장비는 실제 Raspberry Pi가 아니라 PC 시뮬레이터(`device/pc_event_test.py`)이고, 카메라와 시계만 가짜다(YOLO26s, ROI 판정, 상태 기계, 전송은 실제 코드). "빈 자리" 프레임은 사진을 흐려 만든 합성 프레임이다. 실제 Pi 카메라·빈 자리 판정은 여전히 미검증이다(`PC_VERIFICATION.md`).

## 1. 기존 회귀 테스트

| 대상 | 결과 |
|---|---|
| backend pytest, SQLite | **211 passed** (기존 57 + Phase 1 신규 154) |
| backend pytest, PostgreSQL 16 (`parking_guardian_test`) | **211 passed** (54초) |
| device pytest (`python -m pytest device -q`) | **17 passed** (기존 그대로) |

기존 Prototype 테스트 57개는 수정하지 않았고 모두 통과한다. 새 테스트 6개(`test_compare_pc_event_runs.py`)는 이 단계의 비교 도구를 점검한다.

## 2. 같은 시나리오를 두 구성에서 실행

| | Prototype | Phase 1 |
|---|---|---|
| 구성 | `docker-compose.yml`의 backend(`parking-guardian-backend`, API와 모델이 한 컨테이너) | `docker-compose.yml` + `docker-compose.phase1.yml`: `parking-guardian-api`(torch 없음) → HTTP → `parking-guardian-inference`(실제 YOLOv5, gunicorn worker 1개) |
| `/readyz` | `inference: ready` | `inference: remote` (API 컨테이너에서 `import torch` 실패 확인) |
| 데이터 | 임시 DB와 임시 사진 볼륨 (개발 DB·개발 볼륨 사용 안 함) | 임시 DB와 임시 사진 볼륨 |

실행 순서(두 구성 모두 같다, 새 DB에서):
1. `pc_event_test.py` 위반 차량: `empty:60 → sample_b:900 → empty:90`, 10배속. **arrived → confirm → violation → refresh → left** 전체가 나오도록 사진 구간을 900초(refresh 주기 600초 초과)로 했다.
2. `pc_event_test.py` 정상 차량: `empty:60 → sample_c:900 → empty:90`. arrived → permitted → refresh → left.
3. 마지막으로 `send_detection.py sample_b --repeat 2` (이 실행은 장비에 **열린 위반 사건**을 남기므로 1, 2번 뒤에 해야 한다).

### 2.1 위반 차량 (sample_b): arrived → confirm → violation → refresh → left

| # | event | Prototype: server result | Phase 1: server result | episode_ended | same |
|---|---|---|---|---|---|
| 1 | arrived | violation_pending (sim 71.6 s) | violation_pending (sim 71.1 s) | False / False | True |
| 2 | confirm | violation (sim 133.6 s) | violation (sim 131.6 s) | False / False | True |
| 3 | refresh | violation_ongoing (sim 670.2 s) | violation_ongoing (sim 670.3 s) | False / False | True |
| 4 | left | no_vehicle (sim 988.6 s) | no_vehicle (sim 991.1 s) | True / True | True |

- device state changes: Prototype [None, False, True, False] / Phase 1 [None, False, True, False]
- heartbeats: 18 / 18; LED on at end: (False, False)
- frames: 210 / 209; mean detection ms: 172 / 169
- **identical (events, answers, state changes, heartbeats, LED): True**

서버 상태(두 구성 모두 동일): 위반 **1건**, 종료 사유 `VEHICLE_LEFT`, 탐지 횟수 3, 이력 `DETECTED → NOTIFIED → VEHICLE_LEFT`, 담당 관리자 알림 1건.

### 2.2 정상 차량 (sample_c, 스티커 있음)

| # | event | Prototype: server result | Phase 1: server result | episode_ended | same |
|---|---|---|---|---|---|
| 1 | arrived | permitted (sim 71.7 s) | permitted (sim 71.1 s) | False / False | True |
| 2 | refresh | permitted (sim 670.6 s) | permitted (sim 670.2 s) | False / False | True |
| 3 | left | no_vehicle (sim 988.9 s) | no_vehicle (sim 987.0 s) | False / False | True |

- device state changes: Prototype [None, False, True, False] / Phase 1 [None, False, True, False]
- heartbeats: 18 / 18; LED on at end: (False, False)
- frames: 210 / 210; mean detection ms: 133 / 159
- **identical (events, answers, state changes, heartbeats, LED): True**

서버 상태(두 구성 모두 동일): 위반은 2.1의 1건 그대로(정상 차량이 위반을 만들지 않음), 알림도 1건 그대로.

### 2.3 수동 전송 (`send_detection.py sample_b --repeat 2`)

두 구성 모두 첫 전송 `HTTP 200 violation_pending`, 둘째 `HTTP 201 violation`. 서버 상태(두 구성 모두 동일): 위반 2건(방금 만든 열린 사건 1건 `detection_count 2` + 앞의 종료된 1건), 관리자 알림 2건.

## 3. 실행 기록 (구분해서 남긴다)

| 실행 | 구성 | 결과 | 비고 |
|---|---|---|---|
| (버림) Prototype, 첫 실행 | Prototype | 결과를 쓰지 않음 | 시나리오 순서 결함: `send_detection`을 먼저 돌려 장비에 열린 위반 사건이 남았고, 이어진 시뮬레이터가 새 진입(`violation_pending → confirm`)이 아니라 기존 사건의 연장(`violation_ongoing`)으로 처리됐다. 서버 동작은 정상이고 내 실행 순서가 틀렸다. 순서를 바꾸고 새 DB에서 다시 실행했다 |
| Prototype | Prototype | 기준값으로 사용 | `prototype-b.json`, `prototype-c.json` |
| Phase 1 실행 1 | Phase 1 | sample_b 동일. **sample_c는 이벤트·서버 응답·상태 변화가 같았으나 하트비트 17 vs 18, 프레임 185 vs 210이 달랐다** | `phase1-run1-*.json`. 이 실행에서 장비 PC의 평균 탐지 시간이 376 ms(Prototype 실행 133 ms)였다. 시뮬레이션 시계가 실제 시간의 10배로 흐르므로 PC 쪽 탐지가 느리면 같은 시간에 프레임이 적게 처리되고 하트비트 시각을 하나 놓칠 수 있다. 서버 동작과 무관한 장비 PC의 시간 문제로 판단했으나, 추정이므로 한 번 더 실행했다 |
| Phase 1 실행 2 | Phase 1 | **sample_b, sample_c 모두 Prototype과 완전 동일** (하트비트 18/18, 프레임 b 210/209, c 210/210) | `phase1-run2-*.json`. 위 2.1, 2.2의 표가 이 실행의 값이다. 같은 시나리오 재실행에서 차이가 사라져 실행 1의 차이는 재현되지 않았다 |

원인이 PC 부하였다는 것을 직접 측정으로 확인하지는 않았다. "재실행에서 사라졌다"까지가 확인된 사실이다.

## 4. 결론과 차이

- **같은 것**: 장비가 보낸 이벤트 순서(arrived, confirm, refresh, left), 서버 응답(`violation_pending`, `violation`, `violation_ongoing`, `permitted`, `no_vehicle`), `episode_ended`, 장비 상태 변화, 하트비트 수, LED, 서버에 생긴 위반·이력·알림.
- **다른 것(의도된 구조 차이)**: `/readyz`의 `inference` 값(`ready` → `remote`), 판정을 하는 프로세스(API 안 → inference 컨테이너). 응답의 `inference.backend`는 두 구성 모두 `yolo`다(서비스가 알려 주는 값이다).
- **이 단계에서 보지 않은 것**: AI 장애 시 동작(3단계 `compose-drills.md`), 실제 Pi 카메라와 NCNN 속도, 실제 빈 주차면 사진, 야간. 장비 코드는 바뀌지 않았으므로 이 단계가 새로 확인해 주는 것은 "서버를 분리해도 장비가 보는 세계는 같다"이다.
- 이전 Prototype 기록(`../../PC_VERIFICATION.md` B-2절, 사진 구간 200초)과도 같은 순서·응답이다. 이번에는 사진 구간을 900초로 늘려 refresh까지 포함했다.

## 5. 정리 상태

- 개발 DB(`parking_guardian`)는 위반 8건 그대로, 개발 볼륨(`pgdata`, `backend_storage`)은 유지했다. 이 단계에서 만든 임시 DB 2개와 임시 사진 볼륨은 삭제했다. 공용 사진 볼륨의 파일은 원래의 6개뿐이고 오늘 만들어진 파일은 없다.
- 제가 띄운 컨테이너는 모두 내렸다.
