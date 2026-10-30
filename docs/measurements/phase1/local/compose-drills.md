# Phase 1 Docker Compose 통합 드릴 (2026-10-30)

`docker-compose.yml` + `docker-compose.phase1.yml`, `python backend/scripts/check_phase1_compose.py`의 출력이다. 개발 DB를 건드리지 않도록 같은 PostgreSQL 인스턴스 안에 `parking_guardian_phase1` DB를 따로 만들어 썼고 끝난 뒤 삭제했다. API는 `api` 이미지(torch 없음), inference는 `inference` 이미지다. 설정: API 스레드 8, `AI_CLIENT_MAX_CONCURRENCY` 4, `AI_CLIENT_DEADLINE` 10초, inference 대기 상한 2, torch 스레드 1.

## 결과 요약

| 질문 | 답 |
|---|---|
| API가 inference 없이 뜨는가 | 뜬다. `/readyz` 200 (`inference: remote`), 관리자 로그인·목록 정상. 탐지만 503 |
| inference가 뜨는 동안 API는 | `/readyz`가 계속 200 (19번 확인 모두). 탐지는 503 → 모델 준비 뒤 200 |
| 실제 사진 판정이 Prototype과 같은가 | 6장 모두 같다 (불일치 0). 연속 2회 전송으로 위반 생성(201), 상태 NOTIFIED, 관리자 알림 1건 — 아래 보충 |
| inference 중단 시 | 관리자 API 0.03초에 200, 탐지는 503. 재시작 후 **31.6초** 만에 200 |
| 트래픽 중 재시작 | 탐지 28.6초간 불가(200 4건, 503 27건), 관리자 요청 77건 전부 200 (최대 0.08초). 끊긴 연결은 재시도하지 않음(`connection_lost` 2건) |
| 지연이 마감보다 길 때 | 탐지는 **10.39초**에 503 (마감 10초). 그동안 관리자 요청 10건 전부 200 |
| 동시 4장 | 3장 판정 + 1장 503 (busy 뒤 재시도도 busy). 관리자 목록 최대 0.06초 |
| 동시 8장 | API의 동시 호출 상한(4)에 막혀 4장이 0.2~0.3초에 즉시 거절, 1장은 busy 재시도 뒤 거절, 3장 판정. 관리자 목록 최대 0.08초 |

## 보충과 정정 (스크립트·환경에서 알게 된 것)

1. **C절 "위반 0건"은 스크립트 시나리오 결함이다.** 위반 확정은 "연속 2회"인데 사이에 허용 판정(sample_c, e)을 끼워 연속이 끊겼다. 살아 있는 스택에서 sample_b를 연속 2번 보내 다시 확인했다: 1번째 `200 violation_pending`, 2번째 `201 violation`(violation_id 1), 목록 1건, 상태 NOTIFIED, kim@a.com 알림 1건. 스크립트는 마지막에 sample_b를 연속 2번 보내도록 고쳤다(`check_phase1_compose.py` C절). 다만 고친 뒤 스크립트 전체를 다시 실행하지는 않았고, 같은 동작을 살아 있는 스택에서 따로 확인한 값을 위 표에 썼다. 아래 원문 출력은 고치기 전 실행의 것이라 "violations visible to the admin: 0"으로 남아 있다.
2. **inference 컨테이너가 없을 때 탐지 503이 8.5초 걸린다.** 이 환경(Docker Desktop)에서 존재하지 않는 이름을 해석하는 데 시도당 약 4초(`getaddrinfo`, 실측 4.0~4.1초)가 걸리고, API가 연결 실패를 1회 재시도하기 때문이다(4.1 + 4.1 + 알파). 연결 타임아웃(1초)은 DNS 조회를 포함하지 않는다는 `HttpInference` 문서의 한계가 그대로 나타났다. 마감 10초 안(8.5초)에 끝나서 계약은 지켜졌지만, DNS가 더 느리면 마감을 넘길 수 있다. **Kubernetes에서는 Service 이름이 엔드포인트가 없어도 해석되므로(ClusterIP) 같은 현상은 아닐 것으로 예상하나 확인하지 않았다.** 코드는 바꾸지 않았다.
3. `retries_total{reason="connect_error"}` 6건은 위 DNS 실패(A, D)와 시작 직후 연결 거절(B, E)의 재시도 합이다.

---

## A. The API without any inference service

- API `/readyz` 200 after 17.3 s (db + migrations + seed included): `{"database": "ok", "inference": "remote", "status": "ok"}`
- admin login and violation list: 200 in 0.05 s
- a detection with the inference container not running: **503 inference_unavailable** after 8.55 s

## B. The inference service starts while the API keeps serving

| seconds | inference /readyz | API detection | answer | detection took s |
|---|---|---|---|---|
| 0.5 | None | 503 | inference_unavailable | 0.50 |
| 4.7 | 503 | 503 | inference_unavailable | 0.71 |
| 32.9 | 200 | 200 | ok | 2.86 |

- API `/readyz` answered only [200] the whole time (19 checks)
- inference container health: `parking-guardian-inference-1 healthy`

## C. Real photos through API -> inference

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

## D. Inference stopped, then started

- while stopped: admin list 200 in 0.03 s, API `/readyz` 200, detection **503 inference_unavailable** after 8.53 s
- right after `start`: detection 503 inference_unavailable after 0.66 s
- first detection answered 200 **31.6 s** after `start` (model loading included)

## E. Inference restarted while detections and admin requests keep coming

- detections during the run: {200: 4, 503: 27}
- admin requests: 77, all 200: True, slowest 0.08 s
- detections were not served for 28.6 s after the restart began (restart command itself took 2.2 s to get going)

## F. Every photo delayed beyond the API deadline (INFERENCE_FAULT_DELAY_MS=15000, deadline 10 s)

- detection: **503 inference_unavailable** after 10.39 s (deadline 10 s)
- admin requests while it was waiting: 10, statuses [200], slowest 0.06 s
- API client metrics: requests_total{outcome="timeout"} 1.0; retries_total{reason="connect_error"} 6.0
- delay removed, inference ready again

## G. Photos at once through the API (API threads 8, AI_CLIENT_MAX_CONCURRENCY 4, INFERENCE_MAX_WAITING 2)

### 4 photos at once (sample_a.jpg, about 3.5 s each)

| API status | answer | seconds |
|---|---|---|
| 503 | inference_unavailable | 1.19 |
| 200 | permitted | 3.13 |
| 200 | permitted | 6.41 |
| 200 | permitted | 9.34 |

- judged 3, refused 1; whole burst 9.3 s
- admin list during the burst: 28 requests, statuses [200], slowest 0.06 s

### 8 photos at once (sample_a.jpg, about 3.5 s each)

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
