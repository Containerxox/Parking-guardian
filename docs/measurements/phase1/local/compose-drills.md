# Phase 1 Docker Compose 통합 드릴 (2026-10-30)

> **이 문서의 모든 값은 로컬 Docker Compose(Docker Desktop, CPU 8개 / 메모리 8 GiB) 측정이다. EKS가 아니다.** 같은 시나리오를 EKS에서도 검증해야 한다(설계 9.6 단계 6-3b). 이 결과를 EKS의 근거로 쓰지 않는다.

`docker-compose.yml` + `docker-compose.phase1.yml`, `python backend/scripts/check_phase1_compose.py`. 개발 DB를 건드리지 않도록 같은 PostgreSQL 인스턴스 안에 임시 DB(`parking_guardian_phase1`)를 만들어 썼고 끝난 뒤 삭제했다. API는 `api` 이미지(torch 없음), inference는 `inference` 이미지다. 설정: API 스레드 8, `AI_CLIENT_MAX_CONCURRENCY` 4, `AI_CLIENT_DEADLINE` 10초, inference 대기 상한 2, torch 스레드 1.

## 실행 기록 (두 번을 구분한다)

| | 실행 1 | 실행 2 |
|---|---|---|
| 스크립트 | **수정 전** | **수정 후** (C절 시나리오 수정, `docker exec` 출력 UTF-8) |
| 결과 | A~G 실행은 끝났으나 **C절이 잘못된 결과를 냈다** | **A~G 전 절 정상 통과** (exit 0) |
| 문제 | C절이 "위반 0건"으로 끝났다. 위반 확정은 "연속 2회"인데, 시나리오가 사이에 허용 판정 사진(sample_c, e)을 끼워 연속이 끊긴 **스크립트 결함**이었다. 서비스 결함이 아니다 | 없음. C절은 sample_b를 연속 2번 보내 `violation_pending` → `violation`(201, violation_id 1), 관리자에게 보이는 위반 1건 |
| 처리 | 같은 동작을 살아 있는 스택에서 따로 확인해 기록했고(위반 생성, 상태 NOTIFIED, 관리자 알림 1건), 스크립트를 고쳤다 | 고친 스크립트를 처음부터(스택을 내렸다 다시 올린 뒤) 전체 재실행 |

두 실행의 전문은 아래 "원문 출력"에 구분해 두었다. 실행 1의 C절 출력(위반 0건)은 고치기 전 결과 그대로 남겨 두었다.

## 장애 격리 요약: 관리자 API와 사진 탐지는 다르게 반응한다

AI 컨테이너가 없거나 막혀도 **관리자 API는 정상**이고, **사진 탐지만 503**(`inference_unavailable`)이다. 두 가지를 섞어 읽지 않는다.

| 시나리오 (AI 쪽 상태) | 관리자 API (로그인·목록·`/readyz`) | 사진 탐지 |
|---|---|---|
| A. inference 컨테이너가 없음 | `/readyz` 200 (`inference: remote`), 목록 200 (0.02~0.05초) | **503**, 8.3~8.6초 (이름 해석에 시도당 약 4초 + 1회 재시도) |
| B. inference가 막 뜨는 중(모델 로딩) | `/readyz` 계속 200 (실행 1: 19번, 실행 2: 10번 모두) | 503 → 모델 준비 뒤 **200** |
| D. inference 중단 → 시작 | 중단 중에도 목록 200 (0.03~0.04초), `/readyz` 200 | 중단 중 503 (8.3~8.5초). 시작 뒤 첫 200까지 31.6초(실행 1) / 13.1초(실행 2) |
| E. 트래픽 중 inference 재시작 | **실행 1: 77건 전부 200 (최대 0.08초), 실행 2: 53건 전부 200 (최대 0.11초)** | 재시작 뒤 28.6초(실행 1) / 18.1초(실행 2) 동안 불가: 503 27건 / 20건 (끊긴 연결은 재시도하지 않음) |
| F. 모든 사진이 마감(10초)보다 오래 걸림 (지연 15초 주입) | 대기 중 10건 전부 200 (최대 0.06 / 0.04초) | **503**, 10.39초 / 10.22초 (마감 10초) |
| G-1. 사진 4장 동시 | 목록 28건 / 13건 전부 200 (최대 0.06초) | 3장 판정 + 1장 503 |
| G-2. 사진 8장 동시 | 목록 24건 / 10건 전부 200 (최대 0.08 / 0.07초) | 3장 판정, 4장은 API 동시 호출 상한(4)에 막혀 0.1~0.3초에 즉시 503, 1장은 busy 재시도 뒤 503 |

> E의 "관리자 요청 77건 전부 정상"은 Phase 1의 장애 격리 증거로 보존한다(실행 1 원문 E절). 실행 2에서도 53건 전부 정상으로 재현됐다.

### 실제 사진 판정 (C절)
- 6장 모두 Prototype(프로세스 안) 판정과 같다(불일치 0/6). 연속 2회 전송으로 위반이 생성되고(201) 관리자에게 보인다.

### 두 실행 사이의 시간 차이 (같은 설정)

| 항목 | 실행 1 | 실행 2 |
|---|---|---|
| inference 시작 → 첫 탐지 200 | 31.6초 (B절 32.9초) | 13.1초 (B절 12.8초) |
| 모델 로딩(`pg_model_load_seconds`) | 22.5초 | 9.2초 |
| 사진 3장 동시(1장 처리 시간 추정) | 3.1 / 6.4 / 9.3초 (장당 약 3.2초) | 1.4 / 2.7 / 4.2초 (장당 약 1.2초) |

같은 이미지·같은 설정인데 2~3배 차이가 났다. 원인은 확인하지 않았다(PC의 다른 부하나 CPU 상태로 보이나 추정). 이 문서의 시간 값은 크기 정도로만 읽고, 절대값이나 비교 근거로 쓰지 않는다. 동작(어떤 응답이 나오는가)은 두 실행에서 같았다.

## 보충: 환경에서 알게 된 것

1. **inference 컨테이너가 없을 때 탐지 503이 8.3~8.6초 걸린다.** Docker Desktop에서 존재하지 않는 이름을 해석하는 데 시도당 약 4초(`getaddrinfo`, 실측 4.0~4.1초)가 걸리고, API가 연결 실패를 1회 재시도하기 때문이다. 연결 타임아웃(1초)은 DNS 조회를 포함하지 않는다는 `HttpInference` 문서의 한계가 그대로 나타났다. 마감 10초 안에 끝나 계약은 지켜졌지만 DNS가 더 느리면 넘길 수 있다. **Kubernetes Service 이름은 엔드포인트가 없어도 해석되므로 같은 현상은 아닐 것으로 예상하나 확인하지 않았다.** EKS 검증 항목에 넣었다.
2. `retries_total{reason="connect_error"}` 6건은 A, D의 DNS 실패와 시작 직후의 연결 거절(B, E)의 재시도 합이다. `connection_lost`(실행 1에만 2건)는 재시작 순간에 이미 보낸 요청의 연결이 끊긴 경우이며 재시도하지 않는다.

## EKS에서 같은 시나리오를 검증하는 계획 (설계 9.6 단계 6-3b에도 기록)

| 로컬 시나리오 | EKS에서 하는 방법 | 확인할 것 |
|---|---|---|
| A, D (inference 없음) | `kubectl scale deploy/inference --replicas=0` 후 복구 | 관리자 API 200과 탐지 503, **503이 나오기까지 시간**(Service 이름은 해석되므로 DNS 4초 현상이 없는지, 연결 타임아웃이 실제로 1초에 끊는지) |
| B (모델 로딩 중) | Pod 삭제 후 재생성, Probe 구성(설계 3.5.1) 실측 | 로딩 중 Pod가 Service에 들어오지 않는지(readiness), API `/readyz`가 흔들리지 않는지 |
| E (트래픽 중 재시작) | `kubectl rollout restart`, Pod 1개 삭제, 노드 drain | 요청 손실 구간, 관리자 API 오류율 0, PDB 효과 |
| F (지연) | `INFERENCE_FAULT_DELAY_MS` 주입 배포(테스트 전용) | 마감 안에 503, 관리자 API 영향 없음 |
| G (동시 요청) | k6로 같은 패턴 | 대기 상한·bulkhead가 EKS 자원에서도 같은 비율로 작동하는지(사진당 시간이 다르다) |

---

## 원문 출력

### 실행 2 (수정 후 스크립트, 전체 통과)

#### A. The API without any inference service

- API `/readyz` 200 after 7.0 s (db + migrations + seed included): `{"database": "ok", "inference": "remote", "status": "ok"}`
- admin login and violation list: 200 in 0.02 s
- a detection with the inference container not running: **503 inference_unavailable** after 8.29 s

#### B. The inference service starts while the API keeps serving

| seconds | inference /readyz | API detection | answer | detection took s |
|---|---|---|---|---|
| 0.4 | None | 503 | inference_unavailable | 0.35 |
| 1.7 | 503 | 503 | inference_unavailable | 0.29 |
| 12.8 | 200 | 200 | ok | 1.10 |

- API `/readyz` answered only [200] the whole time (10 checks)
- inference container health: `parking-guardian-inference-1 starting`

#### C. Real photos through API -> inference

| photo | API status | result | verdict | stickers | expected (Prototype) | match |
|---|---|---|---|---|---|---|
| sample_a.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_b.jpg | 200 | violation_pending | violation | 0 | violation | True |
| sample_c.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_d.jpg | 200 | violation_pending | violation | 0 | violation | True |
| sample_e.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_f.jpg | 200 | no_vehicle | no_vehicle | 0 | no_vehicle | True |

- verdict mismatches against the in-process results: **0** of 6
- sample_b twice in a row: 200 `violation_pending`, then 201 `violation` (violation_id 1); violations visible to the admin: 1

#### D. Inference stopped, then started

- while stopped: admin list 200 in 0.04 s, API `/readyz` 200, detection **503 inference_unavailable** after 8.29 s
- right after `start`: detection 503 inference_unavailable after 0.37 s
- first detection answered 200 **13.1 s** after `start` (model loading included)

#### E. Inference restarted while detections and admin requests keep coming

- detections during the run: {200: 5, 503: 20}
- admin requests: 53, all 200: True, slowest 0.11 s
- detections were not served for 18.1 s after the restart began (restart command itself took 1.6 s to get going)

#### F. Every photo delayed beyond the API deadline (INFERENCE_FAULT_DELAY_MS=15000, deadline 10 s)

- detection: **503 inference_unavailable** after 10.22 s (deadline 10 s)
- admin requests while it was waiting: 10, statuses [200], slowest 0.04 s
- API client metrics: requests_total{outcome="timeout"} 1.0; retries_total{reason="connect_error"} 6.0
- delay removed, inference ready again

#### G. Photos at once through the API (API threads 8, AI_CLIENT_MAX_CONCURRENCY 4, INFERENCE_MAX_WAITING 2)

##### 4 photos at once (sample_a.jpg, about 3.5 s each)

| API status | answer | seconds |
|---|---|---|
| 503 | inference_unavailable | 1.12 |
| 200 | permitted | 1.44 |
| 200 | permitted | 2.65 |
| 200 | permitted | 4.17 |

- judged 3, refused 1; whole burst 4.2 s
- admin list during the burst: 13 requests, statuses [200], slowest 0.06 s

##### 8 photos at once (sample_a.jpg, about 3.5 s each)

| API status | answer | seconds |
|---|---|---|
| 503 | inference_unavailable | 0.10 |
| 503 | inference_unavailable | 0.13 |
| 503 | inference_unavailable | 0.13 |
| 503 | inference_unavailable | 0.15 |
| 503 | inference_unavailable | 1.11 |
| 200 | permitted | 1.20 |
| 200 | permitted | 2.23 |
| 200 | permitted | 3.27 |

- judged 3, refused 5; whole burst 3.3 s
- admin list during the burst: 10 requests, statuses [200], slowest 0.07 s

API client metrics (non-zero):
- `pg_ai_client_requests_total{outcome="connect_error"} 6.0`
- `pg_ai_client_requests_total{outcome="unavailable"} 34.0`
- `pg_ai_client_requests_total{outcome="ok"} 21.0`
- `pg_ai_client_requests_total{outcome="timeout"} 1.0`
- `pg_ai_client_requests_total{outcome="busy"} 2.0`
- `pg_ai_client_requests_total{outcome="bulkhead_full"} 4.0`
- `pg_ai_client_duration_seconds_count 64.0`
- `pg_ai_client_retries_total{reason="connect_error"} 6.0`
- `pg_ai_client_retries_total{reason="busy"} 2.0`

Inference metrics (non-zero):
- `pg_inference_requests_total{outcome="ok",verdict="permitted"} 6.0`
- `pg_inference_rejected_total 4.0`
- `pg_model_load_seconds 9.203540802001953`


---

### 실행 1 (수정 전 스크립트; C절은 시나리오 결함으로 위반 0건)

#### A. The API without any inference service

- API `/readyz` 200 after 17.3 s (db + migrations + seed included): `{"database": "ok", "inference": "remote", "status": "ok"}`
- admin login and violation list: 200 in 0.05 s
- a detection with the inference container not running: **503 inference_unavailable** after 8.55 s

#### B. The inference service starts while the API keeps serving

| seconds | inference /readyz | API detection | answer | detection took s |
|---|---|---|---|---|
| 0.5 | None | 503 | inference_unavailable | 0.50 |
| 4.7 | 503 | 503 | inference_unavailable | 0.71 |
| 32.9 | 200 | 200 | ok | 2.86 |

- API `/readyz` answered only [200] the whole time (19 checks)
- inference container health: `parking-guardian-inference-1 healthy`

#### C. Real photos through API -> inference

| photo | API status | result | verdict | stickers | expected (Prototype) | match |
|---|---|---|---|---|---|---|
| sample_a.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_b.jpg | 200 | violation_pending | violation | 0 | violation | True |
| sample_c.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_d.jpg | 200 | violation_pending | violation | 0 | violation | True |
| sample_e.jpg | 200 | permitted | permitted | 1 | permitted | True |
| sample_f.jpg | 200 | no_vehicle | no_vehicle | 0 | no_vehicle | True |

- verdict mismatches against the in-process results: **0** of 6
- a second sighting of sample_b: 200 `violation_pending`; violations visible to the admin: 0

#### D. Inference stopped, then started

- while stopped: admin list 200 in 0.03 s, API `/readyz` 200, detection **503 inference_unavailable** after 8.53 s
- right after `start`: detection 503 inference_unavailable after 0.66 s
- first detection answered 200 **31.6 s** after `start` (model loading included)

#### E. Inference restarted while detections and admin requests keep coming

- detections during the run: {200: 4, 503: 27}
- admin requests: 77, all 200: True, slowest 0.08 s
- detections were not served for 28.6 s after the restart began (restart command itself took 2.2 s to get going)

#### F. Every photo delayed beyond the API deadline (INFERENCE_FAULT_DELAY_MS=15000, deadline 10 s)

- detection: **503 inference_unavailable** after 10.39 s (deadline 10 s)
- admin requests while it was waiting: 10, statuses [200], slowest 0.06 s
- API client metrics: requests_total{outcome="timeout"} 1.0; retries_total{reason="connect_error"} 6.0
- delay removed, inference ready again

#### G. Photos at once through the API (API threads 8, AI_CLIENT_MAX_CONCURRENCY 4, INFERENCE_MAX_WAITING 2)

##### 4 photos at once (sample_a.jpg, about 3.5 s each)

| API status | answer | seconds |
|---|---|---|
| 503 | inference_unavailable | 1.19 |
| 200 | permitted | 3.13 |
| 200 | permitted | 6.41 |
| 200 | permitted | 9.34 |

- judged 3, refused 1; whole burst 9.3 s
- admin list during the burst: 28 requests, statuses [200], slowest 0.06 s

##### 8 photos at once (sample_a.jpg, about 3.5 s each)

| API status | answer | seconds |
|---|---|---|
| 503 | inference_unavailable | 0.20 |
| 503 | inference_unavailable | 0.23 |
| 503 | inference_unavailable | 0.28 |
| 503 | inference_unavailable | 0.28 |
| 503 | inference_unavailable | 1.22 |
| 200 | permitted | 3.18 |
| 200 | permitted | 5.17 |
| 200 | permitted | 7.96 |

- judged 3, refused 5; whole burst 8.0 s
- admin list during the burst: 24 requests, statuses [200], slowest 0.08 s

API client metrics (non-zero):
- `pg_ai_client_requests_total{outcome="connect_error"} 6.0`
- `pg_ai_client_requests_total{outcome="connection_lost"} 2.0`
- `pg_ai_client_requests_total{outcome="unavailable"} 56.0`
- `pg_ai_client_requests_total{outcome="ok"} 19.0`
- `pg_ai_client_requests_total{outcome="timeout"} 1.0`
- `pg_ai_client_requests_total{outcome="busy"} 2.0`
- `pg_ai_client_requests_total{outcome="bulkhead_full"} 4.0`
- `pg_ai_client_duration_seconds_count 86.0`
- `pg_ai_client_retries_total{reason="connect_error"} 6.0`
- `pg_ai_client_retries_total{reason="busy"} 2.0`

Inference metrics (non-zero):
- `pg_inference_requests_total{outcome="ok",verdict="permitted"} 6.0`
- `pg_inference_rejected_total 4.0`
- `pg_model_load_seconds 22.49752712249756`
