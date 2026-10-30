# 0019. 사진 접수와 AI 판별을 S3 + SQS FIFO 로 나누고, 상태 이벤트는 큐를 거치지 않는다

- 상태: **제안** (2026-10-30). 방향만 정했다. **Phase 1(0018) 측정 결과를 보고 확정하거나 범위를 줄인다.** 측정이 근거를 주지 않으면 그 사실을 이 문서에 적는다.
- 전체 설계: `docs/design/ARCHITECTURE_EVOLUTION.md` 6장(상세 표와 상태 전이도는 그쪽이 원문).
- 바꾸는 결정: 0014 의 "응답으로 confirm 을 요청", "재시도·`event_id`·`seq` 는 MVP 제외". 알림 발행 Adapter(0007)를 큐 뒤로.
- 유지하는 결정: 0004(주차 한 번 = 위반 한 건, 2회 연속 확정), 0005(판단은 서버. 사건 종료도 서버 판정으로만), 0006, 0014 의 사진을 보내는 때(arrived / confirm / refresh / left), 빈 자리·모든 프레임은 보내지 않음.

## 문제

Phase 1 에서도 장비 요청은 판정이 끝날 때까지 연결과 API 스레드를 잡는다. 판정 용량을 넘는 사진은 거절되고 장비에는 재시도가 없다. 판정 용량은 사진이 오는 순간에 이미 떠 있어야 한다. (거절·유실의 실제 크기는 Phase 1 측정으로 확인한다.)

비동기로 바꾸면 코드에서 확인한 두 가지가 깨진다.

- `record_verdict()` 는 끊김 판정과 `last_seen_at` 에 처리 시각(`utcnow()`)을 쓴다. 큐 지연이 생기면 정상 장비가 SIGNAL_LOST 로 닫힐 수 있다.
- 장비는 응답의 `violation_pending` 을 보고 60초 뒤 confirm 을 찍는다. 응답에 판정이 없으면 이 경로가 끊긴다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| SQS Standard + DB 가드만 | 순서 보장이 없어 모든 순서 문제를 DB 가드가 떠안는다 |
| **SQS FIFO(그룹 = 장비) + DB 순서 가드** | 채택. 장비별 순서는 큐가, 재전달·redrive 로 섞이는 경우는 가드가 막는다 |
| 그룹 = 회사 / 주차장 | 무관한 장비끼리 서로를 막는다. 독 메시지 하나가 회사·주차장 전체를 멈춘다 |
| 그룹 = 주차면 | 사건 상태는 Machine 행에 있다. 한 장비가 여러 면을 보면 두 그룹이 같은 행을 고친다 |
| confirm: 장비 폴링 / 진입 뒤 항상 confirm | 요청 증가 / 정상 차량도 사진 2장 |
| **confirm: Heartbeat 응답의 명령** | 채택 |

## 결정

1. **두 경로.** 상태 경로(Heartbeat, 세션 시작, 출차 보고, 명령 확인)는 API 가 DB 에 바로 반영한다. 판별 경로(모든 사진의 위반 판별)만 큐를 탄다. `left` 요청은 상태 부분이 즉시 반영되어 AI 적체에 막히지 않고, 사건 종료는 빈 자리 판정이 확인할 때 한다.
2. **접수 순서**: S3 `inbox/`(event_id 로 정해지는 키) → DB `detection_jobs` 행(아웃박스) commit → SQS SendMessage → 202. 전송이 실패해도 202 를 주고 재전송기가 보낸다.
3. **재전송기(outbox-relay)**: `pending_publish` 작업을 장비별 seq 순서로 전송(지수 백오프, 포기하지 않음), DLQ 메시지를 DB `dead_lettered` 로 옮김, 끊긴 부수 효과 마무리, 보존 기간 정리. DB 가 진실의 원천이고 SQS 는 운반 수단이다.
4. **식별자**: `event_id`(중복 제거, `UNIQUE(machine_id, event_id)`), `session_id`(진입 ~ 출차), `seq`(세션 안 순서), `command_id`, `job_id`(SQS 중복 제거 ID). 장비 시계(`detected_at`)는 순서 판단에 쓰지 않는다.
5. **순서 가드**: Machine 행에 `current_session_id`, `last_applied_seq`. 현재 세션의 더 큰 seq 만 사건 상태를 바꾸고, 나머지는 판정만 기록하는 `stale`. `left` 판별은 현재 세션의 사건만 닫는다.
6. **시각**: `record_verdict(observed_at=접수 시각)`. 끊김 판정은 상태 경로의 마지막 신호 시각으로 한다.
7. **Worker**: 조건부 UPDATE 로 선점(`claim_attempt` fencing), 추론은 트랜잭션 밖, 판정 결과·사건 반영·작업 `done` 은 한 트랜잭션. 증거 사진 복사와 알림은 commit 뒤 멱등 부수 효과(`effects_status`). 락 유지, 동시 처리 1.
8. **confirm**: Worker 가 `device_commands` 에 `capture_confirm`(`due_at` = 접수 + 60초, `expires_at` = due + 9분)을 만든다. Heartbeat 응답이 명령을 싣고, 장비는 max(due_at, 받은 시각)에 찍는다. 장비는 실행한 command_id 를 기록해 중복 실행하지 않는다.
9. **장비**: 로컬 아웃박스(디스크), seq 순서 전송, full jitter 지수 백오프(최대 60초, `Retry-After` 존중), 이벤트별 포기 기준(refresh·confirm 10분, arrived·left 30분), 202·200(중복) 모두 성공.
10. **보존**: 판별용 임시 사진 `inbox/` 7일, 확정 증거 `violations/` 90일(지금은 기록만 하는 `IMAGE_RETENTION_DAYS` 를 수명 주기로 실제 적용), 작업 행 30일, DLQ 4일.
11. **확장**: KEDA SQS 스케일러, min 1(모델 로딩 때문에 0 은 쓰지 않음).

## 이유

- 큐가 순서를, DB 가 진실을 맡으면 각 실패 지점의 복구가 "다시 보내기" 와 "다시 처리해도 같은 결과" 두 가지로 줄어든다.
- 상태와 판별을 나누면 AI 장애 동안에도 장비 연결 상태와 출차 보고가 정확하다.
- 타이밍 분석(설계 문서 6.9): confirm 시각을 서버가 `due_at` 으로 정해 주면 큐 대기 + 추론 시간 q ≤ 60초일 때 늦음은 최대 q 다. Heartbeat 1분 주기를 바꾸지 않아도 된다. 2회 확정은 refresh 사진도 세므로 confirm 이 끝내 안 와도 판정은 10분 안에 이어진다.

## 결과

- 얻을 것(예상, 측정 전): 접수 지연이 판별 용량과 분리된다. 과부하에서 사진이 거절 대신 적체된다. Worker 장애 동안 관리자 API 와 장비 상태가 정상이다.
- 감수할 것: 테이블 4개(작업, 판정 결과, 세션, 명령), 재전송기, 상태 기계, redrive 운영 절차, 장비 프로토콜 변경. LED·confirm 이 Heartbeat 한 주기 늦을 수 있다. **End-to-End 판별 지연은 동기보다 늘 수 있다.** 비교 표의 1행은 접수 응답이 아니라 End-to-End 지연과 유실 수다.
- 비용(추정): SQS 는 무료 범위 안. SQS VPC Endpoint 는 월 십수 달러대라 쓰지 않고 NAT 를 경유한다.

## 열린 질문

1. AI 가 오래 멈췄을 때 장비 출차 보고만으로 사건을 닫는 대체 규칙(0005 원칙 변경).
2. 판정 대기 중 Heartbeat 15초(적응형) 를 공식 구성에 넣을지.
3. 동기 엔드포인트를 비교 재현용 설정으로 남길지(권장).

## 근거

- 코드: `backend/app/services/detections.py`(Machine 행 잠금, 처리 시각 사용, 2회 확정이 모든 이벤트 사진을 셈), `backend/app/models/parking.py`(상태 소유자 Machine, ParkingZone.machine_id), `backend/app/services/notifications.py`(알림 유니크 키), `device/pi_client.py`(응답 의존 confirm, 재시도 없음).
- 부분 실패 15가지와 멱등성 검증표 13항목: 설계 문서 6.11, 6.13.

## 한 줄 설명

사진 접수와 AI 판별을 큐로 떼어 내되, 장비 상태는 큐 밖에서 즉시 반영하고, 순서·중복·부분 실패를 식별자와 아웃박스로 다뤄 "다시 보내도, 다시 처리해도 같은 결과" 가 되도록 했습니다.
