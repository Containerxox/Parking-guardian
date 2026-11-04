# 사진 10장 스모크 · 장비 준비 · 시험 일정 계획 (2026-11-04) — 장비 3대 등록과 스모크는 실행 완료(6절), 본 시험은 아직

분석 근거: [P2_ANALYSIS_2026-11-04.md](P2_ANALYSIS_2026-11-04.md), 도구: [P2_TOOLS.md](P2_TOOLS.md). 표기: **[관찰]**, **[시험]**(로컬 시험), **[계산]**, **[추정]**, **[미확인]**.
`<…>` 는 자리표시자다. 계정 ID·호스트·키는 문서에 적지 않는다. 모든 단계는 **별도 승인** 뒤에만 실행한다.

## 1. 지금 장비 상태 (읽기 전용 조회, 2026-11-04 [관찰])
| 장비 | 회사/주차장 | 세션 | 마지막 신호 | 활성 사건(`ended_at IS NULL`) |
|---|---|---|---|---|
| 1 `RPI-A-GANGNAM-01` | 1 / 1 | 없음 | 10-28 | **위반 #3 (RESOLVED)** |
| 2 `RPI-A-PANGYO-01` | 1 / 2 | 없음 | 11-01 | 없음 (Phase 1 부하 장비) |
| 3 `RPI-A-SUWON-01` | 1 / 3 | 활성 1 (seq 1) | 11-04 05:16 | 없음 (첫 E2E) |
| 4 `RPI-B-HQ-01`, 5 `RPI-B-LOGIS-01` | 2 / 4, 5 | 없음 | 11-01 | 없음 (Phase 1 부하 장비) |
| 6 `RPI-E2E-TEST-01` | **3 / 6** | 활성 1 (seq 2) | 11-04 07:07 | **위반 #5 (RESOLVED, 관리자 UI 시험에서 처리 완료)** |
- 시리얼 `RPI-LOAD-01/02` 는 **없다**(중복 없음). 주차장 6 은 ACTIVE, 회사 `E2E-TEST-COMPANY`, 장비 1대.
- `detection_jobs` 는 장비 3(1건)·6(2건)만 있고 모두 `done`. `device_sessions` 활성 2개.

## 2. 장비 2대 생성 계획 (미실행)
**API [관찰: `api/parking.py`]**: `POST /api/v1/machines` (SUPER_ADMIN만) — 본문 `{parking_lot_id, serial_number, name}`. 시리얼이 있으면 409. 응답 201 에 원본 `api_key` 가 **한 번만** 들어 있고 서버에는 해시만 남는다. `POST /machines/<id>/rotate-key`(SUPER_ADMIN)는 새 키를 한 번 돌려준다(이전 키 무효). 둘 다 `audit_logs` 에 1줄(`MACHINE_REGISTERED`, `MACHINE_KEY_ROTATED`).
**도구**: `tools/p2_devices.py` — 공식 API 만, 비밀번호는 환경변수/`getpass`, 출력·저장 없음 [시험: 모의 서버, 가짜 키·비밀번호 누출 검사 통과].
1. **읽기 전용 점검**: `python tools/p2_devices.py preflight --base-url https://APP_HOST --lot 6 --serials RPI-E2E-TEST-01,RPI-LOAD-01,RPI-LOAD-02,RPI-LOAD-03` — 회사 이름 `E2E-TEST-COMPANY` 확인, 시리얼 존재·상태·소속 주차장, 문제가 있으면 종료 코드 2. (SUPER_ADMIN 로그인은 사용자가 자기 터미널에서 한다.)
2. **dry-run**: `create … --serials RPI-LOAD-01,RPI-LOAD-02,RPI-LOAD-03` — 등록할 목록만 출력, 변경 없음.
3. **등록**: `create … --serials RPI-LOAD-01,RPI-LOAD-02,RPI-LOAD-03 --apply` — 없는 시리얼만 등록, 이미 있는 것은 건드리지 않음, 회사가 다르면 중단. 응답의 키는 **읽지도 않고 버린다**.
4. **키는 사용 직전에만 발급**: `launch --yes-rotate -- <명령>` 이 `rotate-key` 로 새 키를 받아 **자식 프로세스의 환경변수 `DEVICE_KEYS` 로만** 넘긴다(출력·파일·명령줄에 없음; k6 는 프로세스 환경변수를 읽는다 [시험]). 회차마다 장비당 감사 로그 1줄이 늘고 이전 키는 무효가 된다.
5. **사후 확인**: `preflight` 다시(세 장비 ACTIVE·같은 주차장), `p2_protect.py verify --allow-new machines,audit_logs --exclude …` 로 기존 행 불변, `p2_collect.py runs` 로 작업 없음 확인.
**기존 데이터에 미치는 영향**: `machines` +3, `audit_logs` +3(+키 회전마다 +장비 수). 위반·세션·작업·알림 변화 없음. 주차장 6 의 `machine_count` 가 4 가 된다(기존 E2E 장비 1 + 신규 3).
**결정됨(A안)**: 새 장비 3대(`RPI-LOAD-01..03`)만 부하·스모크에 쓰고 `RPI-E2E-TEST-01` 과 위반 #5 는 변경하지 않는다. 이유: 활성 사건(`ended_at IS NULL`)이 있는 장비는 마지막 신호가 10분 넘게 지났으면 **첫 사진 하나**로 사건이 `SIGNAL_LOST` 로 종료되고 이력이 추가된다 [시험: 실제 백엔드 코드]. 기존 데이터 보호 검증(`p2_protect.py`)이 `violations`·`violation_history` 불변을 확인한다.

## 3. 사진 10장 스모크 실행 계획 (미실행)
**목적**: 도구와 서버 경로 확인(성능 측정이 아니다). 입력: `sample_a`(허가 판정) 10장, 장비 3대에 4/3/3, 1초 간격 [시험: `STEPS=smoke10:1:10` → 장비별 전송 시각 0,3,6,9 / 1,4,7 / 2,5,8]. 위반 사진 없음.
**이벤트 프로토콜 점검 [시험: 실제 백엔드 코드, `test_smoke_semantics.py`]**
- 장비마다 첫 사진 `arrived`(seq 1), 이후 `refresh`(seq 2,3,4) — **refresh 를 연속해서 받아도** 전부 접수·판정(`done`)되고 위반·알림·확인 명령·이력이 생기지 않는다. 첫 사진이 `refresh` 여도 접수된다.
- 새 세션은 이전 활성 세션을 대체하지만 이전 세션의 대기 작업도 그대로 판정된다. 워밍업 세션(seq 1)과 측정 세션(seq 1)이 같은 장비에서 이어져도 전부 반영된다.
- 활성 사건이 있는 장비는 사건이 바뀐다(2절). 그래서 스모크는 **활성 사건이 없는 새 장비**로 한다.
- **[미확인] AWS 에서의 실제 동작**: 위는 SQLite + 가짜 큐 + 가짜 추론이다. 실제 YOLO 의 판정(허가 사진이 `permitted` 로 나오는지)과 SQS FIFO 실동작은 스모크가 처음 확인한다.
**콜드 스타트 처리**: Worker 가 0개일 때 첫 사진은 콜드 스타트(약 25~45 s [관찰: E2E 2회])를 치러 10초 기준을 넘는다. 그래서 `WARMUP_PHOTOS=1 WARMUP_WAIT_S=75` 로 **설정 단계에서 장비마다 1장(총 3장)을 먼저 보내** Worker 를 띄우고 75초 기다린 뒤 측정 10장을 보낸다. 워밍업 3장의 접수→판정 지연은 `report.warmup`(콜드 스타트 관찰값)으로 따로 나오고 측정 통계에서는 빠진다.
**순서** (사용자가 자기 터미널에서 실행, 작업 디렉터리 `loadtest/`, 결과 폴더 `docs/measurements/phase2/results/<날짜>-smoke10-1/` 를 미리 만든다 — k6 가 만들지 않는다)
1. 사전 점검(읽기 전용): `python tools/p3.py status --expect p2-baseline`(공존 구성 그대로), `python tools/p2_watch.py preflight --queue-url … --dlq-url … --secret-id <RDS 비밀 ID> --window-h 2`(회전 일정·Pending·Argo·KEDA·빈 큐·1000m 자리 1), `python tools/p2_protect.py snapshot --out <폴더>/baseline.json --exclude "machines:<새 장비 id>"`.
2. 관찰자 시작(별도 터미널): `python tools/p2_watch.py run --out <폴더>/timeline.jsonl --queue-url … --dlq-url … --secret-id … --interval 15 --duration 780`. (Argo 자동 동기화는 켜진 공존 구성이므로 `--allow-outofsync` 없음. Worker 는 KEDA 자동이라 `--expect-workers` 없음.)
3. 실행: `python tools/p2_devices.py launch --base-url https://APP_HOST --lot 6 --serials RPI-LOAD-01,RPI-LOAD-02,RPI-LOAD-03 --yes-rotate -- k6 run --log-output=file=<폴더>/sends.log --log-format=raw -e BASE_URL=https://APP_HOST -e API_RATE=0 -e P2_SCENARIO=SMOKE -e STEPS=smoke10:1:10 -e WARMUP_PHOTOS=1 -e WARMUP_WAIT_S=75 -e RUN_ID=<날짜>-smoke10-1 -e OUT_DIR=<폴더> k6/scenarios_p2.js` (`API_RATE=0` 이라 관리자 로그인이 필요 없다).
4. 마지막 사진 뒤 약 60초: `python tools/p2_collect.py report --sends <폴더>/sends.log --source kubectl --out <폴더> --machines <ids> --drain-wait 60`, `python tools/p2_collect.py sqs --queue parking-guardian-judgement.fifo --start … --end … --out <폴더>/sqs-main.csv` (DLQ 도 같은 방식).
5. 쿨다운(300 s) 뒤 관찰자가 끝날 때까지 두고: `python tools/p2_smoke_check.py --report <폴더>/p2-report.json --attempts <폴더>/p2-attempts.csv --timeline <폴더>/timeline.jsonl --sqs <폴더>/sqs-main.csv --dlq <폴더>/sqs-dlq.csv --require-scale-down`, `python tools/p2_watch.py resources --out <폴더>/timeline.jsonl`.
6. 정상 상태 확인: `python tools/p3.py status --expect p2-baseline`(Worker 0, Argo 6개 Synced/Healthy, KEDA Ready), `python tools/p2_protect.py verify --baseline <폴더>/baseline.json --allow-new audit_logs`(기존 행 불변; 감사 +3 은 키 회전), `python tools/p2_collect.py runs --source kubectl --since <시작 시각>`(남은 미완료 0).
**기대값과 통과 기준** (`p2_smoke_check.py` 가 판정, 자료가 없으면 SKIP = 통과 아님)
| 항목 | 기대 |
|---|---|
| 접수 | 측정 10건 모두 202, 재시도 0, `event_id` 유일, 장비별 seq 가 전송 순서대로 1,2,3,(4) |
| 판정 | 10건 모두 `done`(stale 0), 접수→판정 ≤ 10 s(Worker 가 이미 떠 있으므로), 미완료·처리 실패 0 |
| 워밍업 | 3건도 `done`; 지연 = 콜드 스타트 관찰값(이미지 캐시가 있으면 약 25 s, 없으면 약 45 s [관찰]) |
| 큐 | 본 큐 0으로 소진, DLQ 0 전 기간 |
| Worker | 0 → 1(이상) → 쿨다운 후 0 (타임라인 `worker_replicas` 변화) |
| 기존 데이터 | `p2_protect verify` 통과(감사 로그 새 행만 허용) |
| 새 데이터량 | `detection_jobs` +13(측정 10 + 워밍업 3), `detection_results` +13, `device_sessions` +6(워밍업 3 + 측정 3), `audit_logs` +3(키 회전), S3 `inbox/` +13 장(약 4.8 MB) [계산] |
**중단 기준**: 관찰자의 ABORT(Worker 정지, KEDA 확장 실패, DLQ, Pending, 재시작, 회전, Argo 이상 — [P2_TOOLS.md 7절](P2_TOOLS.md)), k6 임계값 실패(`dropped_slots`, 4xx, 연결 실패), 202 가 아닌 응답 1건이라도. 중단하면 미완료 작업은 지우지 않고 `runs` 로 현황만 기록한다.

## 4. 시험 일정과 추가 노드 없이의 공정성
**일정**: 2회 분리. 1회차 = 선행 준비 + Phase 1 재측정(공존 구성, `p3.py pause`), 2회차 = 비교 구성 + Phase 2 측정(`p2-pause`, `p2-workers N`) + (선택) 공존 복귀 후 Phase 1 S1 한 번(같은 날 드리프트 대조). 장애 주입(P2-F)은 이번 정상 성능 비교에서 제외한다. RDS 스냅샷은 비용·보존 필요를 분석한 뒤 별도 결정한다(여기서는 가정하지 않는다).
**추가 노드(3대 그대로) 조건에서 공정한 비교가 불가능한 것**
| 비교 | 가능 여부 | 이유 |
|---|---|---|
| Phase 1 전체 경로 S1 (inference 2) ↔ Phase 2 S1 (Worker 2 고정) | **가능** | AI 예약 CPU 각 2000m(1절), 같은 사진·장비·도착률·PC 경로. 비교 구성에서 inference 를 제거하므로 두 구성을 같은 시각에 돌릴 수는 없다(순차 비교) |
| 공존 구성에서 두 경로를 **동시에** 부하 | **불가능** | 1000m 자리가 노드 하나뿐이라 Worker 는 1개만 들어가고 inference 롤링·3번째 inference 도 막힌다(R7). 간섭 효과는 측정하지 못한다 |
| Worker 3 ↔ inference 3 (전체 경로) | 사실상 **불가능**(권하지 않음) | Phase 1 의 `p3.py pause` 는 HPA 2/2 고정만 한다. inference 3 은 HPA 가 CPU 로 정할 때만 생기고 고정 절차가 없다. Worker 3 의 결과는 "Phase 2 확장성"으로만 보고하고 Phase 1 과 짝짓지 않는다 |
| N=1 짝 (inference 1 ↔ Worker 1) | 전체 경로로는 불가능(HPA 고정 절차 없음) | inference 단독 E0(닫힌 루프)와 Worker 1 포화(W)로 간접 비교만 가능 |
| 확장 반응(H4: HPA 대 KEDA) | **제한적** | Phase 1 은 HPA 를 2 로 고정해 측정하므로 확장하지 않는다. HPA 켠 S4 별도 회차가 필요하고, Phase 2 K 와 입력이 다르다 |
| 장비 직렬화 한계(1.5C) | 비교는 가능하나 **해석 주의** | 장비 3대면 Worker 를 늘려도 장비당 처리 상한(약 1.4 장/초 [추정])이 있어 Phase 2 만 이 한계를 가진다. Phase 1 은 장비와 무관하게 병렬(공정성에 영향) — 결과에 한계를 명시 |
| 같은 날 환경 | 부분적 | 두 회차 사이 RDS 크레딧·이미지 캐시·노드 상태가 달라진다. 회차마다 RDS `CPUCreditBalance`·노드 CPU 를 기록하고 드리프트 대조(Phase 1 S1 한 번 더)로 편차를 본다 |
| 실효 CPU 동등성 | **검증 필요** | 스레드 고정 방식이 다르다(Worker 만 `torch.set_num_threads` 단독, 1절 [미확인]). W 시험의 Worker CPU 표본으로 확인한다 |

## 5. 실제 AWS 스모크 전에 필요한 승인 (각각 별도)
1. (결정 완료) 장비는 A안: 새 장비 3대. `RPI-E2E-TEST-01`·위반 #5 불변.
2. (진행 중) 도구 변경의 Git 반영 — 앱 리포 `loadtest/` 코드·문서는 커밋 대상, 결과 원본은 제외.
3. 장비 등록 `p2_devices.py create --serials RPI-LOAD-01,RPI-LOAD-02,RPI-LOAD-03 --apply`(SUPER_ADMIN 로그인은 사용자가 직접).
4. 스모크 실행 `launch --yes-rotate`(장비 키 회전 + 사진 13장 접수 + SQS 발행 + KEDA 확장) 와 읽기 전용 관찰.
5. (스모크 통과 후) Phase 1 재측정 구성 고정(`p3.py pause --apply`), 이어서 본 시험 승인.


## 6. 스모크 실행 결과 (2026-11-04, 실제 AWS 실행 완료 — 사진 13장)
RUN_ID `20261104-smoke10-1`. 장비 `RPI-LOAD-01..03`(id 7·8·9, 회사 3·주차장 6)만 사용, 워밍업 3장 + 측정 10장. 결과 원본은 `docs/measurements/phase2/results/20261104-smoke10-1/`(Git 제외). 시각은 UTC. **10장으로 잰 값은 처리량 한계나 성능 보장이 아니다.** 아래 표현은 이 한 번의 실행에서 관찰된 것만 말한다.

### 6.1 기능 검증 — 성공 [관찰]
| 항목 | 결과 |
|---|---|
| 전송 수 | 워밍업 3 + 측정 10 = 총 13장(초과 없음). 서버 `detection_jobs` +13, 결과 +13, 세션 +6, S3 `inbox/` +13 |
| 접수 | 측정 10건 모두 HTTP 202. 재시도·중복·4xx·5xx·연결 실패 0, 건너뛴 슬롯 0 |
| 판정 | 워밍업 3/3, 측정 10/10 `done`. `stale`·`rejected`·`dead_lettered` 0 |
| 장비별 순서 | 장비마다 seq 1,2,3(,4) 순서대로 접수·반영(`last_applied_seq` 4·3·3). `event_id` 전부 유일 |
| Relay→SQS→Worker | 접수→발행 p50 0.11 s. 발행→판정 p50 0.78 s(추론 p50 0.71 s). 판정은 Worker 1개가 처리 |
| 큐·DLQ | 본 큐와 DLQ 최종 0, DLQ 전 기간 0 |
| KEDA/Worker | 0 → 1 → 0 (축소 결정 10:51:01). ScaledObject Ready=True 유지, Argo 6개 Synced/Healthy |
| 기존 데이터 | 위반 #5·알림 #9·#10 불변, `RPI-E2E-TEST-01`(id 6) 불변, 다른 회사 데이터 불변. 증가는 `audit_logs` +3(키 회전)뿐 |
| 관찰자 | 경고·ABORT 0, 표본 오류 0 |
판정 도구 `p2_smoke_check.py`: 14개 항목 전부 PASS.

### 6.2 워밍 상태 응답 지연 — 측정 10건 [관찰, n = 10]
접수→판정 지연: min 0.854 s, **p50 0.889 s, p95 1.038 s**, max 1.042 s. 10초 이내 판정 완료율 100 %(10/10).
- 표본 10개이므로 p95·p99 는 사실상 최댓값 근처의 값이다(`p99_reliable = false`). 도착률은 1장/초(장비 3대 합)였고 Worker 대기열은 쌓이지 않았다. **부하가 높을 때의 지연은 이 실험이 말해 주지 않는다.**
- 202 응답 시간(k6): 평균 165 ms, 최대 309 ms(판정 지연과 다른 의미).

### 6.3 콜드 스타트 지연 — 워밍업 3건 [관찰, 콜드 스타트 사건 1회]
접수→판정 지연 30.24 ~ 31.26 s(p50 30.7 s). 같은 콜드 스타트에 걸린 세 장이라 **독립된 표본 3개가 아니라 사건 1회의 관찰**이다. 10초 기준은 이 구간에서 충족하지 못한다(`minReplicaCount 0` 구성에서 유휴 뒤 첫 사진의 비용).
구성(대략): 접수 10:45:32 → KEDA 확장 결정 10:45:46(약 14 s, 15 s 폴링의 우연) → 컨테이너 시작 약 10:45:52 → 모델 로딩·워밍업 약 9 s [추정: 이전 E2E 의 값 + 이번 총합에서의 차] → 첫 판정 약 10:46:03. 이미지 캐시가 있는 상태였다. 이전 E2E 의 24.7 s(KEDA 반응 7.5 s)와 비교되지만 같은 조건이 아니다.

### 6.4 관측 결과의 표본 수와 해상도
- 관찰자 표본 13개(8.6분), 표본 간격 39~65 s(**설정 15 s 와 다름** → [P2_OBSERVABILITY.md](P2_OBSERVABILITY.md) 1절에서 원인 분석·개선). 타임라인 이벤트 시각의 오차는 표본 간격 정도다.
- `kubectl top` 표본: Worker 6개. Worker CPU 최대 1m, 메모리 최대 901Mi — **짧은 추론 CPU 버스트를 놓친 값**이라 CPU 사용량으로 해석하지 않는다.
- CloudWatch SQS 지표(1분): 12개 구간 모두 0. 약 30 s 지속한 3건 적체를 **포착하지 못했다**(1분 스냅샷 한계).
- k6: 측정 전송 10건(P2SEND). DB: 작업·결과 각 13행(워밍업 포함).

### 6.5 미확인
- 10장은 처리량·지연 분포·p99 를 말하지 못한다. 동시 접수가 늘 때의 FIFO·Worker 확장 동작은 보지 못했다.
- 실제 CPU 사용량(위 6.4), 모델 로딩 완료 시각의 직접 관측, Worker 가 2개 이상일 때의 동작.
- 쿨다운 동작의 원인(아래 가설), 이미지 캐시가 없는 새 노드에서의 콜드 스타트.
- 관찰자가 `kubectl exec`(DB 읽기)로 backend Pod 를 흔든 정도(스모크의 backend CPU 최대 223m 가 이것 때문인지).
- 실제 YOLO 가 허가 사진을 `permitted` 로 판정한 것은 확인했다(위반·알림·확인 명령 0) - 다른 사진에서의 정확도는 다루지 않았다.

### 6.6 본 부하 테스트에서 검증할 가설 (스모크는 근거를 주었을 뿐 확정하지 않았다)
| 번호 | 가설 | 근거(스모크 1회) | 검증 방법 |
|---|---|---|---|
| H-K1 | KEDA 쿨다운은 마지막 작업 완료가 아니라 KEDA 가 마지막으로 활동을 **폴링으로 본** 시각부터 센다 | 마지막 측정 판정 후 243 s 에 축소(설정 300 s) | P2-C/P2-K: 간격을 달리한 단발 사진, `Active` 전환·KEDA 로그·축소 시각 비교 |
| H-K2 | 드문 사진(처리 < 폴링 간격)에서는 사진이 오는 중에도 0 으로 내려가 매번 콜드 스타트를 치른다 | 측정 10장이 폴링에 잡히지 않은 정황 | 위와 같음 |
| H-K3 | 0→1 반응은 폴링 주기 안의 분포다(1~15 s) | 7.5 s, 1 s, 약 14 s | 콜드 스타트 ≥ 5회 반복 |
| H-C1 | 워밍 상태의 p95 는 낮은 도착률에서 1초 안팎이고, 장비 직렬화 한계(약 1.4 장/초/장비 [추정]) 부근에서 대기열이 쌓인다 | n=10, 도착률 1 장/초 | S1 0.2C~1.5C 단계, 대기열 에피소드·지연 분포 |
| H-C2 | Worker 의 실효 CPU 가 requests(1000m)를 크게 넘지 않는다(공정 비교 전제) | 스모크 CPU 값은 신뢰 불가 | W 시험에서 cAdvisor 누적 카운터로 평균·구간 최대 |
| H-O1 | `kubectl exec` 의 DB 읽기가 backend CPU 를 흔든다 | backend 최대 223m > 요청 200m | `--db-period` 30 s vs 읽기 없음 비교(짧은 확인) |
