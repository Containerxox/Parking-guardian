# Phase 1 · Phase 2 비교 부하 테스트 도구 (2026-11-04 작성 — AWS 에서는 아직 실행하지 않았다)

코드와 문서는 Git 으로 관리한다. 측정 결과 원본, 임시 DB, 실행 로그, 백업 파일, 장비 키·비밀번호·토큰은 저장소에 올리지 않는다(`.gitignore`, 8절). 이 도구들로 Phase 1 코드를 바꾸지 않는다(`p3.py` 만 보완, 원본은 백업).
추가 문서: [P2_ANALYSIS_2026-11-04.md](P2_ANALYSIS_2026-11-04.md)(CPU 공정성·간헐 실패·데이터 증가량·Git 제외), [P2_SMOKE_PLAN.md](P2_SMOKE_PLAN.md)(장비 2대·사진 10장 스모크·일정·공정성).

아래 도구는 전부 **로컬 모의 서버와 가짜 클러스터**로만 시험했고,
AWS·클러스터·실제 DB·실제 장비 키에 접속하지 않았다. 표기: **[관찰]** 코드·기록에서 읽은 사실, **[시험]** 로컬 시험으로 확인, **[미확인]** 실제 AWS 에서만 확인 가능.

## 1. 기존 도구 분석 (수정하지 않았다 — p3.py 만 백업 후 보완)

| 도구 | 하는 일 | 이번 비교와의 관계 |
|---|---|---|
| `k6/lib.js`, `k6/scenarios.js` | 전체 경로(PC → ALB → api → inference). 시나리오 S0~S3, S5. 일반 API 5 req/s + 사진 `ramping-arrival-rate`. 지표 `api_ms/api_ok/photo_ok/photo_ms/photo_http_2xx|503|other`, 단계 태그 `step`, 워밍업 `phase:warmup` | **Phase 1 측정에 그대로 쓴다.** 수정하지 않았다(해시 불변, 회귀 시험 통과) |
| `k6/inference.js` | inference 단독(E0~E3, T1). 클러스터 안 k6 Job. 닫힌 루프(`VUS`) 또는 도착률 | **B0(C 재산정)에 그대로 쓴다** |
| `tools/p3.py` | Argo 자동 동기화 중단 + HPA min=max=2 고정, 백업·복구·검증(Phase 1 용) | **보완**: Application 6개, Phase 2 비교 구성(아래 6절). 원본은 `p3.py.bak-before-p2` |
| `tools/e3_run.py`, `s0_run.py`, `s1_run.py`, `s5_guard.py` … | 회차 실행·기록·가드(Phase 1) | 건드리지 않았다. 회차 폴더·무효 규칙의 기준으로 참고 |

**Phase 1 측정 지표 [관찰]**: 일반 API p95·성공률(`api_ms`, `api_ok`), 사진 성공률/응답시간(`photo_ok`, `photo_ms`), 상태별 개수(`photo_http_*`), 서버 지표(`pg_ai_client_requests_total{outcome}` 등, Prometheus).
**장비 인증 [관찰]**: 요청 헤더 `X-Device-Key`. Phase 1 은 데모 장비 2·4·5 의 키를 `flask device-key <SERIAL>` 로 유도해 `-e DEVICE_KEYS` 로 넘겼다. 새 시험 장비는 SUPER_ADMIN 의 `rotate-key` 로 키를 받는다([test-device-procedure.md](../../parking-guardian-config/docs/test-device-procedure.md)). 키는 파일·출력·메모리 문서에 적지 않는다.
**기존 C = 3.107 장/초의 조건 [관찰: Phase 1 최종 보고서]**: inference 2 Pod, 클러스터 안 k6(E3), 동시 VUS = Pod × 2 = 4, 빌드 `c14a7f0`, Argo 자동 동기화 중단 + HPA 2 고정, `sample_a`, 3회, "성공 처리량"(busy 503 15.6~19.8 % 를 뺀 값).

**기존 결과와 새 실험 조건이 다른 부분** (그래서 기존 값을 재사용하지 않는다)
| 항목 | 기존 Phase 1 | 새 실험 |
|---|---|---|
| 빌드 | `c14a7f0` | `60c2e43` (코드·애드온 변경: CloudWatch Agent 요청 100m, Relay, KEDA, ESO, Argo 6개) |
| 장비 | 데모 장비 2·4·5 (회사 1·2) | **시험 전용 장비 3대**(회사 3). 현재 1대(`RPI-E2E-TEST-01`), 2대 추가 필요 |
| 장비 키 | `flask device-key` 로 유도 | `rotate-key` 로 발급 |
| 경로 | `/device/detections`(동기, 200/503) | Phase 2 `/device/detection-jobs`(202, multipart 필드 `event`, `event_id`, `session_id`, `seq`), Phase 1 은 그대로 |
| 서버 쪽 쓰기 | 허가 판정: 장비 행만 | 사진마다 S3 `inbox/` PUT + 작업·결과 행 2개 + 세션 행 |
| 도착률 모델 | `ramping-arrival-rate`(요청이 서로 겹침) | 장비당 1 VU 직렬(실제 장비처럼 순차) — 다른 의미이므로 결과에 명시 |
| 공유 상태 | inference Pod 2개 | Worker 2개 고정(KEDA 일시정지) |
| 네트워크 | PC(Wi-Fi) → ALB 공개 주소 | 같은 PC → `https://APP_HOST`(ALB 는 내 IP `/32` 만 허용) |

**Phase 1·2 공통으로 쓰는 것**: `lib.js`(환경변수, 사진, 단계), 일반 API 부하(같은 요청 3종), 단계 이름·길이(S1 0.2C…1.5C × 180 s), 임계값(api p95 < 500 ms, api_ok > 99 %, `dropped_iterations == 0`), 지표 정의(`perf_metrics.py`).

## 2. 새로 만든 파일

| 파일 | 역할 |
|---|---|
| `k6/lib_p2.js` | 순수 함수: 구조 있는 UUID(run tag·장비·단계·카운터), 프로필(S0/S1/W/D/K/C/SMOKE/STEPS), 장비별 전송 시각, 응답 분류 |
| `k6/scenarios_p2.js` | Phase 2 부하. 장비당 scenario 1 + VU 1 + 반복 1 의 직렬 루프. 전송마다 `P2SEND` 로그, 시작 시 `P2META`/`P2START`, 종료 시 `summary.json` |
| `k6/test_lib_p2.js` | `lib_p2.js` 단위 시험(k6 로 실행, 36,883 검사) |
| `k6/order_probe.js` | 대조 실험: arrival-rate 방식이 장비 순서를 못 지킨다는 증거(모의 서버 전용) |
| `tools/perf_metrics.py` | **지표 정의(사전 등록 대상)** + 분위수·결과 분류·단계 요약 |
| `tools/p2_collect.py` | `report`: k6 로그 + DB(SELECT 전용) → 단계별 처리량·지연·성공률·실패·미완료·Worker별 건수·대기열 추이 / `sqs`: CloudWatch SQS 지표 |
| `tools/p1_points.py` | Phase 1 의 k6 `--out json` 점 → 같은 의미의 보고서, `compare`: Phase 1·2 단계별 나란히 |
| `tools/p2_watch.py` | 시험 관찰자(읽기 전용) + 중단 규칙 + 사전 점검 |
| `tools/p2_mock_server.py` | 로컬 모의 서버(시험 전용, 127.0.0.1) |
| `tools/test_perf_tools.py`, `test_p2_integration.py`, `test_guard.py`, `conftest.py` | 단위·통합 시험, 클라우드 명령 차단 가드 |
| `tools/p3.py` (보완) | Phase 2 비교 구성 명령 추가, Application 개수 일반화 |

## 3. 비교 지표의 정의 (실행 전에 고정 — 바꾸면 변경 기록을 남긴다)
자세한 규칙은 `tools/perf_metrics.py` 머리말. 요약:
- **시도(attempt)** = 장비가 사진을 보내려 한 것(재시도는 같은 `event_id` 로 한 시도). 결과는 정확히 하나: `on_time`(판정 완료, 접수→판정 ≤ 10 s) / `late`(> 10 s) / `request_failed`(접수 거절·오류·시간 초과) / `processing_failed`(접수됐으나 rejected·dead_lettered) / `unfinished`(평가 시점까지 판정 없음).
- **10 초 이내 판정 완료율 = on_time / 시도 전체.** 분모에서 **아무것도 빼지 않는다**: 실패한 사진과 평가 시점까지 끝나지 않은 사진은 모두 on_time 이 아니다. 평가 시점은 마지막 전송 + `--drain-wait`(기본 120 s) 이후여야 하고, 더 이르면 `eval_too_early=true`(unfinished 가 부풀려짐). 접수된 지 10 s 도 안 된 unfinished 는 `censored_young` 으로 따로 센다(여전히 on_time 아님).
- **지연**: Phase 1 = 요청→200 응답, Phase 2 = 서버 접수(`received_at`)→판정 저장(`detection_results.created_at`). Phase 2 의 202 응답 시간(`p2_accept_ms`)은 이 값과 **다른 의미**라 따로 보고. 분위수는 선형 보간, 판정이 끝난 것만으로 계산(반드시 completion_rate 와 같이 읽는다). **p99 는 표본 300 미만이면 `p99_reliable=false`**.
- **처리량(장/초)** = 단계 창 안에 **판정이 완료된** 사진 수 / 창 길이(완료 시각 기준). 지연은 보낸 시각이 속한 단계 기준(코호트). 두 기준은 다르다.
- 요청 실패율 = request_failed / 시도, 처리 실패율 = processing_failed / (시도 − request_failed)(Phase 1 은 해당 없음 0). `stale` 은 판정이 있으므로 완료로 센다(`stale_judged` 를 따로 보고).
- 워밍업 사진(id 종류 `b`)은 시도·통계에서 빼고 `warmup_jobs_excluded` 로만 센다.
- 단계 창의 기준 시각은 k6 시나리오의 **실제 시작 시각**(`P2START`)이다(`setup()` 이 끝난 시각이 아니다 — 시험 중 한 번 k6 가 시나리오를 늦게 시작해 슬롯이 버려진 일이 있어 바꿨다).
- CPU·메모리, 큐 대기·처리 중 메시지 수, KEDA 확장 시각, 일반 API 지연: `p2_watch.py` 타임라인(`kubectl top` 은 별도 표본), `p2_collect.py sqs`, `scale_events()`, k6 `api_ms`.

## 4. k6 Phase 2 스크립트
**장비별 직렬화**: 장비 하나 = `scenario dev<N>`(executor `per-vu-iterations`, `vus: 1`, `iterations: 1`) 안의 `for` 루프. 한 VU 는 `http.post` 가 돌아와야 다음 요청을 보내므로 같은 장비의 요청이 겹칠 수 없고, `seq`·`event_id` 카운터는 그 VU 의 지역 변수다.
**arrival-rate 실행기는 쓰지 않는다**: 반복을 아무 VU 에나 배정해 응답을 기다리지 않고 겹친다 — `order_probe.js` 로 확인했다 [시험].
시험 결과: 장비당 최대 동시 요청 1, 서버 도착 seq 가 1,2,3… 그대로, 장비들끼리는 동시(전체 직렬 아님); 순진한 방식은 지연 편차 600 ms 에서 장비 안 동시 요청 >1 과 seq 역전 발생.

필수 환경변수: `BASE_URL`, `DEVICE_KEYS`(쉼표), `P2_SCENARIO`, `C`(S1/W/D/K), `RUN_ID`, `OUT_DIR`(**미리 만들어 둘 것** — k6 가 디렉터리를 만들지 않는다). 일반 API 부하(`API_RATE`>0, 기본 5)에는 `ADMIN_EMAIL`, `ADMIN_PASSWORD`. 선택: `SCALE`, `STEPS`, `W_RATE`, `W_SECONDS`, `BURST`, `RETRY`(기본 0), `EVENT_MODE`, `WARMUP_PHOTOS`, `WARMUP_WAIT_S`, `LATE_TOL_MS`(1000), `REQ_TIMEOUT`(30s), `ABORT_ON_ERRORS=1`. 위반 사진은 `ALLOW_VIOLATIONS=1` + `VIOLATION_RATIO` 가 모두 있어야만 보낸다(기본 0).
실행 형식(키는 환경변수에서 읽게 하고 출력하지 않는다):
```bash
mkdir -p results/<날짜>-p2-s1-1
k6 run --log-output=file=results/<날짜>-p2-s1-1/sends.log --log-format=raw \
  -e BASE_URL=https://APP_HOST -e DEVICE_KEYS="$DEVICE_KEYS" -e ADMIN_EMAIL="$ADMIN_EMAIL" -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -e P2_SCENARIO=S1 -e C=<재측정한 C> -e RUN_ID=<날짜>-p2-s1-1 -e OUT_DIR=results/<날짜>-p2-s1-1 k6/scenarios_p2.js
```
무효 규칙(임계값): `p2_dropped_slots == 0`(목표 도착률을 못 지킨 슬롯), `p2_other == 0`(연결 실패·시간 초과), `p2_http4xx == 0`(스크립트·설정 문제), `dropped_iterations == 0`, `api_ms p95 < 500`, `api_ok > 99 %`. 5xx 는 기록하고 계속한다(`ABORT_ON_ERRORS=1` 이면 50개 초과 시 중단).
재시도: 기본 없음(부하를 바꾸지 않는다). `RETRY=N` 이면 5xx·연결 실패에만, **같은 event_id·seq** 로, 지수 백오프로 재전송한다. 서버가 이미 저장했다면 200 `duplicate` 로 답하고 `p2_duplicate` 로 센다 [시험: 응답 유실 주입].

## 5. 수집·비교 도구
```bash
python tools/p2_collect.py report --sends <dir>/sends.log --source kubectl --out <dir> --machines 7,8,9 --drain-wait 120
python tools/p2_collect.py sqs --queue parking-guardian-judgement.fifo --start <ISO> --end <ISO> --out <dir>/sqs-main.csv   # DLQ 도 같은 방식
python tools/p1_points.py report --points <p1 dir>/points.json --out <p1 dir>            # Phase 1 (k6 --out json=points.json)
python tools/p1_points.py compare --p1 <p1 dir>/p1-report.json --p2 <dir>/p2-report.json --out compare.csv
```
- DB 는 **SELECT 만**: `assert_select_only` 가 모든 문장을 검사(INSERT/UPDATE/DELETE/DDL/`;` 다중 문장/`FOR UPDATE` 거부), `kubectl` 출처는 고정 스크립트의 SHA-256 을 먼저 출력하고 `kubectl exec … python -` 로 backend Pod 안에서 읽기만 하며 끝에 rollback. SQLite(시험용)는 `mode=ro` 로 연다 [시험].
- 출력: `p2-report.json`(정의·단계·전체·작업 상태별 건수·Worker별 건수·단계별 시간·메모), `p2-steps.csv`, `p2-attempts.csv`(시도별 원자료), `p2-backlog.csv`(5 s 간격 시스템 안 작업 수), `p2-workers.csv`.
- **Worker 처리 시작 시각은 DB 에 없다**(claim 때의 `updated_at`·`lease_until` 은 완료 때 덮어쓰이거나 지워진다). 근사 = 판정 완료 − `elapsed_ms`(추론 시간만) — "근사"로 표시. 접수→발행(`published_at`), 발행→판정, 추론 시간, 대기+오버헤드(근사)를 따로 낸다.
- 비밀 방지: 도구는 `DEVICE_KEYS`·비밀번호를 읽지 않는다. 쓰기 직전에 `--forbid-env`(기본 `DEVICE_KEYS,ADMIN_PASSWORD`)의 값이 출력에 있는지 검사해 있으면 파일을 쓰지 않고 중단 [시험].

## 6. p3.py 보완 (Phase 1 복구 도구) — 실제 클러스터에서는 실행하지 않았다
- 원본: `p3.py.bak-before-p2`(내용 불변). 기존 명령·옵션은 그대로 동작한다.
- **Application 개수**: 하드코딩 5개 → 백업에 있는 것(없으면 지금 있는 것). 지금은 `keda` 가 있어 6개. 백업의 앱이 없으면 FAIL, 백업에 없던 앱은 INFO. 옛 5개짜리 백업이 6개 클러스터에서도 복구 검증을 통과한다 [시험].
- 필수 객체(inference HPA/Deployment 등)가 없으면 예외 대신 FAIL (비교 구성에서 Phase 1 명령을 쓴 경우) [시험].
- `snapshot` 이 `deploy-worker/relay`, `scaledobject-worker`, `hpa-keda-hpa-worker`(있으면)도 저장.
- 새 명령 (모두 `--apply` 가 없으면 아무것도 바꾸지 않는다 [시험]):
  - `status --expect p2-baseline|p2-comparison|p2-fixed [--workers N]`
  - `p2-pause --apply`: 비교 구성(Git 반영 완료, 자동 동기화 켜짐) 확인 → 백업 → root·parking-guardian 자동 동기화 끄기 + backend HPA 2/2 → `p2-fixed` 확인
  - `p2-workers N --apply`(N ≤ 3) / `--release`: ScaledObject 의 `paused-replicas=N` 으로 Worker 수 고정. **자동 동기화가 켜져 있으면 거부**
  - `p2-restore --target comparison|baseline --apply`: 어노테이션 제거 → `root.yaml` 재적용(자동 동기화·HPA 는 Git 대로) → 목표 상태 확인(6분 제한, 실패하면 보고만)
- **복구 순서** (비교 구성 시험 뒤): `p2-workers --release` → `p2-restore --target comparison` → (사용자가 Git 의 비교 구성 패치를 되돌려 푸시) → inference 1000m 자리가 필요하므로 **Worker 0·큐 빈 뒤에** → `p2-restore --target baseline`.
- **Worker·Inference replica 와 KEDA 충돌**: worker Deployment 의 replicas 는 KEDA(HPA `keda-hpa-worker`)가 정한다. `kubectl scale` 은 되돌려진다. Application 은 Deployment/worker 의 `/spec/replicas` 를 무시(ignoreDifferences)하므로 Argo 는 KEDA 의 결정을 되돌리지 않는다 [관찰: 11-04]. `paused-replicas` 는 Git 에 없는 값이라 자동 동기화가 켜져 있으면 되돌려질 수 있다 [미확인] → 항상 자동 동기화를 끈 뒤에만 쓴다. 비교 구성에는 inference HPA 가 없다(HPA 패치 대상도 없다).
- 이 도구는 Git 을 바꾸지 않는다(비교 구성 패치의 적용·되돌림은 사용자의 config 커밋).

## 7. 운영 위험 확인과 중단 기준 (R3 · R6 · R7 은 해결된 것이 아니다)
`tools/p2_watch.py preflight` 는 시험 **전에 한 번**, `run` 은 시험 **중** 15 초마다 읽기 전용으로 확인한다. 쓰기 명령과 비밀을 읽는 명령은 실행기에서 거부된다(허용 목록: `kubectl get pods|scaledobject|app|nodes|hpa|deploy|events`, 고정 SELECT 스크립트 `kubectl exec`, `aws sqs get-queue-attributes`, `aws cloudwatch get-metric-statistics|get-metric-data`, `aws secretsmanager describe-secret`) [시험: 15개 쓰기·비밀 명령 거부].
| 위험 | 확인 방법 | 사전 점검 | 시험 중 규칙 (ABORT = 시험 중단·기록) |
|---|---|---|---|
| RDS 비밀번호 회전(R6) | `describe-secret` 의 `LastRotatedDate`·`NextRotationDate` | 다음 회전이 시험 창(`--window-h`) 안이면 문제 | 마지막 회전 시각이 시작 때와 다르면 **ABORT** (`RDS_ROTATED`) |
| SQS 오래된 메시지 | CloudWatch `ApproximateAgeOfOldestMessage` | 큐·DLQ 비어 있어야 함 | ≥ 120 s **WARN**(과부하 단계에서는 정상), ≥ 900 s **ABORT**. **큐 길이/나이만으로는 중단하지 않는다**(1.5C 에서 쌓이는 것이 시나리오) |
| Worker 정지·처리 정체(R3) | DB: 대기 작업 수, 마지막 결과 시각 | — | Worker 가 Ready 인데 대기 작업이 있고 새 판정이 **60 s** 없음 → **ABORT** (`WORKER_STALL`) |
| KEDA 확장 실패 | `kubectl get scaledobject`, Worker Pod 수 | ScaledObject Ready=True | 일이 있는데 Worker 0개가 **90 s** 지속 → **ABORT**; Ready≠True → **ABORT**; `paused-replicas` ≠ 기대 N → WARN |
| DLQ 메시지 | `get-queue-attributes`(DLQ) | DLQ 0 | 1건이라도 → **ABORT** (`DLQ_MESSAGE`) |
| CPU 부족 Pending(R7) | Pod phase, 노드 allocatable − requests | 1000m 자리 수 출력(공존 1, 비교 구성 3 이어야 함) | Pending Pod 가 **60 s** 지속 → **ABORT** (`POD_PENDING`) |
| Argo OutOfSync/Degraded | `kubectl get app` | 6개 Synced/Healthy | Degraded → **ABORT**, OutOfSync → **ABORT**(허용 목록: Phase 1 고정 구성의 `parking-guardian`) |
| Relay 정지 | `pending_publish` 의 나이 | — | 30 s → **ABORT** (`RELAY_STALL`) |
| 재시작 | Pod 재시작 수 | — | backend·relay·worker·inference·frontend 재시작 증가 → **ABORT** |
| 관찰자 자신의 실패 | 표본 수집 오류 | — | 연속 3회 → **ABORT** (`SOURCE_ERROR`) |
이 도구는 k6·클러스터를 멈추지 않는다. ABORT 시 큰 배너와 `ABORT.flag` 를 남기고 종료 코드 3 — 사람이 k6 를 Ctrl+C 로 멈추고 시각·상태를 기록한다. 이 규칙은 시험 전에 사전 등록한다(임계 초는 `p2_watch.Config` 기본값).
무효로 처리: ABORT 가 난 회차, k6 임계값 실패(`dropped_slots`, `other`, `4xx`), 단계 중 Pod 재시작·Argo 이상. 무효 회차도 폴더에 남긴다.

## 8. 테스트 데이터·결과 보존 계획 (DB 삭제·초기화를 전제로 하지 않는다)
- **장비 3대 신규 등록(A안, 미실행, 별도 승인)**: 회사 3·주차장 6 에 SUPER_ADMIN 공식 API 로 `RPI-LOAD-01`, `RPI-LOAD-02`, `RPI-LOAD-03` 등록. 기존 `RPI-E2E-TEST-01` 과 위반 #5 는 부하·스모크에 쓰지 않는다(활성 사건이라 첫 사진에 바뀐다). 순서: 사전 점검(`test-device-preflight.py` 방식, 읽기 전용) → 등록 → 사용 직전 `rotate-key`. 키는 사용자 터미널의 환경변수 `DEVICE_KEYS`(쉼표)로만 쓰고 파일·로그·메모리에 남기지 않는다. 시험이 끝나면 키 유지/회전은 사용자 결정.
- **결과 저장 위치**: `docs/measurements/phase2/results/<날짜>-<시나리오>-<n>/`(`.gitignore` 로 제외돼 저장소에 올라가지 않는다). 회차마다 `sends.log`, `summary.json`, `p2-*.json/csv`, `timeline.jsonl`, `sqs-*.csv`, `conditions.md`, `SHA256SUMS`. Phase 1 도 같은 방식으로 `points.json`, `p1-report.json`.
- **재측정 구분**: 사진마다 `event_id` 의 앞 8자리가 **run tag**(`RUN_ID`+시작시각 해시, 또는 `RUN_TAG` 로 지정)이고 단계 번호도 id 에 들어 있다. 같은 장비·같은 DB 에서 여러 번 재측정해도 수집기가 run tag 로 이번 회차의 행만 고른다. `RUN_ID` 에 날짜·시나리오·번호를 넣는다.
- **데이터량 [계산]**: 사진 1장 = `sample_a` 369,857 B(S3 `inbox/`, 7일 뒤 자동 삭제), DB 작업 1행 + 결과 1행. S1 1회 = 5.2 × C × 180 s ≈ **2,908 장**(C = 3.107 기준) ≈ S3 1.08 GB, DB 약 5,800 행. Phase 2 S1 × 3 이면 약 8,700 장·17,000 행·3.2 GB. 포화(W)·콜드(C) 등을 더하면 2~3배. 세션 행은 회차당 장비 수(3). 위반 사진은 보내지 않는다(위반·알림·증거 사진 없음).
- **중단 시 미완료 작업**: 삭제하지 않는다. 남은 `pending_publish/queued/processing` 는 Relay·Worker 가 계속 처리하고(정상 흐름), 수집기는 평가 시점 기준으로 `unfinished` 로 센다. 정리가 필요하면 별도 승인 후에만(삭제 금지 규칙).
- **포트폴리오 활용**: 정의(3절)와 사전 등록 → 가설별 결과표(`compare.csv`) → 무효·결함·정정 기록(Phase 1 방식) → "측정으로 말할 수 있는 것/없는 것". 단일 실행 값은 일반 성능으로 쓰지 않고 3회 범위와 무효 회차를 같이 적는다. 실제 키·계정 ID·IP 는 공개 자료에 넣지 않는다. 측정 결과 원본은 저장소에 올리지 않으므로, 공개할 때는 `portfolio_summary.py` 의 요약 수치와 그래프만 가공해 쓴다.

## 9. 로컬 검증 결과 (2026-11-04)
- 단위 시험 90개(지표 정의, SELECT 가드, 보고서 계산, 파서, 관찰자 규칙 17종, 읽기 전용 실행기, p3 가짜 클러스터 15종) + 통합 시험 10개(k6 + 모의 서버) + k6 순수 함수 시험(36,883 검사). `conftest.py` 가 시험 중 `kubectl`·`aws`·`terraform`·`helm`·`docker`·`psql` 실행을 막는다 — 모두 통과.
- 실제 AWS·클러스터·RDS·S3·SQS 에 접속하지 않았고, 장비 키·비밀번호는 모두 시험용 가짜 값이다.
- **[미확인]** 실제 서버의 `refresh` 연속 전송(허가 판정이 세션/카운터에 미치는 영향), 실제 ALB 지연 아래의 도착률 유지, PC 업로드 대역폭(1.5C ≈ 14 Mbps), KEDA 로그 형식, 실제 Worker 이름(`worker_id`), `paused-replicas` 의 Argo 상호작용, Container Insights 지표 이름.
