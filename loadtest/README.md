# Phase 1 부하 테스트 실행 계획

기준 문서: `docs/measurements/phase1/README.md`(사전 등록, 가설·통과 기준·상한 선택 규칙), `docs/design/ARCHITECTURE_EVOLUTION.md` 3.8·4·5장. 이 폴더는 **어떻게 실행하는가**만 다룬다. 값과 기준은 사전 등록이 우선이다.

| 파일 | 역할 |
|---|---|
| `k6/lib.js` | 환경변수, 샘플 사진 읽기, 단계 계산 |
| `k6/inference.js` | inference 단독(E0~E3, T1). 클러스터 안 Job |
| `k6/scenarios.js` | 전체 경로(S0~S3, S5, T2/X1/P1 의 부하). 로컬 PC → ALB |

비밀은 파일에 없다. 계정·장비 키는 `-e` 로 넘긴다. 로컬 Docker Compose 에서 짧게 실행해 동작을 확인했다(8절). EKS 에서는 아직 실행하지 않았다.

## 1. 실행 순서와 한 회차의 절차

```text
사전 등록 "고정" 커밋 (E0 전에, 7절 결정 뒤) → (재구축 직후) 노드별 requests 합·Pod 배치 기록(V1)
  → E0 ×3 → E1 ×3 → E2 ×3 → E3 (Pod 1,2,3) ×3 → T1 → T3 계산 → 공식 구성 값 커밋
  → S0 ×3 → S1 ×3 (+ T2 후보 2/4/6, X1, P1) → S2 → S3 → S4(HPA 켬) → S5(a)(b)(c) → V2 → V3
```

회차마다 (사전 등록 7절, 폴더 `docs/measurements/phase1/results/<날짜>-<시나리오>-<번호>/`):

1. 시작 전 기록: 이미지 태그, 노드별 Pod 배치(`kubectl get pods -o wide`), CloudWatch 구성 요소, k6 버전, PC 사양·업로드 속도(전체 경로만), 시각(UTC).
2. 실행: `k6 run --summary-export=<폴더>/summary.json --out json=<폴더>/points.json ...`
3. 실행 중 사람이 하는 것: S5·V2·V3 의 조작 시각 기록(아래 3절). S1 이상은 `pg_stat_activity` 샘플링(4절).
4. 끝난 뒤: Prometheus 쿼리 CSV(5절), 대시보드 PNG, 위반 개수 증가분(6절).
5. 무효 판정: `dropped_iterations` > 0, k6 프로세스 CPU ≥ 70%, 부하가 오를 때 k6 p95 와 서버 p95 의 차이만 커짐 → 무효로 기록하고 다시 한다. 무효 회차도 폴더에 남긴다.

한 회차는 15분 이내(S3는 13분, 사전 정리 포함 15분 안). 회차 사이에 3분 이상 쉬고 시작 때 inference `ready` 를 확인한다.

## 2. 실행 예

### 2.1 inference 단독 (클러스터 안 Job — 스크립트가 같다)

```text
# E0: Pod 1, 동시 1, 3분. 3회 반복
k6 run -e INFERENCE_URL=http://inference:8000 -e RUN_ID=e0-1 -e DURATION=3m k6/inference.js

# E3: Pod N 개. 동시 VUS = Pod 수 × 2 (대기 상한 안에서 모델을 쉬지 않게)
k6 run -e VUS=6 ... k6/inference.js

# T1: 1.2 × C₁ 도착률로 INFERENCE_MAX_WAITING 후보 0, 1, 2, 4 (후보마다 Pod 재시작, 설정은 config 레포의 ConfigMap)
k6 run -e MODE=rate -e RATE=<1.2×C₁> -e MAX_VUS=40 ... k6/inference.js
```

- 처음 5건은 워밍업으로 따로 태그(`phase:warmup`)해 측정에서 뺀다.
- 판단은 클라이언트 `http_req_duration{phase:measure}` 와 서버 `pg_inference_*`(Prometheus)를 같이 본다. E0 에서는 Pod 안 `torch.get_num_threads()` 도 기록한다(사전 등록 E0).
- 클러스터 Job 으로 돌릴 때 사진 2장(705,943 B)을 ConfigMap `binaryData` 로 싣는다. base64 로 941,260 B, 한도 1 MiB(1,048,576 B)까지 **107,316 B(약 10%) 남는다.** 스크립트 파일(약 15 KB)을 같은 ConfigMap 에 넣어도 들어간다. **단 `kubectl apply`(클라이언트 측)는 내용을 주석에 한 번 더 복사해 한도를 넘기므로 `kubectl create` 또는 `--server-side`(Argo CD 포함)로 만든다.** 사진을 바꾸면 크기를 다시 계산한다.

### 2.2 전체 경로 (로컬 PC → ALB)

```text
k6 run -e SCENARIO=S0 -e BASE_URL=https://parking.<도메인> -e ADMIN_EMAIL=kim@a.com -e ADMIN_PASSWORD=... -e DEVICE_KEYS=<key2>,<key4>,<key5> -e RUN_ID=s0-1 k6/scenarios.js
k6 run -e SCENARIO=S1 -e C=<E3 에서 잰 C> ... k6/scenarios.js
```

- `SCENARIO`: S0, S1, S2, S3, S5. **S4 는 S1 프로필을 HPA 를 켠 상태로 실행**한다(스크립트는 같다). T2·X1·P1 도 S1 프로필이다.
- 장비 키는 AWS 에서 `flask device-key <SERIAL>` 로 확인한다(`SEED_DEVICE_KEY_SECRET` 에서 유도됨). 대상 장비 일련번호: `RPI-A-PANGYO-01`(machine 2), `RPI-B-HQ-01`(4), `RPI-B-LOGIS-01`(5). 값은 터미널에서만 쓰고 결과 파일·문서에 남기지 않는다. **실행 전 관리자 API 의 장비 목록으로 id 와 일련번호가 맞는지 확인한다.**
- 최소 k6 버전은 2.x 에서 확인했다(로컬 설치 v2.3.0). Phase 2 도 같은 버전을 쓴다.
- `ALLOW_VIOLATIONS=1` 과 `VIOLATION_RATIO` 를 같이 주지 않으면 위반 사진은 한 장도 보내지 않는다(스크립트가 시작 때 거부한다).

## 2.3 측정 지표 정의 (Phase 1 / Phase 2 비교용 — 고정)

| 지표 | 정의 | Phase 1 | Phase 2 에서는 |
|---|---|---|---|
| `api_ms`, `api_ok` | 관리자 API(목록·요약·알림) 응답 시간, 200 비율. 로그인·readyz 제외 | 그대로 | 그대로 (같은 스크립트) |
| `photo_ok` | 장비가 200/201 을 받은 비율 | "판정까지 끝난 응답" | **"접수 성공(202 등)" 이라 의미가 다르다.** 단독으로 비교하지 않는다 |
| `photo_ms` | 사진 요청 응답 시간 | 판정까지 포함한 시간 = End-to-End | 접수 시간. 비교는 **End-to-End 판별 지연**(접수 → 판정 DB 반영, DB 타임스탬프)으로 한다 |
| `photo_http_2xx / _503 / _other` | 상태별 개수 | `_other` 는 0 이어야 한다(아니면 무효) | 상태 코드 정의는 Phase 2 설계 후 맞춘다 |
| 503 의 원인 | **k6 로 구분 불가**(장비는 모두 `inference_unavailable`). 서버 `pg_ai_client_requests_total{outcome}`: `busy`, `timeout`, `bulkhead_full`(API 동시 상한), `unavailable`, `connection_lost`, `bad_response` | 서버 지표로 구분 | 유실·중복은 DB 로 센다 |
| `inference_busy / _unavailable / _failed` (inference.js) | busy = 대기 줄 가득 참(정상 동작), unavailable = 모델 로딩/실패, failed = 알 수 없는 응답 | `_failed` 0 이어야 한다 | — |

주의: 표본이 0 인 단계의 `photo_ok` 는 k6 요약에 `0.00%` 로 보인다. `0 out of 0` 이면 "측정 없음"이지 실패가 아니다.
워밍업은 `setup()` 에서 부하 시작 전에 보내고(요청은 `phase:warmup` 태그, 사용자 지표에는 기록하지 않는다) 기본 `http_req_duration` 에는 섞이므로 **`{phase:measure}` / `{kind:api}` 서브메트릭으로 읽는다.**

## 3. 주입·조작 시나리오 절차 (사람이 하는 부분)

시각은 `date -u +%T` 로 기록해 결과 폴더에 `events.txt` 로 남긴다.

| 시나리오 | 부하 | 조작 (S5: 시작 2분 / 5분에) | 확인 |
|---|---|---|---|
| S5(a) inference 0개 | `SCENARIO=S5` | 2:00 `kubectl -n parking-guardian scale deploy/inference --replicas=0`, 5:00 원복 | 일반 API p95·오류율 대 S0, 탐지 503 이 나오기까지 시간, 원복 뒤 첫 200 까지 |
| S5(b) 지연 주입 | 같음 | `INFERENCE_FAULT_DELAY_MS` 를 넣은 설정으로 배포(테스트 전용, config 레포 변경은 승인 대상) → 끝나면 되돌림 | 마감(10초) 안 503, 일반 API 영향 |
| S5(c) api Pod 삭제 | 같음 | 2:00 api Pod 1개 삭제 | 요청 손실 구간, 일반 API 오류, PDB |
| V2 | S1 중 1.0C 단계 | inference 있는 노드 1대 `cordon` + `drain` | 서비스 지속, 재배치·Ready 시간 |
| V3 | S1 중 1.0C 단계 | inference 2개를 한 노드에 모은 뒤 그 노드 drain | 탐지 중단 시간, 관리자 API 영향 |
| P1 (선택) | S1 | Argo CD 자동 sync 를 멈추고 api 2개를 inference 없는 노드에 고정 → 끝나면 복구 | 기본 배치(공존) 대비 |

## 4. 관찰 항목 수집 (사전 등록 5.3)

AI 대기 중 DB 연결 점유 — 실행 중 5초마다 (backend Pod 안에서 읽기 전용):

```text
SELECT now(), state, count(*) FROM pg_stat_activity WHERE datname = current_database() GROUP BY state;
```

`idle in transaction` 개수를 `pg_ai_client_in_flight` 와 같은 시각에 맞춰 본다. 쿼리 실행 방법(Pod 에서 psql 이 없으면 Flask 앱 컨텍스트로 실행)은 G8 의 연결 수 확인 명령과 같은 방식으로 한다.

## 5. Prometheus 쿼리 (CSV 로 저장할 것, 단계 구간별)

- 일반 API: `histogram_quantile(0.95, sum by (le,route) (rate(http_request_duration_seconds_bucket[1m])))`, 5xx 비율
- AI 클라이언트: `pg_ai_client_requests_total` outcome 별, `pg_ai_client_in_flight`, 호출 시간 분위수
- inference: `pg_inference_stage_seconds`(stage 별, decode 비중 포함), 락 대기, 거절 수, 모델 상태
- 자원: Pod 별 CPU·메모리, 노드 CPU(**steal** 포함), HPA 현재/목표 복제 수
- 지표 이름은 로컬에서 실제 `/metrics` 출력과 대조했다(2026-10-31). 확인된 이름: `http_request_duration_seconds`(라벨 `method`,`route`,`status`), `pg_ai_client_requests_total`, `pg_ai_client_duration_seconds`, `pg_ai_client_in_flight`, `pg_ai_client_retries_total`, `pg_inference_stage_seconds`, `pg_inference_lock_wait_seconds`, `pg_inference_rejected_total`, `pg_inference_requests_total`, `pg_inference_in_flight`/`_running`/`_waiting`, `pg_inference_model_state`, `pg_model_load_seconds`. **`pg_ai_client_*` 는 inference Pod 에도 이름만 있고 값은 없으므로 api 타깃(job/Pod)으로 한정해서 조회한다.** Prometheus 의 job·Pod 라벨 이름은 EKS 에서 확인한다.
- `pg_ai_client_requests_total` 의 outcome 값(로컬 관찰): `ok`, `busy`, `timeout`, `bulkhead_full` (코드에는 그 밖에 `connection_lost`, `bad_response`, `unavailable`, `invalid_image`).

## 6. DB 에 남는 것 (사전 등록 조건, 사용자 결정 2026-10-31)

- 기본은 **허가 판정 사진(sample_a) 100%, 위반 사진 0%.** 허가 판정은 위반·알림·S3 쓰기를 만들지 않는다. 쓰는 것은 장비 행(machine 2, 4, 5)의 마지막 신호·카운터뿐이다. 로컬에서 약 50장 + 부하 5종을 보낸 뒤 violations·notifications·violation_history 가 모두 0, machine 1·3 은 손대지 않음을 확인했다.
- 따라서 **기본 baseline 은 위반 확정 경로(DB 삽입, S3 저장, 알림)를 측정하지 않는다.** 그 경로는 테스트 전용 데이터와 별도 시나리오에서 `ALLOW_VIOLATIONS=1 -e VIOLATION_RATIO=...` 로 따로 검증한다(미정, 별도 결정).
- 기존 위반 1~4번: machine 1(위반 3)·machine 3(위반 4)에는 사진·Heartbeat 를 보내지 않는다. 스크립트는 `DEVICE_KEYS` 에 준 키로만 보내므로 machine 1·3 키를 주지 않으면 건드릴 수 없다. 회차 전후 확인: `SELECT id, status, detection_count, last_seen_at FROM violations ORDER BY id;` 가 같아야 한다.

## 7. 확정된 사전 등록 조건과 남은 결정

확정(사용자, 2026-10-31):

1. 장비: machine 2, 4, 5 의 3개. 추가 장비 불필요. (구성: 2 = A회사 판교, 4 = B회사 본사, 5 = B회사 물류센터.)
2. 위반 사진 비율 0%.
3. 관리자 계정: COMPANY_ADMIN. **권장 `kim@a.com`**(A회사, 강남·판교 담당, 시드 기준 위반 1~4번을 포함한 회사 전체 조회). `choi@b.com`(B회사)는 B회사에 위반이 없으면 거의 빈 목록이라 지연이 비현실적으로 작게 나온다. 실제 AWS DB 의 `GET /violations` total 값으로 확인한 뒤 고정한다. 읽기 전용 조회이므로 기존 위반에 영향이 없다.

남은 것: 위 3번을 AWS 에서 확인한 뒤 사전 등록 README 를 "고정" 으로 바꿔 커밋한다(이 폴더 포함). 위반 경로 검증 시나리오는 별도 설계가 필요하다.

## 8. 로컬 검증 기록 (2026-10-31, Docker Compose, AWS 연결 없음)

로컬 스택: `docker-compose.yml` + `docker-compose.phase1.yml`, 임시 DB `parking_guardian_loadcheck`(검증 후 삭제), 사진 저장 local volume. 환경변수·compose 설정에 AWS/S3/RDS 값이 없음을 `docker compose config` 로 확인했다. 모든 값은 **스크립트 동작 확인용**이며 성능 근거가 아니다(PC 한 대에서 k6·API·inference 가 같이 돈다).

| 확인 | 결과 |
|---|---|
| inference.js 1건 (`ITERATIONS=1`) | 통과. 측정 1건, 워밍업 1건은 지표에서 제외됨 |
| S0 (SCALE=0.1, 30초) | 통과. 관리자 API 151건 전부 200 |
| S1 (SCALE=0.03, C=0.4, 30초) | 통과. 단계 6개(0.2C ~ 1.5C)가 시간에 맞게 나뉨. 1.2C 이상에서 사진 503 발생(상한이 작동) |
| S2 (BURST=8) | 통과. 2건 성공, 6건 503 |
| S3, S5 (SCALE=0.03) | 통과. S3 첫 실행은 `dropped_iterations`=3, 관리자 p95 2.1초로 무효 판정 규칙에 걸렸고, 재실행은 무효 아님(p95 134 ms, dropped 0). 첫 실행의 원인은 확인하지 못했다(PC 공용 부하로 추정, 미확인) |
| 발견해서 고친 버그 | (1) 워밍업이 측정과 겹쳐 돌던 것 → `setup()` 으로 이동. (2) 단계 구분이 setup 시간만큼 밀리던 것 → setup 종료 시각 기준. (3) S2 가 C 를 요구하던 것 |
