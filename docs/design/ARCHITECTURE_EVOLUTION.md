# Parking Guardian 아키텍처 발전 계획 (Prototype → Phase 1 → Phase 2)

- 상태: **설계 검토용 v3** (2026-10-30). 코드·매니페스트·Terraform·AWS 는 아무것도 바꾸지 않았다.
- 표기: **[확인]** = 저장소 코드나 과거 세션 측정에서 직접 확인한 사실. **[제안]** = 설계 제안. **[추정]** = 측정 전 예상치(측정으로 대체). **[확인 필요]** = 구현 전에 사실 확인이 필요한 것.
- 경로 접두어: `app:` = `capston/`, `config:` = `parking-guardian-config/`, `infra:` = `parking-guardian-infra/`.
- 원칙: Phase 1 과 Phase 2 모두 **안전한 운영 설정**(타임아웃, 동시 상한, 재시도 상한, 멱등성)을 갖춘 상태에서 실제 한계를 잰다. 어느 쪽도 일부러 약하게 만들지 않는다. 장애 주입은 테스트 전용 설정으로만 켜고 결과에 "주입 실험" 으로 표시한다.

## 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v1 | 2026-10-30 | 초안: 로드맵, Phase 1 설계, 실험, S0~S5 수정안, Phase 2 ADR 초안 |
| v3 | 2026-10-30 | 문서 보존 방식 B 결정(공개 문서만 선택 추적). CPU 요청 여유 80m 검토와 배치 안 비교(3.7.1), 최종 변경 파일 목록과 구현 순서(9.1, 9.6) 갱신. ADR 0018·0019, 사전 등록 초안 별도 파일로 작성 |
| v2 | 2026-10-30 | 사용자 결정 5가지 반영. 부하 발생기 전용 노드 격리 설계(3.8), 동시 상한 값을 실험으로 정하는 절차(4.2), Phase 2 상태 경로와 판별 경로 분리, MessageGroupId 분석, event_id·session_id·seq, 아웃박스 재전송기와 상태 전이, 부분 실패 표, 보존 정책 구분, 전 과정 멱등성 검증표, confirm 타이밍 분석과 명령 설계, 장비 재시도·백오프(6장). Phase 1 착수 전 최종 정리(9장), 문서 보존 방안(10장) |

## 사용자 결정 (2026-10-30)

| 번호 | 결정 | 반영 위치 |
|---|---|---|
| D1 | 부하 테스트 때만 전용 노드를 임시 추가. nodeSelector/taint 로 격리. 실제 추가는 별도 승인 | 3.8 |
| D2 | Phase 1 공식 구성에 타임아웃·동시 호출 상한 포함. 값(스레드 절반, 대기 2)은 고정값이 아니라 실험으로 조정. 상한 제거 실험은 별도 구분 | 3.4, 3.5, 4.2 |
| D3 | Phase 2 는 SQS FIFO + DB 순서 가드. MessageGroupId 단위 검토, event_id·session_id·sequence 로 중복·오래된 결과 구분, left 가 AI 큐에 막히지 않게 | 6.3, 6.4, 6.5 |
| D4 | confirm 은 Heartbeat 응답 활용. 타이밍 분석, confirm_due_at·명령 ID·세션 ID 로 중복·지연·재접속 처리, 장비 재시도·백오프 | 6.9, 6.10 |
| D5 | ADR 번호 Phase 1 = 0018, Phase 2 = 0019 (충돌 확인: 10.4) | 7, 6 |

---

## 0. 요약

| 단계 | 구조 | 이 단계가 증명할 것 |
|---|---|---|
| Prototype | Flask API + YOLO 한 Pod (지금 구현) | 기능 검증. 공식 비교 대상 아님. 태그·문서로 보존 |
| Phase 1 | API Deployment ↔ (동기 HTTP, 타임아웃·동시 상한) ↔ AI Inference Deployment | 책임·자원·권한·확장 정책 분리. 동기 대기가 남기는 한계를 부하 테스트로 측정 |
| Phase 2 | API → S3 inbox + DB 아웃박스 → SQS FIFO → AI Worker (KEDA). 상태 이벤트는 큐를 거치지 않음 | 접수와 판별의 분리, 장애 격리, 재시도, 멱등성, 순서 보장, 복구. **최종 판별 지연·처리량·신뢰성·비용을 함께** 비교 |

---

## 1. 저장소 분석: 확인한 사실

### 1.1 앱 (`app:backend`)

| 항목 | 사실 [확인] | 위치 |
|---|---|---|
| 추론 포트 | `adapters.inference().analyze(bytes, filename)` 하나. 구현체 `YoloInference`, `MockInference`. 모듈 주석에 "다음 단계는 HTTP 클라이언트로 교체" | `app/adapters/inference.py` |
| 락 | `threading.Lock` 하나로 모델 로딩과 두 모델 실행을 감싼다. 주석 "hub models are not safe for concurrent calls" (검증 기록 없음) | 같은 파일 |
| 상태 | `cold / loading / ready / failed`. gunicorn `post_worker_init` 훅에서 백그라운드 웜업 | `inference.py`, `gunicorn.conf.py` |
| 무거운 import | `torch`, `cv2`, `numpy` 는 `inference.py` 함수 안에서만 import | grep |
| API 의존성 | `requirements.txt` 에 torch 없음. torch·YOLOv5 는 Dockerfile 이 따로 설치 | `requirements.txt`, `Dockerfile` |
| 서버 | gunicorn worker 1 × thread 4, timeout 120 | `Dockerfile` CMD |
| 메트릭 | 9102 별도 포트. HTTP 2개 + 추론 6개 = 8개 | `app/metrics.py` |
| 장비 API 흐름 | 인증 → 추론 → `record_verdict()`(장비 행 `FOR UPDATE`) → 새 위반일 때만 사진 S3 저장 → 알림. 사진·알림 실패는 사건을 되돌리지 않고 이력만 남김 | `app/api/device.py` |
| 추론 실패 계약 | `InvalidImageError` → 400, `InferenceError` → 503 `inference_unavailable` | `device.py` |
| readyz | DB `SELECT 1` + 추론 상태. `loading` 이면 503, `failed` 는 200 | `app/api/health.py` |
| 사건 규칙 | 위반 판정 2회 연속 → 생성(**어떤 이벤트의 사진이든 센다**: arrived, confirm, refresh), 비위반 3회 → 종료, `event=left` + `no_vehicle` → 즉시 종료, 10분 무신호 → SIGNAL_LOST. 활성 위반 유니크 인덱스 | `app/services/detections.py` |
| 시각 사용 | 끊김 판정, `last_detection_at`, `last_seen_at`, 종료 시각에 **`utcnow()`(처리 시각)**. `detected_at`(장비 시각)은 `pending_since`, `first_seen` 에만 | 같은 파일 |
| 상태 소유자 | 사건 진행 상태(`pending_violation_count`, `pending_since`, `clear_count`, 마지막 신호)는 **Machine 행**에 있다. 활성 위반도 machine 기준으로 찾는다. ParkingZone 은 `machine_id` 를 가지며 한 장비가 여러 면을 볼 수 있는 모델 구조지만, ADR 0014 운영은 장비 1대 = 지정 주차면 1칸 | `models/parking.py`, `detections.py` |
| 알림 | `SyncNotificationPublisher` 가 요청 안에서 처리. 주석에 "다음 단계는 SQS publisher". `notify_violation()` 은 (violation, admin, channel) 유니크 키로 멱등 | `adapters/notifier.py`, `services/notifications.py` |
| 사진 보존 | 위반 확정 사진만 `violations/company-*/parking-lot-*/YYYY/MM/DD/violation-<id>.<ext>`. `image_expires_at` = +90일 기록만, 실제 삭제 없음 | `adapters/storage.py`, `config.py` |

### 1.2 장비 (`app:device/pi_client.py`)

| 항목 | 사실 [확인] |
|---|---|
| 이벤트 | 진입 확정 → `arrived`, 응답이 `violation_pending` 이면 **응답 받은 시각 + 60초**에 `confirm`, 점유 중 10분마다 `refresh`, 출차 확정 → `left` |
| 동기 응답 의존 | ① `violation_pending` 일 때만 confirm 예약 ② LED 를 `result` 로 켬 ③ 로그 |
| 전송 | 하나씩 동기 전송, 타임아웃 (연결 5초, 응답 60초). 실패하면 로그만, **재시도 없음** |
| Heartbeat | 60초마다. 응답 본문은 `ok`, `server_time` 뿐 |
| 없는 것 | `event_id`, `seq`, 세션 ID, 재시도 큐 (ADR 0014 "MVP 제외") |

### 1.3 config, infra

| 항목 | 사실 [확인] |
|---|---|
| backend Deployment | replicas 없음(HPA), maxSurge 1 / maxUnavailable 0, podAntiAffinity(hostname, required), zone spread, initContainer 가 모델 412 MB 다운로드, CPU 1 / 메모리 1.5Gi·2Gi, grace 130초 |
| HPA / PDB | CPU 70%, min 2 / max 3, scaleDown 안정화 300초 / minAvailable 50% |
| 마이그레이션 | Job(sync-wave 1) → Deployment(wave 2). Job 은 torch 를 import 하지 않음 |
| Ingress | `/api` → backend:5000, `/` → frontend:8080, ALB idle 120초 |
| 노드 | m7i-flex.large(2 vCPU, 8 GiB) 1개 노드 그룹, desired 3 / max 3, desired 는 ignore_changes |
| Pod Identity | `cluster/iam.tf` backend 역할 하나(사진 Put/Get/Head + 모델 Get/List) |
| ECR | `data/storage_ecr.tf` 의 `toset(["backend","frontend"])`. CI 푸시 권한은 `for r in aws_ecr_repository.this` 로 **저장소 집합 전체**에 자동으로 붙음 |
| S3 | 사진·모델 버킷. `storage_s3.tf` 에 versioning·lifecycle 설정 없음 |
| 명칭 | infra `docs/phase1/architecture.md`, Ingress 태그 `Phase=1`, HPA 주석이 **지금 구조를 "Phase 1"** 이라 부름 |

### 1.4 노드 자원 (2026-10-28 `kubectl describe nodes`, 모니터링 설치 전)

| 항목 | 값 [확인] |
|---|---|
| Allocatable | CPU 약 1,930m, 메모리 약 7.2 GiB (요청 비율에서 역산) |
| backend 노드의 비-backend 요청 | 약 650m / 640Mi |
| backend 메모리 | 유휴 RSS 약 982Mi, 측정 문서 최대 910 MB |
| 추론 1장 | PC 평균 557~866 ms, p95 0.8~1.7초. **EKS 미측정** |
| 이미지 | backend 2.89 GB / ECR 약 660 MB, torch 레이어 1.77 GB |

재구축 직후 다시 재서 갱신한 뒤 요청값을 확정한다.

---

## 2. 재사용 / 변경 분류

| 대상 | 판정 | 내용 |
|---|---|---|
| `YoloInference`, 락, 상태 머신, 웜업 | 그대로 재사용 | AI 서비스 안으로 옮긴다. 락 유지 |
| `MockInference` | 재사용 | AI 서비스 테스트 백엔드, API 단위 테스트 |
| 메트릭 8개 | 재사용 | 양쪽에서 해당하는 것만 기록 |
| `device.py`, `record_verdict()`, 사진 저장, 알림 | Phase 1 그대로 | Phase 2 에서 3~4단계를 서비스 함수로 추출해 Worker 와 공유, `record_verdict` 에 시각·순서 인자 추가 |
| `health.py` readyz | 작은 변경 | API 는 원격 추론 상태를 readiness 에 넣지 않음 |
| Dockerfile, gunicorn 설정, CI | 변경 | 멀티 타깃(api / inference), 이미지 2개 |
| config 매니페스트 | 추가·변경 | `backend` 이름은 API 로 유지(Ingress, Service, ALB 헬스체크, 마이그레이션 Job 변경 없음). `inference-*` 를 추가 |
| infra | 소규모 추가 | ECR `inference`, inference Pod Identity(모델 읽기), backend 역할에서 모델 권한 제거, (승인 뒤) 부하 발생기 노드 그룹 |
| Argo CD, ESO, ALB, RDS, 네트워크, 모니터링 | 그대로 | |
| `pi_client.py` | Phase 1 그대로 | Phase 2 에서 변경(6.9~6.10) |
| `docs/measurements` | 보존 | Prototype 측정 이력 |

---

## 3. Phase 1 상세 설계 — 동기식 API / AI 서비스 분리 (ADR 0018)

### 3.1 구조

```text
Raspberry Pi ──HTTPS──▶ ALB ──/api──▶ Service backend:5000 ──▶ backend(api) Pod  (Flask, torch 없음)
                                                                 │ HttpInference: 마감·재시도 1회·동시 상한
                                                                 ▼ POST /v1/analyze (원본 바이트)
                                          Service inference:8000 (ClusterIP, 내부 전용)
                                                                 ▼
                                                        inference Pod (YoloInference + 락 + 대기 상한)
                                                        └─ initContainer: S3 모델 다운로드
backend Pod ──▶ RDS, S3 사진 버킷, 알림          inference Pod ──▶ DB 접속 없음, 모델 버킷 읽기만
```

### 3.2 역할 분담 [제안]

| 책임 | api | inference |
|---|---|---|
| 인증, 관리자 기능, 목록·알림·사진 조회 | ✔ | |
| 장비 인증, 업로드 검증 | ✔ | |
| 디코드, 앞유리 탐지, Crop, 스티커 탐지, 판정값 | | ✔ |
| 사건 규칙, DB, 사진 저장, 알림 | ✔ | |

inference 는 DB 자격 증명이 없고 Secret 을 마운트하지 않는다. 장애 영향은 "판정을 못 한다" 로 한정된다.

### 3.3 AI 서비스 인터페이스 [제안]

```text
POST /v1/analyze   Content-Type: application/octet-stream, X-Filename, X-Request-Id
  200 InferenceResult.to_dict()
  400 {"error":"invalid_image"}
  503 {"error":"busy"} + Retry-After: 1        (대기 상한 초과, 3.5)
  503 {"error":"model_unavailable"}            (loading / failed)
GET /healthz (생존), GET /readyz (모델 ready 일 때만 200), :9102 /metrics
```

Flask 를 재사용한다(`inference_service/` 앱 팩토리 + 라우트 1개). FastAPI 전환은 범위 밖.

### 3.4 API 쪽 `HttpInference` 어댑터 [제안]

| 항목 | 초기값 | 확정 방법 |
|---|---|---|
| 연결 타임아웃 | 1초 | 고정(클러스터 내부) |
| 전체 마감 `AI_CLIENT_DEADLINE` | 10초 | T3 (4.2): E0 p99 × (1 + 대기 상한) + 여유 |
| 재시도 `AI_CLIENT_RETRIES` | 1 (busy·연결 실패만, 마감 안에서) | 고정. 2회 이상은 과부하 증폭 |
| 동시 상한 `AI_CLIENT_MAX_CONCURRENCY` | 4 (스레드 8 의 절반) | **T2 (4.2) 로 조정** |
| 예외 매핑 | 타임아웃·연결·5xx·busy → `InferenceError`, 400 → `InvalidImageError` | `device.py` 수정 없이 기존 503/400 계약 유지 |
| `state` | `"remote"` | readyz 가 AI 상태로 API 를 NotReady 로 만들지 않게 |

**readyz 에서 AI 를 빼는 이유**: AI 장애 → 모든 API Pod NotReady → ALB 가 관리자 API 까지 503 이 된다. 분리의 목적이 장애 격리이므로 API readiness 는 DB 만 본다.

### 3.5 AI 서비스 동시성 [제안]

| 항목 | 초기값 | 확정 방법 |
|---|---|---|
| 프로세스 / 모델 인스턴스 | worker 1 / 1개 | E2 결과로 재검토 |
| 스레드 | 4 | 대기 상한 + 2 (추론 1 + 헬스체크 1) 이상 |
| 동시 추론 | 1 (기존 락) | 락은 스레드 안전성 검증 전에는 제거하지 않음 |
| 대기 상한 `INFERENCE_MAX_WAITING` | 2 | **T1 (4.2) 로 조정** |
| torch 스레드 `TORCH_NUM_THREADS` | 1 (지금 Pod 안 값 [확인]) | E1 |
| readiness | 모델 ready 일 때만 200. busy 는 반영 안 함 | 부하로 NotReady 가 되면 남은 Pod 로 몰려 연쇄 장애 |

### 3.6 API 서버 동시성 [제안]

| 항목 | 초기값 | 이유 / 확정 |
|---|---|---|
| gunicorn | worker 1 × thread 8 | 모델이 없어 스레드 비용이 작고, 작업이 DB·S3·HTTP 대기 위주. worker 2 이상은 prometheus-client 멀티프로세스 모드가 필요해 미룸 [확인: `metrics.py` 주석]. T4 (선택) |
| DB 연결 | SQLAlchemy 기본 풀 5 + overflow 10 | **RDS t4g.micro `max_connections` 와 (api Pod 최대 수 × 15 + 마이그레이션·재전송기) 비교 [확인 필요]** |
| gunicorn graceful timeout | 기본 30초 | grace 130초와 맞지 않을 수 있음 [확인 필요, 학습 교재 Part 1 메모]. S5(c) 결과에 영향 |

### 3.7 자원과 배치

노드당 쓸 수 있는 양(잠정): CPU 1,930m − 650m ≈ **1,280m**, 메모리 7.2 − 0.65 ≈ **6.5 GiB**.

| Pod | CPU 요청 / limit | 메모리 요청 / limit | 근거 |
|---|---|---|---|
| inference | 1,000m / 없음 | 1.5Gi / 2Gi | Prototype 과 같음 |
| backend(api) | 200m / 없음 | 256Mi / 512Mi | [추정] RSS 150~300 MB. 로컬 `docker run` 측정 뒤 확정 |
| frontend | 50m | 64Mi / 128Mi | 그대로 |

| 노드 | 평상시 | CPU 요청 합 | 메모리 요청 합 |
|---|---|---|---|
| N1 | inference + api | 650 + 1,000 + 200 = 1,850m / 1,930m | 약 2.4 GiB |
| N2 | inference + api | 1,850m | 약 2.4 GiB |
| N3 | 모니터링·Argo CD·frontend, surge 자리, HPA 3번째 inference 자리 | 약 800m | 약 2~3 GiB |
| L1 (테스트 때만) | k6 전용 | 3.8 | |

- 제약은 CPU. 노드당 inference 1개. N1·N2 여유는 약 80m 라 재측정에서 더 작으면 inference 요청을 900m 로 낮추거나 api 를 N3 에 둔다.
- inference HPA min 2 / max 3 + podAntiAffinity(required). api HPA min 2 / max 3, 분산은 preferred.
- 선택: api→inference 를 같은 AZ 로 우선 보내는 Service `trafficDistribution: PreferClose`(EKS 1.35 에서 사용 가능 [확인 필요]). 기본은 끄고 측정 뒤 결정.

### 3.7.1 N1·N2 의 CPU 요청 여유 80m 검토 (v3)

**80m 가 뜻하는 것**: 요청(requests)은 스케줄러의 예약이다. 실제 사용량의 상한이 아니다(limit 없음). 따라서 80m 는 "Pod 가 실행 중에 CPU 가 모자란다" 가 아니라 **"요청 합이 조금만 늘어도 새 Pod 가 그 노드에 못 뜬다"** 는 뜻이다.

**확인된 문제 4가지**

| 번호 | 문제 | 근거 |
|---|---|---|
| 1 | 시스템 요청 650m 은 추정치다. add-on 업그레이드나 DaemonSet 추가로 100m 만 늘어도 api 가 N1·N2 에서 Pending 이 된다 | 1.4절 값은 모니터링 설치 전, 역산 값 |
| 2 | **HPA 상한 충돌**: inference 가 3개로 늘면 3번째가 N3 에 뜬다(800 + 1,000 = 1,800m). 그러면 api 3번째(200m)는 어디에도 못 뜬다. S4(자동 확장)에서 api 가 늘지 못한다 | 3.7 배치표 계산 |
| 3 | **실제 CPU 경합**: limit 이 없을 때 노드 CPU 가 모자라면 커널(CFS)은 요청에 비례해 나눈다(1,000 : 200). torch 가 1스레드면 inference 는 약 1코어만 쓰므로 나머지 1코어를 api 와 DaemonSet 이 쓴다. 영향은 작을 것으로 본다 [추정]. E1 에서 torch 2스레드를 고르면 경합이 커진다 | 1.1절 torch 스레드 1 [확인] |
| 4 | **측정 해석의 오염**: 공존 상태에서 일반 API 가 느려지면 원인이 "동기 대기" 인지 "같은 노드 CPU 경합" 인지 섞인다. Phase 1 의 결론(H1)이 흐려진다 | — |

**비교**

| 안 | 내용 | N1·N2 여유 | 운영 | 측정 | 비용 | 판단 |
|---|---|---|---|---|---|---|
| A | 3대, 공존, api 200m (v2 안) | 80m | 동작한다. 여유가 작고 문제 2가 남는다 | 문제 4 | 0 | 운영 기본 후보 |
| B | api 요청 100m | 180m | HPA 70% 기준이 70m 가 되어 지나치게 민감하다. 경합 때 CPU 몫이 더 줄어든다. 실제 사용량보다 작은 요청은 과다 배치를 부른다 | 문제 4 그대로 | 0 | 기각 |
| C | inference 요청 900m | 180m | 약 1코어를 쓰는 Pod 를 덜 예약한다. torch 1스레드면 실제 영향은 작다 | 문제 4 그대로 | 0 | A 의 대체안 |
| D | 3대, 역할 분리(api 2개를 N3 에) | — | api 가 노드 1대에 몰려 그 노드 장애 = API 전체 중단. 모니터링과 같은 노드 | 분리는 깨끗 | 0 | 기각 |
| **E** | **테스트 세션 동안만 main 노드 4대** + api 가 inference 노드를 피하는 preferred anti-affinity | 넉넉 (api 는 N3·N4) | 평소 3대는 A 와 같다 | 공존 없이 측정. inference·api 모두 3까지 확장 가능 | 테스트 시간 동안 노드 1대 | **측정용 권장** |
| F | 평소에도 4대 | 넉넉 | 가장 깨끗 | 깨끗 | 상시 노드 1대 | 실제 트래픽(하루 사진 백여 장 수준)에 과하다 |

**권장**

1. **매니페스트는 노드 수에 맞춰 스스로 배치되게 한다.** api 에 두 가지 preferred 규칙을 둔다. api 끼리 다른 노드(가중치 100), inference 가 있는 노드 피하기(가중치 50). 3대면 공존하고 4대면 갈라진다. 테스트 전용 설정 파일이 필요 없다.
2. **운영 기본은 3대, 안 A.** 재측정에서 노드 시스템 요청이 700m 를 넘으면 안 C 로 바꾼다.
3. **측정은 안 E.** S0~S5 와 Phase 2 의 같은 테스트 모두 main 4대 + 부하 노드 1대에서 한다. 비교 A·B 의 노드 조건이 Phase 1 과 Phase 2 에서 같다.
4. **api 요청값은 측정으로 다시 정한다.** S0(일반 API 5 req/s)의 api CPU 사용량 p95 를 보고 요청을 맞춘다. 요청이 실제 사용량을 반영해야 HPA 의 % 가 의미를 갖는다.
5. **선택 실험 P1**: S1 프로필을 "3대 공존" 과 "4대 분리" 에서 한 번씩 돌려 공존 경합의 크기를 숫자로 남긴다. Prototype 계획의 "자원 경합" 가설을 측정 항목으로 되살리는 셈이다.

**안 E 에 필요한 것 (별도 승인 뒤)**: infra `cluster` 의 main 노드 그룹 `node_max_size` 3 → 4(값 하나). desired 는 지금처럼 CLI 로 테스트 때만 4. 테스트 중 전체 노드는 main 4 + 부하 1 = 5대, 10 vCPU 다. 계정의 vCPU 한도와 Free plan 허용 여부를 확인해야 한다 [확인 필요].

### 3.8 부하 발생기 전용 노드 (D1, 실제 추가는 별도 승인)

| 항목 | 설계 [제안] |
|---|---|
| 노드 그룹 | 기존 `main` 과 **별도의 관리형 노드 그룹 `loadgen`** (infra `cluster/nodes.tf` 에 추가). min 0 / max 1 / **desired 0**, desired 는 ignore_changes |
| 인스턴스 | 변수로. 기본 m7i-flex.large. c7i-flex.large 가 Free plan 에서 허용되면 더 싸다 [확인 필요] |
| 라벨 | `workload=loadgen` |
| taint | `dedicated=loadgen:NoSchedule` (노드 그룹 taint 블록) |
| k6 Job | `nodeSelector: workload=loadgen` + 해당 toleration. CPU 요청 1,000m / 메모리 1Gi 로 노드를 사실상 점유 |
| 앱·모니터링 Pod | toleration 이 없으므로 이 노드에 뜨지 않는다 |
| 예외 | 모든 taint 를 허용하는 DaemonSet(aws-node, kube-proxy, ebs-csi-node, CloudWatch 에이전트, node-exporter)은 뜬다. 정상이며 node-exporter 로 k6 노드 CPU 도 본다 |
| 격리 확인 | 켠 직후 `kubectl get pods -A -o wide --field-selector spec.nodeName=<L1>` 로 DaemonSet 과 k6 외에 없는지 확인 |
| k6 병목 확인 | 테스트 중 L1 CPU < 70%. 넘으면 결과 무효로 표시 |
| 운영 | 세션 시작 `aws eks update-nodegroup-config --scaling-config desiredSize=1`, 끝나면 0. 해체 순서에 "loadgen desired 0" 확인 추가 |
| 기존 HPA | 영향 없음. inference·api 의 max 는 main 노드 수 3 기준 그대로 |
| 대안 | E0~E3 은 HPA 를 쓰지 않으므로 L1 없이 N3 에서 k6 를 돌려도 된다(비용 0). S1~S5 는 L1 사용 |

### 3.9 확장 정책

| | api | inference |
|---|---|---|
| 지표 | CPU 70% (200m 기준) | CPU 70% (1,000m 기준) |
| min / max | 2 / 3 | 2 / 3 |
| scaleDown 안정화 | 120초 [추정] | 300초 |
| PDB | minAvailable 1 | minAvailable 50% |

### 3.10 계측 [제안]

api 추가: `pg_ai_client_request_seconds{outcome}`(ok, timeout, connect_error, busy, server_error, invalid_image), `pg_ai_client_in_flight`, `pg_ai_client_rejected_total`, `pg_http_threads`(상수). inference 추가: `pg_inference_rejected_total{reason}`. PodMonitor 2개, `route` 라벨 규칙 유지. 대시보드 `phase-comparison` 재구성(부하 → 일반 API 분위수·오류 → 스레드 점유 → AI 호출 → 락 대기·대기 수·거절·처리량 → Pod 자원·Ready → HPA).

### 3.11 Phase 1 의 알려진 비용

네트워크 홉 1개와 사진 바이트 재전송, 이미지 2개·CI 1개 추가, 매니페스트 약 2배. inference 콜드 스타트는 그대로, api 는 작은 이미지로 빨라짐 [추정].

---

## 4. 소규모 실험 (Phase 1 본 테스트 전)

### 4.1 동시성 실험

inference 단독, k6 closed-loop.

| 번호 | 바꾸는 것 | 측정 | 비고 |
|---|---|---|---|
| E0 | 없음 (Pod 1, worker 1, torch 1, 동시 1) | 장당 서비스 시간 분포, 최대 처리량 C₁ | 이후 모든 값의 근거 |
| E1 | torch 스레드 1 → 2 | 처리량, p95, CPU | 남는 CPU 가 적어 이득이 없을 수 있다 [추정]. 단정하지 않음 |
| E2 | 같은 노드에 inference Pod 2개(요청 각 600m) = "프로세스 2개" | 노드당 처리량, 지연, 메모리 | 코드 변경 없이 프로세스 수 효과를 본다 |
| E3 | Pod 1 → 2 → 3 (노드마다 1개) | 서비스 전체 처리량 C | 선형 확장 여부. **C 가 S 시나리오 부하 단계의 기준** |

### 4.2 상한 값 조정 (D2) — 값이 아니라 "선택 규칙" 을 사전 등록한다

| 번호 | 조정 대상 | 후보 | 부하 | 선택 규칙 (테스트 전 고정) |
|---|---|---|---|---|
| T1 | `INFERENCE_MAX_WAITING` | 0, 1, 2, 4 | inference 단독, 1.2C₁ | 처리량이 최대의 98% 이상인 후보 중 **가장 작은 값**. 모델을 쉬지 않게 할 만큼만 줄을 세우고, 그 이상은 지연만 늘린다 |
| T2 | `AI_CLIENT_MAX_CONCURRENCY` | 2, 4, 6 (스레드 8) | S1 혼합 부하 1.5C 단계 | 일반 API p95 < 500ms 와 오류율 < 1% 를 지키는 후보 중 **사진 성공률이 가장 높은 값**. 동률이면 작은 값 |
| T3 | `AI_CLIENT_DEADLINE` | 계산값 | — | E0 p99 × (1 + T1 결과) + 1초, 올림 |
| T4 (선택) | API 스레드 수 | 4, 8, 16 | S1 | T2 와 같은 규칙 |
| **X1 (별도 구분)** | 상한 제거 (`MAX_CONCURRENCY` = 스레드 수, `MAX_WAITING` = 무제한) | — | S1 | 공식 구성이 아니다. "상한이 무엇을 막아 주는가" 를 보여 주는 대조 실험. 보고서에서 별도 절 |

T1~T3 결과로 정한 값이 **Phase 1 공식 구성**이 되고, 그 값으로 S0~S5 를 실행한다. 선택 규칙에 맞는 후보가 없으면 그 사실을 기록하고 가장 가까운 후보를 쓴다.

---

## 5. 부하 테스트 계획 (Phase 1 / Phase 2 공통 골격)

### 5.1 원칙

- 같은 k6 스크립트, 같은 사진(sample_a 정상, sample_b 위반), 같은 장비 키 5개, 같은 노드 타입, k6 는 L1 노드.
- 일반 API: 로그인 1회 후 위반 목록, 대시보드 요약, 알림 조회 반복 + readyz 1 req/s 별도 기록.
- 사진 도착률 단계는 **E3 의 처리 용량 C 의 배수**(0.2C, 0.5C, 0.8C, 1.0C, 1.2C, 1.5C). 절대값 표도 남긴다.
- 각 시나리오 3회, 중앙값과 범위. 한 회차 15분 이내. node-exporter CPU steal 기록(m7i-flex 특성).
- **비교 A (동일 자원)**: HPA·KEDA 끔, api 2 + inference(또는 Worker) 2 고정, 같은 요청값. **비교 B (운영 자동 확장)**: HPA/KEDA 켬, 같은 최대 노드 수.
- 양쪽 모두 공식 구성(안전 설정)으로만 측정한다. 장애 주입 설정은 S5 에서만 켠다.

### 5.2 시나리오

| 번호 | 목적 | Phase 1 | Phase 2 (같은 프로필) |
|---|---|---|---|
| S0 | 기준선 | 일반 API 5 req/s, 5분, 사진 0 | 같음 |
| S1 | 결합 지점 | 일반 API 5 req/s + 사진 0.2C→1.5C 단계(각 3분), 비교 A | 접수 p95, 큐 길이, End-to-End 판별 지연 |
| S2 | 버스트 | 사진 20건 동시 후 유휴 | 큐 해소 시간, 유실 0 |
| S3 | 지속 과부하 | 사진 1.2C 10분 → 거절·타임아웃, 스레드 점유, 회복 시간 | 적체 크기·해소 시간, 유실 0, DLQ 0 |
| S4 | 자동 확장 | S1 프로필, 비교 B. api·inference HPA 각각의 반응. "일반 API 만 3배" 로 api 만 느는지 | KEDA 반응(큐 → Worker Ready → 첫 처리) |
| S5 | 장애 (주입 실험) | (a) inference 0개 (b) inference 지연 주입 (c) api Pod 1개 삭제 | (a) Worker 0개 (b) 처리 중 Worker 강제 종료 (c) 독 메시지 → DLQ (d) 같은 사진 중복 전송 (e) 순서 뒤바꿈 재전송 (f) SQS 전송 실패 주입 → 재전송기 회수 (g) 메시지 삭제 실패 주입 |

### 5.3 지표

Phase 1: 일반 API p50/p95/p99·오류율, AI 호출 시간·outcome·타임아웃, API 스레드 점유율, AI 처리량(Pod 별·전체), 락 대기, 거절 수, 전파 정도(S5 동안 일반 API 대 S0), api·inference CPU·메모리, HPA 트리거 → Ready → 첫 성공 추론, **사진 성공률**.

Phase 2 추가: 접수 응답 시간, SQS 대기 수·가장 오래된 메시지 나이, Worker 처리량, End-to-End 지연(접수 → 판정 DB 반영, DB 타임스탬프), 재처리·복구 시간, DLQ·dead_lettered 수, 중복 위반·알림·판정 수(목표 0), stale 처리 수, 아웃박스 적체(`pending_publish` 수·나이), KEDA 반응, 추가 비용.

비교 표의 1행은 **End-to-End 판별 지연과 유실 수**다. 접수 응답이 빨라진 것만으로 개선을 주장하지 않는다.

### 5.4 사전 등록 (테스트 전에 커밋)

- 통과 기준: 일반 API p95 < 500ms, 오류율 < 1%, 0.8C 이하에서 사진 성공률 ≥ 99%.
- 4.2 의 선택 규칙.
- 가설과 반대 결과 조항:
  - H1 "사진 부하가 일반 API 를 지연시킨다": 상한이 있으면 성립하지 않을 수 있다. 그러면 Phase 1 의 한계는 "과부하 시 사진 거절·유실(장비 재시도 없음)" 로 기록한다.
  - H2 "AI 장애가 일반 API 가용성에 영향을 준다": readyz 분리로 영향이 없을 것으로 예상한다. 없으면 Phase 1 분리의 성과로 기록한다.
  - H3 "과부하 시 탐지가 실패한다": S3 에서 측정. 거절이 0 이면 Phase 2 명분을 확장 비용·운영 유연성으로 옮기거나 범위를 줄인다.
- 결과: k6 요약 JSON, Prometheus 쿼리 CSV, 대시보드 PNG 를 `app:docs/measurements/phase1/` 에 커밋.

---

## 6. Phase 2 설계 — ADR 0019 초안: S3 + SQS 기반 비동기 판별

> 상태: **제안**. Phase 1 측정 결과를 보고 확정한다.

### 6.1 문제

Phase 1 에서 장비 요청은 판정이 끝날 때까지 HTTP 연결과 API 스레드를 잡는다. 판정 용량을 넘는 사진은 거절되고 장비에는 재시도가 없어 유실된다(크기는 Phase 1 측정으로 확인). 판정 용량은 사진이 오는 순간에 맞춰 이미 떠 있어야 한다.

### 6.2 두 경로로 나눈다: 상태 경로와 판별 경로

장비 요청에는 두 종류의 정보가 섞여 있다. "장비가 무엇을 보았다고 말하는가"(상태)와 "사진이 위반인가"(판별)다. **상태는 큐를 거치지 않고 DB 에 바로 반영하고, 판별만 큐로 보낸다.** 그래서 `left` 나 Heartbeat 는 AI 적체에 막히지 않는다.

| 경로 | 처리 | 대상 | AI 적체 영향 |
|---|---|---|---|
| **상태 경로 (동기, DB 만)** | API 가 요청 안에서 처리 | Heartbeat, 세션 시작(`arrived` 의 상태 부분), 출차 보고(`left` 의 상태 부분), 명령 수신 확인 | 없음 |
| **판별 경로 (비동기)** | S3 inbox + 작업 행 + SQS FIFO → Worker | 모든 사진의 위반 판별과 그 결과의 사건 반영 | 있음 (측정 대상) |

`left` 요청 하나는 두 경로를 모두 탄다.

1. 상태 경로(즉시): 세션을 `ended` 로 표시하고 `left_reported_at` 기록, 그 세션의 대기 중 명령(confirm) 취소, Heartbeat 응답의 `violation_active` 계산에 "출차 보고됨" 반영.
2. 판별 경로: 빈 자리 사진의 판별 작업을 큐에 넣는다. 사건 **종료**는 지금처럼 서버가 `no_vehicle` 을 확인했을 때만 한다(ADR 0005·0014 원칙 "서버가 판사다" 유지).

AI 가 오래 멈추면 사건은 "출차 보고됨, 판별 대기" 로 열려 있다. 관리자 화면에 이 상태를 보이게 한다. 장비 보고만으로 닫는 대체 규칙(예: 30분)은 원칙 변경이므로 **열린 질문**으로 남긴다(6.15).

끊김 판정(SIGNAL_LOST)은 Heartbeat 와 접수 시각(상태 경로)만 보므로 큐 지연의 영향을 받지 않는다(6.6).

### 6.3 MessageGroupId 단위 (D3)

FIFO 큐는 같은 그룹 안에서 앞 메시지가 끝나야 다음을 내준다. 그룹은 **순서가 필요한 상태의 소유자**와 같아야 한다. 너무 크면 서로 무관한 장비가 서로를 막고, 너무 작으면 같은 상태를 두 Worker 가 동시에 고친다.

| 후보 | 같은 그룹 안에서 막히는 범위 | 상태 소유자와의 관계 | 판단 |
|---|---|---|---|
| 회사 | 회사의 모든 주차장·장비 | 회사 단위 공유 상태 없음 | 기각. 독 메시지 하나가 회사 전체 판별을 멈춘다 |
| 주차장 | 주차장의 모든 장비 | 주차장 단위 공유 상태 없음 | 기각. 한 칸의 느린 사진이 옆 칸을 막는다 |
| 주차면 (zone) | 한 칸 | 사건 진행 상태는 **Machine 행**에 있다 [확인]. 한 장비가 두 칸을 보면 두 그룹이 같은 Machine 행을 동시에 고친다 | 지금은 장비 1대 = 1칸이라 machine 과 같다. 다중 칸 장비가 생기면 상태를 zone 으로 옮기는 별도 설계 필요 |
| **장비 (machine)** | 한 장비 | 상태 소유자와 정확히 일치 | **채택** |

- `MessageGroupId = "m-<machine_id>"`. 회사·주차장 ID 는 넣지 않는다(그룹 ID 는 순서 단위일 뿐이고, 의미 없는 내부 ID 만 둔다).
- `MessageDeduplicationId = <job_id>`. 5분 안의 중복 전송(재전송기 재시도)을 SQS 가 버린다.
- 처리량: FIFO 는 배치 없이 API 동작당 초당 300건 [확인 필요: 현재 한도]. 이 시스템의 사진 수(장비 수 × 시간당 수 장)와 비교해 충분하다.
- 그룹 막힘의 크기: 처리 중 Worker 가 죽으면 그 장비만 Visibility Timeout(60초)만큼 멈춘다. 독 메시지는 maxReceiveCount(3) × 60초 ≈ 3분 동안 그 장비만 막는다. 다른 장비는 영향 없음.

### 6.4 식별자: event_id, session_id, seq

| 식별자 | 만드는 곳 | 범위 | 용도 |
|---|---|---|---|
| `event_id` (UUID) | 장비, 사진·이벤트마다 | 전역 고유 | **중복 제거**. 재전송해도 같은 값. `UNIQUE(machine_id, event_id)` |
| `session_id` (UUID) | 장비, `arrived` 때 새로 | 진입 ~ 출차 한 번 | **오래된 결과 구분**. 세션이 바뀌면 이전 세션 결과는 현재 상태를 바꾸지 않음 |
| `seq` (정수) | 장비, 세션 안에서 1부터 | 세션 안 | **세션 안 순서**. arrived = 1, confirm·refresh 는 증가, left = 마지막 |
| `command_id` (UUID) | 서버 | 명령 하나 | confirm 명령의 중복 실행 방지, 수신 확인 |
| `job_id` | 서버 | 판별 작업 하나 | SQS 메시지 본문, 중복 전송 제거 |

`detected_at`(장비 시계)는 순서 판단에 쓰지 않는다. 표시·감사용이다. 장비 시계는 틀릴 수 있다.

**세션 순서**: 서버는 세션을 처음 본 시각(`sessions.first_received_at`, 서버 시계)으로 세션 사이 순서를 정한다. 장비가 재부팅하면 세션 카운터는 사라지지만, 점유가 다시 감지되면 새 `arrived` 와 새 `session_id` 로 시작하므로 문제가 없다.

### 6.5 순서 가드와 stale 판정

Machine 행에 `current_session_id`, `last_applied_seq` 를 둔다. Worker 의 반영 트랜잭션(6.8)은 장비 행을 `FOR UPDATE` 로 잠근 뒤 다음을 검사한다.

| 조건 | 결과 |
|---|---|
| `job.session_id` = 현재 세션 이고 `job.seq` > `last_applied_seq` | **반영**. `last_applied_seq = job.seq` |
| `job.session_id` = 현재 세션 이고 `job.seq` ≤ `last_applied_seq` | `stale`. 판정 결과는 `detection_results` 에 기록, 사건 상태는 안 바꿈 |
| `job.session_id` 가 현재 세션보다 먼저 시작된 세션 | `stale`. 단, 그 세션의 `left` 판별은 **그 세션이 연 사건**을 닫는 데만 쓴다(아래) |
| `job.session_id` 가 처음 보는 세션 (상태 경로가 먼저 등록했어야 함) | 상태 경로가 접수 때 세션을 등록하므로 정상 흐름에서는 생기지 않는다. 생기면 `stale` + 경고 로그 |

`left` 판별의 예외: 장비 재부팅으로 세션 A 가 끝나기 전에 세션 B 가 시작될 수 있다. 세션 A 의 사건이 열려 있고 그 뒤 세션 B 가 같은 차를 계속 보고 있다면, A 의 `left` 사진으로 사건을 닫으면 안 된다. 그래서 **left 판별이 사건을 닫는 조건은 "그 사건이 현재 세션의 것일 때"** 로 제한한다. 지금 코드의 "left + no_vehicle 이면 1회로 종료" 규칙은 현재 세션 안에서만 적용된다.

**순서를 큐 단계에서도 지킨다**: 같은 장비의 더 오래된 작업이 아직 `pending_publish` 이면, 새 작업도 즉시 전송하지 않고 `pending_publish` 로 둔다. 재전송기는 장비마다 `seq` 순서로 보낸다. 그래서 SQS 전송 실패가 순서 뒤바뀜으로 이어지지 않는다. 가드는 그래도 남는 경우(수동 redrive, 재부팅)를 위한 두 번째 방어선이다.

### 6.6 시각 기준 변경 (코드에서 확인한 위험)

`record_verdict()` 는 끊김 판정, `last_detection_at`, `last_seen_at`, 종료 시각에 `utcnow()` 를 쓴다 [확인]. 비동기에서는 이것이 **처리 시각**이 된다.

- 변경: `record_verdict(..., observed_at=job.received_at)` 로 **API 접수 시각**을 넘긴다. `last_detection_at`, `last_seen_at`, 종료 시각은 접수 시각을 쓴다.
- 끊김 판정은 상태 경로에서 갱신하는 "마지막 신호 시각"(접수 시각 또는 Heartbeat 중 늦은 것)으로 한다. 큐 지연이 10분을 넘어도 정상 장비가 SIGNAL_LOST 로 닫히지 않는다.
- Phase 1 동기 경로에서는 접수 시각 ≈ 처리 시각이므로 동작이 바뀌지 않는다. 기존 테스트로 회귀를 확인한다.

### 6.7 아웃박스: 작업 행, 재전송기, 상태 전이

**테이블** [제안]

```text
detection_jobs
  id (UUID, job_id)            machine_id        event_id (UNIQUE with machine_id)
  session_id   seq   event     detected_at (장비)  received_at (서버)
  s3_key       status          publish_attempts   next_publish_at   published_at
  claim_attempt (fencing)      lease_until        worker_id
  result_id → detection_results   violation_id (이 작업이 만든 위반)   effects_status
  last_error   created_at   updated_at
detection_results   (job_id UNIQUE, verdict, window, stickers, elapsed_ms, model_version, applied: bool)
device_sessions     (session_id PK, machine_id, first_received_at, status active|ended|superseded, left_reported_at)
device_commands     (command_id PK, machine_id, session_id, type, due_at, expires_at, status, source_job_id,
                     UNIQUE(machine_id, session_id, type) WHERE status IN (pending, delivered))
```

**상태 전이**

```text
            (API 접수 트랜잭션)
                  │
                  ▼
           pending_publish ──(재전송기/API 즉시 전송 성공)──▶ queued
              ▲      │                                        │
   (수동 redrive)    └─(Worker 가 먼저 받음: 전송은 됐는데 상태 갱신만 실패)─┐
              │                                               ▼          ▼
        dead_lettered ◀──(DLQ 감시)── processing ◀────(claim)──────────────
                                        │   ▲
                                        │   └─(lease 만료 뒤 재전달 시 claim 재허용)
             ┌──────────────┬───────────┼───────────────┐
             ▼              ▼           ▼               ▼
           done           stale      rejected      (트랜잭션 실패 → 그대로 processing, 재전달 대기)
     (사건 반영됨)    (가드에 걸림)  (영구 오류: 디코드 불가, inbox 사진 없음)

effects_status (done 이고 이 작업이 위반을 만들었을 때만): pending → done | failed
```

| 상태 | 의미 | 누가 바꾸나 |
|---|---|---|
| `pending_publish` | DB 에는 있고 SQS 에 아직 보냈다고 확인되지 않음 | API(생성), 재전송기 |
| `queued` | SQS 전송 성공 확인 | API 즉시 전송 또는 재전송기 |
| `processing` | Worker 가 선점. `lease_until` 까지 | Worker |
| `done` | 판정과 사건 반영이 한 트랜잭션으로 commit | Worker |
| `stale` | 판정은 기록, 사건 상태는 안 바꿈 | Worker |
| `rejected` | 다시 해도 안 되는 오류 | Worker |
| `dead_lettered` | maxReceiveCount 초과로 DLQ 에 감 | 재전송기의 DLQ 감시 |

**재전송기 (outbox-relay)** [제안]

| 항목 | 설계 |
|---|---|
| 형태 | Deployment 1개, API 이미지, 명령 `flask outbox-relay`. CPU 50m / 메모리 128Mi |
| 주기 | 2초마다 루프 |
| 일 1: 발행 | `pending_publish` 이고 `next_publish_at ≤ now` 인 작업을 `FOR UPDATE SKIP LOCKED` 로 최대 50개. **장비별 seq 오름차순**, 장비마다 가장 오래된 것부터. SendMessage 성공 → `queued`. 실패 → `publish_attempts+1`, `next_publish_at` = 지수 백오프(1, 2, 4 … 최대 60초). 포기하지 않는다. `pending_publish` 의 최대 나이 > 5분이면 경보 |
| 일 2: DLQ 감시 | DLQ 를 읽어 해당 작업을 `dead_lettered`(+ 마지막 오류)로 바꾸고 DLQ 메시지는 지운다. **DB 가 진실의 원천**이고 DLQ 는 운반 수단이다. redrive = 작업을 `pending_publish` 로 되돌리는 관리 명령 `flask jobs redrive <id|--all-dead>` |
| 일 3: 부수 효과 마무리 | `done` 이고 `effects_status = pending` 이며 5분 지난 작업의 증거 사진 복사·알림을 다시 실행(멱등) |
| 일 4: 정리 | 보존 기간이 지난 작업 행 삭제(6.12) |
| 다중 실행 | `SKIP LOCKED` 로 2개가 동시에 돌아도 같은 작업을 두 번 잡지 않는다. 중복 전송이 생겨도 dedup ID 와 claim 이 막는다 |
| 장애 영향 | 재전송기가 죽어도 API 즉시 전송 경로는 동작한다. 재전송기는 실패분 회수와 정리만 맡는다 |

API 는 접수 트랜잭션 commit 뒤 **즉시 전송을 한 번 시도**한다(지연을 줄이는 최적화). 실패하거나 같은 장비의 앞 작업이 대기 중이면 재전송기에 맡기고 202 를 준다.

### 6.8 Worker 처리 절차

```text
1. ReceiveMessage (long poll 20초, 최대 1건)
2. claim:  UPDATE detection_jobs SET status='processing', claim_attempt=claim_attempt+1,
             lease_until=now()+60s, worker_id=:me
           WHERE id=:job AND (status IN ('pending_publish','queued')
                              OR (status='processing' AND lease_until < now()))
           RETURNING claim_attempt
   - 0행이고 status ∈ {done, stale, rejected}: (done 이고 effects pending 이면 효과 마무리) → DeleteMessage → 끝
   - 0행이고 다른 Worker 가 유효한 lease 보유: 아무것도 안 함(메시지는 다시 보인다)
3. S3 GET inbox 사진 (없으면 rejected: missing_image → DeleteMessage)
4. 추론 (트랜잭션 밖. 오래 걸림). 처리가 길어지면 ChangeMessageVisibility 로 연장
   - InvalidImageError → rejected → DeleteMessage
   - InferenceError → 메시지를 지우지 않음 → 재전달 → 3회 뒤 DLQ
5. 반영 트랜잭션:
   - 장비 행 FOR UPDATE
   - 작업 행 재확인: status='processing' AND claim_attempt = 내가 받은 값 (fencing: lease 가 만료돼
     다른 Worker 가 다시 잡았다면 내 결과는 버린다)
   - detection_results INSERT (job_id UNIQUE)
   - 순서 가드 (6.5) → 반영 또는 stale
   - 반영이면 record_verdict(observed_at=received_at), 새 위반이면 job.violation_id, effects_status='pending',
     violation.image_status='pending', confirm 이 필요하면 device_commands INSERT (6.9)
   - job.status = done | stale
   - COMMIT
6. 부수 효과 (effects_status='pending' 일 때): inbox → violations/ CopyObject(같은 키 덮어쓰기 = 멱등),
   image_status=UPLOADED, notify_violation (멱등) → effects_status='done'
7. DeleteMessage
```

Worker = SQS 소비 + `YoloInference` + 사건 반영. Phase 1 inference 이미지에 앱 코드를 더한 이미지. 프로세스 1, 동시 처리 1, 락 유지. Worker 는 DB 자격 증명이 필요하다(Phase 1 inference 와 다른 점. ADR 에 기록).

### 6.9 confirm 타이밍 분석 (D4)

**지금 (동기)** [확인]: arrived 사진 응답을 받은 시각 t₀ + s (s = 추론 포함 응답 시간, 수 초)에서 60초 뒤 confirm. 두 사진 간격 ≈ 60초.

**Heartbeat 로 바꿀 때의 문제**: 서버가 판정을 마치는 시각을 t₀ + q (q = 큐 대기 + 추론)라 하자. 장비는 그 뒤 첫 Heartbeat 에서야 명령을 안다. Heartbeat 위상은 사진과 무관하므로 첫 Heartbeat 는 [t₀+q, t₀+q+60) 어디든 올 수 있다.

| 방식 | confirm 촬영 시각 | 문제 |
|---|---|---|
| ① 명령 받자마자 촬영 | [t₀+q, t₀+q+60) | 받는 시점이 t₀ 직후일 수 있어 **두 사진이 몇 초 차이로 찍힌다**. "잠깐 선 차" 를 거르는 60초 간격의 의미가 사라진다 |
| ② 서버가 `due_at = 접수시각 + 60초` 를 주고, 장비는 max(due_at, 받은 시각) 에 촬영 [채택] | [t₀+60, t₀+q+60) | 늦음 = max(0, q + U − 60), U ~ [0,60). **q ≤ 60 이면 최대 늦음 = q, 평균 q²/120초.** q = 5초면 평균 약 0.2초, 최대 5초 |

결론: **정상 부하(q 가 수 초)에서는 Heartbeat 60초 주기를 그대로 써도 confirm 이 지금과 거의 같은 시각에 찍힌다.** 적체로 q 가 커지면 늦음도 q 만큼 커진다. 늦음 자체가 사건 판정을 틀리게 하지는 않는다. 2회 확정 규칙은 어떤 이벤트의 위반 사진이든 세므로 [확인], confirm 이 끝내 안 와도 10분 뒤 `refresh` 가 두 번째 목격이 된다. **confirm 은 정확성이 아니라 지연을 줄이는 장치**다.

**선택 개선 (측정 뒤 결정)**: 장비가 "사진을 보냈고 아직 그 세션의 판정 상태를 모르는" 동안만 Heartbeat 를 15초로 줄인다(최대 3분). 적체 때 늦음 상한이 q − 45 수준으로 준다. Heartbeat 는 본문이 없어 비용이 작다.

**시계 차이**: 서버는 `due_at`(서버 시각)과 `server_time` 을 함께 보낸다. 장비는 `로컬 촬영 시각 = 로컬 now + (due_at − server_time)` 으로 바꾼다. 장비 시계가 틀려도 상대 간격은 맞다.

### 6.10 명령 프로토콜과 장비 동작 (D4)

**Heartbeat 요청과 응답** [제안]

```json
POST /device/heartbeat
{ "session_id": "…|null", "occupied": true, "last_seq": 3,
  "acked_commands": ["cmd-…"] }

200
{ "ok": true, "server_time": "2026-…Z",
  "violation_active": false,
  "commands": [
    { "command_id": "cmd-…", "type": "capture_confirm", "session_id": "…",
      "due_at": "…Z", "expires_at": "…Z" } ] }
```

**명령 생명 주기**: `pending`(Worker 가 생성) → `delivered`(Heartbeat 응답에 실림) → `fulfilled`(그 command_id 를 단 confirm 사진이 접수됨) | `cancelled`(세션 종료, left, 새 세션) | `expired`(`expires_at` 경과).

| 상황 | 처리 |
|---|---|
| **중복 전달** | 서버는 `fulfilled` 전까지 매 Heartbeat 에 같은 명령을 다시 싣는다. 장비는 실행한 command_id 를 최근 50개 파일에 저장하고 같은 ID 는 무시한다 |
| **중복 생성** | Worker 재전달로 명령을 두 번 만들려 해도 `UNIQUE(machine, session, type)`(활성 상태) 가 막는다 |
| **장비가 confirm 전에 출차** | 장비: 세션이 끝나면 그 세션 명령을 버리고 left 를 보낸다. 서버: left 상태 경로가 명령을 `cancelled` 로 |
| **세션이 다른 명령** | 장비는 `session_id` ≠ 현재 세션인 명령을 버리고 `acked_commands` 로 알린다. 서버는 `cancelled` |
| **늦은 전달** | `expires_at = due_at + 9분`(refresh 10분 전). 지나면 장비는 실행하지 않는다. 그 뒤엔 refresh 가 두 번째 목격 |
| **장비 재부팅·재접속** | 메모리 일정이 사라진다. 차가 그대로면 점유 재감지 → 새 세션 arrived → 서버가 이전 세션 명령 `cancelled`, 이전 세션 `superseded`. 새 arrived 의 위반 판정이 기존 pending 횟수에 더해져(지금 규칙과 같다) 확정될 수 있다 |
| **Heartbeat 실패** | 명령은 서버에 남아 다음 Heartbeat 에 다시 온다 |
| **confirm 업로드 실패** | 아래 재시도 정책. 같은 event_id, 같은 command_id |
| **LED** | `violation_active` 로 켜고 끈다. 갱신은 최대 Heartbeat 한 주기 늦다 |

**장비 재시도와 백오프** [제안]

| 항목 | 정책 |
|---|---|
| 로컬 아웃박스 | 보낼 사진과 메타데이터(event_id, session_id, seq, event, command_id, detected_at)를 SD 카드 디렉터리에 먼저 쓴다. 최대 20건 / 50 MB. 넘치면 가장 오래된 `refresh` 부터 버린다(`arrived`·`confirm`·`left` 는 우선 보존) |
| 전송 순서 | 한 번에 하나, seq 순서. 앞 건이 성공해야 다음 건 (장비 단에서 순서 보장) |
| 재시도 대상 | 연결 실패, 타임아웃, 5xx, 429 |
| 재시도 안 함 | 400, 401, 403, 413 (로그 남기고 버림) |
| 성공 | 202(새 접수), 200(중복: 같은 job_id) 모두 성공으로 처리하고 파일 삭제 |
| 백오프 | full jitter 지수 백오프: 대기 = random(0, min(60초, 2초 × 2^시도)). `Retry-After` 가 있으면 그 값 이상 |
| 포기 기준 | `refresh`·`confirm`: 촬영 후 10분이 지나면 버림(다음 refresh 가 대신한다). `arrived`·`left`: 촬영 후 30분까지 재시도. 이후 버리고 로그 |
| Heartbeat | 재시도 없음. 다음 주기에 보낸다 (Heartbeat 가 곧 재시도) |
| 재부팅 | 로컬 아웃박스 파일이 남아 있으면 이어서 보낸다. 세션이 끝난 사진도 보낸다(서버 가드가 stale 로 처리) |

### 6.11 부분 실패 검토

| 실패 지점 | 남는 상태 | 결과 | 복구 |
|---|---|---|---|
| S3 PUT 실패 | 없음 | 503 | 장비 재시도(같은 event_id) |
| **S3 성공 → DB INSERT 실패** | inbox 에 객체 1개 | 503 | 장비 재시도. inbox 키를 `inbox/m-<machine>/<yyyy/mm/dd>/<event_id>.<ext>` 로 **event_id 에서 결정**하므로 재시도는 같은 키를 덮어쓴다. 끝내 재시도가 없으면 수명 주기가 7일 뒤 지움 |
| DB INSERT 가 중복(UNIQUE 위반) | 기존 작업 | 200 + 기존 job_id | 없음 (정상) |
| **DB commit → SQS 전송 실패** | `pending_publish` | 202 | 재전송기가 백오프로 계속 전송 |
| SQS 전송 성공 → `queued` 갱신 실패 | `pending_publish` 인데 메시지는 있음 | 202 | Worker claim 이 `pending_publish` 도 허용. 재전송기의 중복 전송은 5분 안이면 dedup, 그 뒤면 claim 이 done 을 보고 삭제 |
| 202 응답 유실 | 작업 있음 | 장비는 실패로 봄 | 장비 재시도 → 200 중복 응답 |
| Worker 가 추론 중 종료 | `processing`, lease | — | lease·Visibility 만료 뒤 재전달 → claim 재허용 |
| 추론 일시 오류 | `processing` | — | 메시지를 지우지 않음 → 재시도 → 3회 뒤 DLQ → `dead_lettered` |
| 디코드 불가, inbox 사진 없음 | `rejected` | — | 메시지 삭제. 재시도로 해결되지 않으므로 DLQ 를 거치지 않는다 |
| 반영 트랜잭션 실패 (DB 장애) | `processing` | — | 롤백. 재전달 때 다시 추론(계산 낭비뿐, 상태 오염 없음) |
| lease 만료 뒤 늦게 끝난 Worker | 다른 Worker 가 재선점 | — | fencing(`claim_attempt`)으로 늦은 Worker 의 commit 이 0행 → 결과 버림 |
| **commit 성공 → 증거 복사·알림 전 종료** | `done`, effects `pending` | — | 재전달 시 claim 이 효과 마무리, 또는 재전송기 일 3 |
| 증거 사진 복사 실패 반복 | effects `failed`, image_status FAILED | 사건은 유지 | 지금의 `IMAGE_UPLOAD_FAILED` 이력 패턴 그대로 |
| 알림 실패 | 알림 행 실패 상태 | 사건은 유지 | 지금의 `NOTIFY_FAILED` 이력 + 멱등 재시도 |
| **처리 완료 → DeleteMessage 실패** | `done`, 메시지 남음 | — | Visibility 만료 뒤 재전달 → claim 이 done 을 보고 DeleteMessage. 같은 그룹은 그 동안(최대 60초) 멈춘다 |
| SQS 전체 장애 | `pending_publish` 적체 | 접수는 계속 202 | 복구 뒤 재전송기가 seq 순서로 배출 |
| 재전송기 장애 | 실패분 회수 중단 | 즉시 전송 경로는 정상 | Deployment 재시작. `pending_publish` 나이 경보 |
| RDS 장애 | — | 접수 503, 관리자 API 도 실패 | RDS 는 단일 장애점 (Phase 1·2 공통, 범위 밖으로 명시) |

### 6.12 보존 정책

| 데이터 | 위치 | 보존 [제안] | 근거 |
|---|---|---|---|
| **AI 판별용 임시 사진** | S3 `inbox/` | **7일** 수명 주기 만료 | DLQ·`dead_lettered` 조사와 redrive 기간. 비위반·완료 사진도 Worker 가 지우지 않고 수명 주기에 맡긴다(삭제를 처리 경로에 넣지 않아 재시도·멱등이 단순) |
| **확정 위반 증거** | S3 `violations/` (기존 키 규칙) | **90일** (`IMAGE_RETENTION_DAYS`) 수명 주기 만료를 실제로 건다 | 지금은 만료 시각을 기록만 하고 지우지 않는다 [확인]. 만료 뒤 앱은 `image_status=EXPIRED` 표시(야간 작업) |
| 보류 증거 (이의 제기 등) | S3 `evidence-hold/` | 수동 해제 전까지 | 수명 주기 대상 접두어 밖으로 옮겨 보존 [제안, 필요 시] |
| 판별 결과 메타데이터 | DB `detection_results` | 90일 | 판정 근거(박스, 신뢰도) 감사 |
| 작업 행 | DB `detection_jobs` | `done`·`stale`·`rejected` 는 30일 뒤 삭제, `dead_lettered` 는 해결 때까지 | |
| 세션·명령 | DB | 30일 | |
| DLQ | SQS | 메시지 보관 4일 (재전송기가 즉시 DB 로 옮기므로 짧아도 된다) | inbox 7일보다 짧게 |

S3 버전 관리 설정은 `storage_s3.tf` 에서 찾지 못했다 [확인]. 켜져 있다면 이전 버전 만료 규칙도 필요하다 [확인 필요].

Phase 1 은 지금처럼 위반 확정 사진만 저장하고 inbox 가 없다.

### 6.13 전 과정 멱등성 검증표

| 산출물 | 중복이 생길 수 있는 원인 | 막는 장치 | 검증 테스트 |
|---|---|---|---|
| 접수 (작업 행) | 장비 재시도, 응답 유실 | `UNIQUE(machine_id, event_id)` → 같은 job_id 로 200 | 같은 요청 2회 → 작업 1개 |
| inbox 사진 | 재시도 | event_id 로 결정되는 키 | 재시도 후 객체 1개 |
| SQS 메시지 | 재전송기 재시도, 상태 갱신 실패 | dedup ID = job_id (5분), 그 이상은 claim | 같은 job 메시지 2개 → 반영 1회 |
| 선점 | 동시 수신, lease 만료 | 조건부 UPDATE + `claim_attempt` fencing | 두 Worker 동시 처리 → 1개만 commit |
| 판정 결과 | 재처리 | `detection_results.job_id UNIQUE` | 재전달 후 결과 1행 |
| 사건 진행 상태 (카운터) | 재처리, 순서 뒤바뀜 | 장비 행 잠금 + 순서 가드 + 작업 `done` 과 같은 트랜잭션 | 같은 사진 2회 → pending 1 증가 |
| 위반 생성 | 재처리, 경합 | 위와 같음 + 기존 활성 위반 유니크 인덱스 [확인] | 위반 확정 사진 재전달 → 위반 1건 |
| 위반 이력 줄 | 재처리 | 반영 트랜잭션 안에서만 추가 | 재전달 후 DETECTED 1줄 |
| 증거 사진 | 효과 재실행 | 위반 ID 로 결정되는 키 덮어쓰기 + `effects_status` | 효과 2회 → 객체 1개, 상태 UPLOADED |
| 알림 | 효과 재실행 | 기존 (violation, admin, channel) 유니크 [확인] | 효과 2회 → 알림 행 1개 |
| 사건 종료 | left 재처리, 이전 세션 left | 가드 + "현재 세션의 사건만 닫음" | 이전 세션 left 재전송 → 현재 사건 열린 채 |
| confirm 명령 | 재처리 | 활성 명령 UNIQUE(machine, session, type) | 재전달 → 명령 1개 |
| confirm 실행 | 명령 중복 전달 | 장비의 실행 ID 기록 + 사진의 command_id | 같은 명령 3회 수신 → 사진 1장 |

테스트 방법: 단위·통합 테스트에서 각 단계 직후에 예외를 주입(pytest monkeypatch)하고, 같은 메시지 재전달·순서 뒤바꿈·두 Worker 동시 실행을 재현해 **행 수와 상태**를 단언한다. 운영 테스트는 S5(d)(e)(f)(g).

### 6.14 KEDA, 비용, 감수할 것

- KEDA `aws-sqs-queue` 스케일러, 목표 대기 수/Pod 는 E0 처리량에서 계산. **min 1**(0 이면 첫 사진이 이미지 Pull + 모델 로딩을 기다린다). max = 노드 수. KEDA 는 Argo CD Application, SQS 권한은 Pod Identity.
- 관리자 API 는 SQS·Worker 에 의존하지 않는다. Worker 0개여도 관리자 기능 정상(S5 로 검증).

| 비용 항목 | 예상 [추정, 요금표 확인 필요] |
|---|---|
| SQS FIFO 요청 | 월 100만 건 무료 범위 안 |
| SQS VPC Endpoint | AZ 2개 × 시간 요금 → 월 십수~20달러대. **NAT 경유 권장** |
| S3 inbox | 1 GB 미만 |
| 재전송기 Pod, KEDA | 노드 안 메모리 수백 MB, 추가 비용 없음 |

감수할 것: 장비 응답이 즉시 판정을 주지 않는다(LED·confirm 이 Heartbeat 한 주기 늦을 수 있음). 테이블 4개, 상태 기계, 운영 절차(redrive) 추가. End-to-End 지연은 동기보다 늘 수 있고 이를 비교 표 1행에 둔다.

### 6.15 열린 질문

1. AI 가 오래 멈췄을 때 장비의 출차 보고만으로 사건을 닫는 대체 규칙을 둘지(ADR 0005·0014 원칙 변경).
2. 적응형 Heartbeat(판정 대기 중 15초)를 Phase 2 공식 구성에 넣을지. S1 의 confirm 늦음 측정 뒤 결정.
3. 동기 엔드포인트를 Phase 2 이후에도 남길지(비교 재현용 설정 플래그로 남기는 것을 권장).

---

## 7. ADR 0018 초안 요지 — Phase 1 동기식 서비스 분리

> 원문: `docs/adr/0018-phase1-sync-inference-service.md`. Phase 2 는 `docs/adr/0019-phase2-async-sqs-judgement.md`. 사전 등록: `docs/measurements/phase1/README.md`.

- **결정**: inference 를 별도 Deployment·Service 로 분리. API 는 HTTP 어댑터로 동기 호출하고 타임아웃·재시도 상한·동시 상한을, AI 서비스는 대기 상한을 공식 구성으로 둔다. 상한 값은 4.2 의 선택 규칙으로 정한다. 락 유지.
- **이유**: 자원·권한·확장 정책 분리, AI 장애가 관리자 API readiness 를 끌어내리지 않게. 앱이 이미 포트 구조라 변경이 작다.
- **기각한 대안**: 같은 Pod 의 사이드카(자원·확장 분리 안 됨), TorchServe/Triton(범위 밖), 바로 비동기(측정 근거 없이 복잡도 추가).
- **결과**: 이미지 2개, 홉 1개, 매니페스트 약 2배. 동기 대기라는 제약은 남는다 → 부하 테스트로 크기를 잰다.

---

## 8. 로드맵

| 단계 | 내용 | 왜 / 해결하는 문제 | 난이도 | AWS 비용 |
|---|---|---|---|---|
| 0. 문서 보존 | 공개 문서 선택 추적 + 문서 전용 커밋 (10장, 2026-10-30 완료) | 설계 유실 방지, 사전 등록 증거 | 낮음 | 0 |
| 1. Prototype 보존 | 세 레포 태그 `prototype-v1`, `docs/architecture/prototype.md`, infra 문서 명칭 주석 | 이력 보존, 명칭 충돌 해소 | 낮음 | 0 |
| 2. 재측정 | 재구축 직후 노드 요청 합, api 이미지 RSS(로컬), RDS `max_connections` | 3.6·3.7 값 확정 | 낮음 | 클러스터 켜는 시간 |
| 3. 앱 Phase 1 | 9.1 의 app 파일 | 분리 본체 | 중간 | 0 |
| 4. CI | inference 이미지 | 배포 | 낮음 | 0 |
| 5. infra | ECR, Pod Identity. 부하 노드 그룹은 **별도 승인 뒤** | 최소 권한, 측정 격리 | 낮음 | 월 1달러 미만 + 테스트 시간 |
| 6. config | 9.1 의 config 파일 | 배포 | 중간 | 0 |
| 7. 사전 등록 + E0~E3, T1~T3, X1 | 공식 구성 확정 | 근거 기반 값 | 낮음 | 세션당 1~3달러 |
| 8. S0~S5 + Phase 1 보고서 | 비교 A/B | Phase 1 의 실제 한계 | 중간 | 세션당 1~3달러 |
| 9. ADR 0019 확정 | 측정 결과로 6장 수정 | 근거 기반 결정 | 낮음 | 0 |
| 10. Phase 2 구현 | SQS FIFO + DLQ, inbox·증거 수명 주기, 테이블 4개, 재전송기, Worker, 장비 프로토콜, KEDA | 비동기 전환 | **높음** | 무료 범위 + NAT |
| 11. Phase 2 테스트 + 비교 보고서 | 같은 프로필 | 개선과 비용을 함께 증명 | 중간 | 세션당 1~3달러 |

---

## 9. Phase 1 착수 전 최종 정리 (v3)

### 9.1 변경 예정 파일 (Phase 1, 아직 아무것도 고치지 않음)

**app (`capston/`)**

| 파일 | 변경 | 구현 단계 (9.6) |
|---|---|---|
| `backend/app/adapters/inference.py` | `HttpInference` 추가, `build_inference` 에 `http` 분기, `YoloInference` 에 `TORCH_NUM_THREADS` 손잡이 | 2 |
| `backend/app/config.py` | `INFERENCE_URL`, `AI_CLIENT_*`(마감, 연결, 재시도, 동시 상한), `INFERENCE_MAX_WAITING`, `TORCH_NUM_THREADS` | 2 |
| `backend/app/api/health.py` | `remote` 상태 처리(API readiness 는 DB 만) | 2 |
| `backend/app/metrics.py` | AI 클라이언트 메트릭 4개, 추론 거절 1개 | 2 |
| `backend/tests/test_inference_http.py` (신규), `test_health.py`·`test_metrics.py` | 타임아웃·재시도·동시 상한·예외 매핑, readyz | 2 |
| `backend/inference_service/` (신규) | 앱 팩토리, `/v1/analyze`, `/healthz`, `/readyz`, 대기 상한 | 3 |
| `backend/wsgi_inference.py`, `backend/gunicorn_inference.conf.py` (신규) | AI 서비스 진입점과 웜업 훅 | 3 |
| `backend/gunicorn.conf.py` | 로컬 추론일 때만 웜업 | 3 |
| `backend/tests/test_inference_service.py` (신규) | 대기 상한, 상태별 응답, API↔AI 계약 | 3 |
| `backend/Dockerfile` | 멀티 타깃 `api`(torch 없음) / `inference` | 4 |
| `docker-compose.yml` | inference 서비스 추가, backend `INFERENCE_BACKEND=http` | 4 |
| `.github/workflows/build-push.yml` | matrix 에 inference(타깃 지정), config 레포 태그 3줄 | 6 |
| `docs/architecture/prototype.md` (신규) | Prototype 구조·설정·측정 기록 | 1 |
| `docs/adr/0018…`, `docs/measurements/phase1/README.md` | 측정 뒤 값·결과 채움 | 9, 11 |

**config (`parking-guardian-config/`)**

| 파일 | 변경 | 단계 |
|---|---|---|
| `apps/parking-guardian/backend-deployment.yaml` | initContainer·모델 볼륨 제거, 요청 200m/256Mi(S0 뒤 재조정), threads 8, required anti-affinity → **preferred 2개**(api 끼리 100, inference 노드 피하기 50) | 7 |
| `backend-hpa.yaml`, `backend-pdb.yaml` | 기준·안정화 120초, PDB minAvailable 1 | 7 |
| `inference-deployment.yaml`, `inference-service.yaml`, `inference-hpa.yaml`, `inference-pdb.yaml`, `inference-podmonitor.yaml`, `serviceaccount-inference.yaml` (신규) | AI 서비스 한 벌. inference 끼리 required anti-affinity, Secret 마운트 없음 | 7 |
| `serviceaccount-backend.yaml` | 주석(모델 권한 제거) | 7 |
| `kustomization.yaml` | resources, images 3개, `ECR_INFERENCE` replacements, `inference-config` 생성기, `INFERENCE_BACKEND=http`, `INFERENCE_URL` | 7 |
| `config.env`, `config.env.example`, `scripts/export-config-env.py` | `ECR_INFERENCE` | 7 |
| `platform/monitoring/config/dashboards/build_dashboards.py` + JSON 3개 | 패널 재구성 | 7 |
| `apps/loadtest/` (신규), `argocd/apps/loadtest.yaml` (신규, **수동 sync**) | k6 스크립트 ConfigMap, Job(nodeSelector + toleration) | 9 |
| `README.md` | 구조 갱신, `kubectl apply -k` 잔재 정리 | 7 |

**infra (`parking-guardian-infra/`)**

| 파일 | 변경 | 단계 |
|---|---|---|
| `terraform/stacks/data/storage_ecr.tf`, `outputs.tf` | 저장소 집합에 `inference` (CI 푸시 권한은 자동 포함 [확인]) | 5 |
| `terraform/stacks/cluster/iam.tf`, `variables.tf` | inference 역할(모델 읽기) + Pod Identity 연결, backend 역할에서 모델 권한 제거 | 5 |
| `terraform/stacks/cluster/nodes.tf`, `variables.tf`, `terraform.tfvars.example` | `loadgen` 노드 그룹(desired 0, 라벨·taint), main `node_max_size` 3 → 4 — **별도 승인 뒤** | 10 |
| `docs/phase1/architecture.md` (로컬 전용) | 머리말에 "이 문서의 Phase 1 = 새 명칭의 Prototype" | 1 |

**device**: Phase 1 변경 없음.

### 9.2 예상 리소스 배치 (재측정 전 잠정)

**운영 기본 (main 3대, 안 A)**

| 노드 | Pod | CPU 요청 합 / 1,930m |
|---|---|---|
| N1 | inference, backend(api), DaemonSet | 1,850m |
| N2 | inference, backend(api), DaemonSet | 1,850m |
| N3 | frontend, Argo CD, 모니터링, ESO, ALB 컨트롤러, (surge 자리) | 약 800m |

재측정에서 노드 시스템 요청이 700m 를 넘으면 inference 요청을 900m 로 낮춘다(안 C).

**측정 세션 (main 4대 + 부하 1대, 안 E, 승인 뒤)**

| 노드 | 비교 A (고정: api 2 + inference 2) | 비교 B (HPA 최대) |
|---|---|---|
| N1 | inference | inference |
| N2 | inference | inference |
| N3 | api, 플랫폼·모니터링 | api, 플랫폼·모니터링, (api 3번째 또는 surge) |
| N4 | api | api, inference 3번째 (공존 발생, 기록) |
| L1 | k6 | k6 |

### 9.3 테스트 전략

| 층 | 무엇을 | 언제 |
|---|---|---|
| 단위 | `HttpInference` 의 타임아웃·재시도·동시 상한·예외 매핑, AI 서비스 대기 상한, readyz | 단계 2·3. 로컬 pytest (CI 에 테스트 단계가 없다 [확인]. 추가 여부는 별도 결정) |
| 계약 | API ↔ AI 서비스: 같은 프로세스에서 AI 앱의 test client 로 요청·응답 형식 | 단계 3 |
| 회귀 | 기존 57개 테스트, SQLite·PostgreSQL | 단계 2~4 |
| 로컬 통합 | docker-compose 로 api + inference + PostgreSQL, PC 시뮬레이터로 sample_b·sample_c, inference 컨테이너 중지 시 관리자 API 정상 | 단계 4 |
| 클러스터 확인 | readyz, 장비 사진 2장, inference 0개로 줄였을 때 관리자 API 정상, 대시보드 쿼리 전부 데이터 | 단계 8 |
| 실험 | E0~E3, T1~T3 | 단계 9 |
| 부하 | S0~S5, X1, (선택) P1, 비교 A/B, 각 3회 | 단계 10 |

### 9.4 비용 [추정]

| 항목 | 예상 |
|---|---|
| 클러스터 운영 (main 3대 + 플랫폼) | 시간당 약 0.52달러 (10-28 기준), 쓸 때만 켬 |
| ECR `inference` | 월 수십 센트 이하 |
| 측정 세션 추가 노드 (main 1 + 부하 1) | 테스트 시간에만. 노드당 시간당 약 0.1달러 수준 [확인 필요: 서울 요금, Free plan 허용] |
| Phase 1 실험·테스트 전체 | 세션 2~3회 × 3~4시간 → 총 수 달러 |
| 재구축 | 매 세션 재구축·해체 약 40분이 과금 시간에 포함 |

### 9.5 주요 위험

| 위험 | 영향 | 대응 |
|---|---|---|
| N1·N2 CPU 요청 여유 약 80m | 시스템 요청이 조금만 늘어도 Pending, 3대에서 HPA 상한 충돌 | 3.7.1: 운영은 안 A/C, 측정은 안 E. 재측정으로 확정 |
| 공존 시 CPU 경합 | 측정 해석 오염 | 측정은 4대 분리, P1 로 경합 크기 측정 |
| api 이미지 메모리 미측정 | 요청값 오류 | 단계 4 에서 로컬 측정 |
| RDS 연결 수 | api 스레드 8 × Pod 3 + overflow 가 한도 초과 가능 | 단계 0 에서 `max_connections` 확인, 풀 크기 명시 |
| vCPU 한도 / Free plan | 측정 세션 5대(10 vCPU)가 막힐 수 있음 | 단계 0 에서 조회(읽기 전용). 막히면 E 시리즈만 3대에서 |
| m7i-flex 성능 변동 | 측정 흔들림 | steal 기록, 3회 반복, 같은 시간대 |
| torch 스레드 안전성 미검증 | 락 제거 불가 | 락 유지, 동시성은 Pod 수로만 |
| gunicorn graceful timeout 30초 vs grace 130초 | S5(c) 결과 왜곡 | 단계 0 에서 확인, 필요하면 명시 |
| api→inference AZ 교차 | 지연·비용 소폭 | 측정, 필요하면 `trafficDistribution` |
| CI 순서 | ECR `inference` 가 없는데 CI 가 먼저 바뀌면 빌드 실패 | 단계 5(ECR) 다음에 단계 6(CI) |
| 결과가 Phase 2 근거를 주지 않음 | 로드맵 변경 | 사전 등록 반대 결과 조항대로 |
| 명칭 혼동 (기존 "Phase 1") | 문서·태그 혼동 | 단계 1 |

### 9.6 구현 순서

각 단계는 "적용해줘" 지시 뒤에 하고, 단계마다 커밋을 나눈다. 🔒 = AWS 변경, 사용자 승인 필요.

| 단계 | 내용 | 레포 | 완료 기준 |
|---|---|---|---|
| 0 | 사전 확인(읽기 전용): RDS `max_connections`, vCPU 한도, gunicorn graceful timeout 기본값과 현재 설정 | — | 9.5 의 확인 필요 항목 해소 |
| 1 | Prototype 보존: 세 레포 태그 `prototype-v1`, `docs/architecture/prototype.md`, infra 로컬 문서 머리말 | app, config, infra | 태그 push |
| 2 | API 쪽: `HttpInference`, 설정, 메트릭, readyz, 단위 테스트 | app | pytest 전부 통과 (SQLite·PostgreSQL) |
| 3 | AI 서비스: `inference_service/`, 진입점, gunicorn 분리, 계약 테스트 | app | pytest 통과, 로컬에서 두 프로세스로 사진 판정 |
| 4 | Dockerfile 멀티 타깃, docker-compose, PC 시뮬레이터 통합, api 이미지 RSS·크기 측정 | app | compose 로 위반 확정·종료, AI 중지 시 관리자 API 정상 |
| 5 🔒 | infra: ECR `inference`(data), inference Pod Identity·backend 권한 축소(cluster) | infra | 한 리소스씩 plan → apply |
| 6 | CI: inference 이미지 빌드·푸시, config 태그 3줄 | app | 두 이미지가 ECR 에 |
| 7 | config: 매니페스트, kustomization, 대시보드 | config | `kubectl kustomize` 통과 |
| 8 🔒 | 클러스터 재구축(기존 순서) → 배포 확인 | — | 9.3 클러스터 확인 항목 |
| 9 | 사전 등록 "고정" 커밋 → E0~E3, T1~T3(k6 는 N3, HPA 끔) → 공식 구성 값 커밋 | app, config | 공식 구성 확정 |
| 10 🔒 | 부하 노드 그룹 + main max 4 → S0~S5, X1, P1 | infra, config | 결과 파일 커밋 |
| 11 | Phase 1 보고서, ADR 0018 "결정됨", ADR 0019 수정 | app | 문서 커밋 |

---

## 10. 문서 보존 (v3: 방식 B 결정)

### 10.1 결정과 사실

- 사용자 결정(2026-10-30): **공개할 문서만 Git 에 선택 추적**한다. `docs/` 전체는 추적하지 않는다.
- 사실 [확인]: 앱 레포 `.gitignore` 가 `docs/` 전체를 제외하고 있었다. 기존 ADR 17개와 측정 문서도 Git 에 없었다.

### 10.2 공개 범위 검토 결과

검사 항목: AWS 계정 ID(12자리, config.env 값 대조), 실제 도메인(tfvars 값 대조), ARN, 보안 그룹·VPC ID, Access Key 형식, 이메일, IP, 로컬 사용자 경로, 비밀번호·키 문자열, 다른 사람의 이름·저장소 주소.

| 파일 | 공개 | 근거 |
|---|---|---|
| `docs/adr/` 전체 (0001~0019, README) | ✔ | 계정·도메인·ARN 없음. 0014·0015 에 비공개 Claude 아티팩트 링크와 공개 GitHub 프로젝트 주소(EasyKoreanLpDetector) 가 있다. 민감 정보는 아니고, 아티팩트 링크는 외부인이 열 수 없다 |
| `docs/design/ARCHITECTURE_EVOLUTION.md`, `API.md`, `FLOW_*.md` 4개 | ✔ | 데모 계정(`kim@a.com` 등, `Passw0rd!`)은 이미 공개된 `README.md`·`.env.example` 에 있는 로컬 데모 값 |
| `docs/measurements/` 의 md 4개, json 4개, log 2개, `phase1/` | ✔ | 측정 기록. 개발용 장비 키 `pgk_dev_<serial>` 은 이미 공개된 `backend/README.md`·`cli.py` 의 로컬 고정 규칙 |
| `docs/design/EXECUTION_FLOW.md` | ✘ | 10-20 버전(interval 시절)으로 대체됨. 혼동 방지 |
| `docs/legacy/LEGACY_ANALYSIS.md` | ✘ | 원래 팀 저장소 주소와 팀원 이름이 든 브랜치명 |
| `docs/REFACTORING_PLAN.md` | ✘ | 팀원 이름이 든 브랜치명, 옛 Phase 명칭(1~10)으로 새 명칭과 충돌 |

공개 문서 중 제외 문서를 가리키는 곳은 `FLOW_CONFIG_AND_DATA.md` 의 `EXECUTION_FLOW.md` 언급 1곳이다(링크 아님).

### 10.3 `.gitignore` 방식

`docs/` 를 `docs/*` 로 바꾸고 허용 목록으로 다시 포함한다. 새 파일이 자동으로 공개되지 않도록 `design/` 과 `measurements/` 는 파일 형식 단위로 허용한다. 측정 결과를 커밋할 때마다 같은 검사를 한다.

### 10.4 ADR 번호

0018·0019 를 앱 레포에 썼다. infra 레포에 ADR 파일은 없었다. ADR 색인의 "infra ADR 은 0018부터" 문장을 "같은 번호 체계에서 다음 번호" 로 바꿨다.
