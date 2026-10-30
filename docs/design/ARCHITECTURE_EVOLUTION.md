# Parking Guardian 아키텍처 발전 계획 (Prototype → Phase 1 → Phase 2)

- 상태: **설계 검토용 v4** (2026-10-30). 코드·매니페스트·Terraform·AWS 는 아무것도 바꾸지 않았다.
- 표기: **[확인]** = 저장소 코드나 과거 세션 측정에서 직접 확인한 사실. **[제안]** = 설계 제안. **[추정]** = 측정 전 예상치(측정으로 대체). **[확인 필요]** = 구현 전에 사실 확인이 필요한 것.
- 경로 접두어: `app:` = `capston/`, `config:` = `parking-guardian-config/`, `infra:` = `parking-guardian-infra/`.
- 원칙: Phase 1 과 Phase 2 모두 **안전한 운영 설정**(타임아웃, 동시 상한, 재시도 상한, 멱등성)을 갖춘 상태에서 실제 한계를 잰다. 어느 쪽도 일부러 약하게 만들지 않는다. 장애 주입은 테스트 전용 설정으로만 켜고 결과에 "주입 실험" 으로 표시한다.

## 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v1 | 2026-10-30 | 초안: 로드맵, Phase 1 설계, 실험, S0~S5 수정안, Phase 2 ADR 초안 |
| v4 | 2026-10-30 | 노드 운영 결정: 공용 노드 그룹 3대 유지, API·AI 공존 허용, 추가 노드 계획 제외(v3 의 main 4대 + 부하 노드 1대 취소). 3.7.1 재작성(요청값 대 실측, 배치 규칙 preferred, preferred 검증 V1~V3, 경합 구분 방법), 3.8 부하 발생기 위치(클러스터 안 작은 Job / 로컬 PC), 3.9 HPA max 를 노드 수와 분리, 3.12 CloudWatch add-on 분석(결정 대기), 9.2·9.6 재작성, 11장 향후 개선 계획 신설 |
| v3 | 2026-10-30 | 문서 보존 방식 B 결정(공개 문서만 선택 추적). CPU 요청 여유 80m 검토와 배치 안 비교(3.7.1), 최종 변경 파일 목록과 구현 순서(9.1, 9.6) 갱신. ADR 0018·0019, 사전 등록 초안 별도 파일로 작성 |
| v2 | 2026-10-30 | 사용자 결정 5가지 반영. 부하 발생기 전용 노드 격리 설계(3.8), 동시 상한 값을 실험으로 정하는 절차(4.2), Phase 2 상태 경로와 판별 경로 분리, MessageGroupId 분석, event_id·session_id·seq, 아웃박스 재전송기와 상태 전이, 부분 실패 표, 보존 정책 구분, 전 과정 멱등성 검증표, confirm 타이밍 분석과 명령 설계, 장비 재시도·백오프(6장). Phase 1 착수 전 최종 정리(9장), 문서 보존 방안(10장) |

## 사용자 결정 (2026-10-30)

| 번호 | 결정 | 반영 위치 |
|---|---|---|
| D1 | ~~부하 테스트 때만 전용 노드를 임시 추가~~ → **v4 에서 취소.** 공용 노드 그룹 3대 유지, API·AI 공존 허용. k6 는 AI 단독 실험만 클러스터 안 작은 Job, 전체 경로는 로컬 PC | 3.7.1, 3.8 |
| D6 | (v4) CloudWatch add-on 제거·축소는 승인하지 않음. 분석만 | 3.12 |
| D7 | (v4) 노드 그룹 분리는 향후 개선 계획으로만 기록 | 11 |
| D8 | (v4) CloudWatch add-on 은 O1 우선. 변경은 승인 뒤 | 3.12 |
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
| infra | 소규모 추가 | ECR `inference`, inference Pod Identity(모델 읽기), backend 역할에서 모델 권한 제거. 노드 그룹·노드 수는 그대로(v4) |
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
| 동시 상한 `AI_CLIENT_MAX_CONCURRENCY` | 4 (스레드 8 의 절반). **운영값이 아닌 시작값** | **T2 (4.2) 로 조정.** 스레드 수보다 작아야 한다. 현재 Dockerfile 기본 스레드가 4 라서 기본값 4 는 모든 스레드를 AI 대기에 쓸 수 있다. `build_inference` 가 상한 ≥ `GUNICORN_THREADS` 이면 경고를 남긴다 |
| 예외 매핑 | 타임아웃·연결·5xx·busy → `InferenceError`, 400 → `InvalidImageError` | `device.py` 수정 없이 기존 503/400 계약 유지 |
| `state` | `"remote"` | readyz 가 AI 상태로 API 를 NotReady 로 만들지 않게 |

**`X-Request-Id` 는 추적용이다.** 양쪽 로그를 잇는 용도이며, 서비스는 이 값으로 중복 추론을 막지 않는다(멱등성 키가 아니다). 중복 분석은 재시도 규칙(서비스가 거절했거나 받지 못한 요청만 반복, 읽기 타임아웃·전송 뒤 끊김은 반복하지 않음)으로 피한다.

**전체 마감(deadline)의 한계.** `requests` 의 타임아웃은 소켓 동작 단위(연결, 데이터를 기다리는 각 시점)이지 전체 시간이 아니다. DNS 조회는 연결 타임아웃에 포함되지 않고, 응답을 조금씩 흘려 보내는 서버는 마감을 넘길 수 있다. AI 서비스는 분석이 끝난 뒤 한 번에 응답하므로 실제로는 읽기 타임아웃(= 남은 시간)이 호출을 제한하지만, 10초는 보장값이 아니라 설계 목표다. **실측(Docker Desktop, 2026-10-30):** inference 컨테이너가 없을 때 이름 해석 실패에 시도당 약 4초가 걸려, 연결 실패 1회 재시도를 합쳐 탐지 503 이 8.5초에 나왔다(마감 안이지만 여유 1.5초). Kubernetes Service 이름은 엔드포인트가 없어도 해석되므로 같은 현상은 아닐 것으로 예상하나 확인하지 않았다(`phase1/local/compose-drills.md`).

**readyz 에서 AI 를 빼는 이유**: AI 장애 → 모든 API Pod NotReady → ALB 가 관리자 API 까지 503 이 된다. 분리의 목적이 장애 격리이므로 API readiness 는 DB 만 본다.

### 3.5 AI 서비스 동시성 [제안]

| 항목 | 초기값 | 확정 방법 |
|---|---|---|
| 프로세스 / 모델 인스턴스 | worker 1 / 1개 | E2 결과로 재검토 |
| 스레드 | 4 | 대기 상한 + 2 (추론 1 + 헬스체크 1) 이상 |
| 동시 추론 | 1 (기존 락) | 락은 스레드 안전성 검증 전에는 제거하지 않음 |
| 대기 상한 `INFERENCE_MAX_WAITING` | 2 | **T1 (4.2) 로 조정** |
| torch 스레드 `TORCH_NUM_THREADS` | 서비스 **시작값** 1 (**운영값이 아니다. EKS 의 E0/E1 로 정한다**). **Prototype 은 설정하지 않고 torch 기본값(보이는 코어 수)으로 돈다** (코드·Dockerfile·매니페스트 확인. Pod 안 값은 미측정). 이 PC(4코어) 실측: 1개 3.52초, 2개 2.20초, 4개 1.51초 (`phase1/local/inference-service-check.md`). `torch.set_num_threads` 만으로는 스레드 풀에 일관되게 적용되지 않아 `OMP_NUM_THREADS` 도 설정한다 | E1 (EKS 에서 비교). 환경변수(`OMP_NUM_THREADS`)가 Linux gunicorn 에서 torch import 전에 적용되는지 3-3 에서 확인 |
| readiness | 모델 ready 일 때만 200. busy 는 반영 안 함 | 부하로 NotReady 가 되면 남은 Pod 로 몰려 연쇄 장애 |
| 모델 로딩 실패 | 백그라운드 최대 3회(`MODEL_LOAD_ATTEMPTS`) 시도 뒤 `/healthz` 503 → livenessProbe 로 컨테이너 재시작 | 단계 5-4 에서 inference Deployment 의 livenessProbe 가 `/healthz` 를 보는지 확인 |

**대기 상한은 프로세스 안의 제한이다.** `AdmissionGate`(`inference_service/admission.py`)는 한 프로세스의 메모리 안에서만 센다. gunicorn worker 가 2개면 worker 마다 모델 1개 + 대기 2개씩 따로 허용되어 상한이 2배가 되고, 모델 메모리도 2배가 된다. 그래서 AI 서비스는 **worker 1개** 전제다(3.5 첫 행). 2-6 에서 gunicorn 설정이 `workers=1` 을 강제하는지 확인하고, Pod 를 늘려 얻는 전체 상한은 "Pod 수 × (1 + 대기 상한)" 이다.

**게이트가 디코드까지 직렬화한다.** `AdmissionGate` 는 `analyze()` 전체를 감싸므로 이미지 디코드(`cv2.imdecode`)도 한 번에 하나씩 실행된다. Prototype 은 디코드가 락 밖이라 병렬이었다. 측정 없이 최적화하지 않고, E1 에서 함께 비교한다(4.1).

### 3.5.1 inference Probe 구성 검토 (제안, 아직 매니페스트 아님)

근거는 `phase1/local/inference-service-check.md` 의 로컬 값(모델 준비까지 28.4초, 모델 로딩 실패 시 포기까지 약 34.5초)이다. EKS(2 vCPU)는 더 느릴 수 있으나 **측정하지 않았다**. 아래 시간은 6-2 에서 실측한 뒤 정한다.

| Probe | 경로 | 제안 | 이유 |
|---|---|---|---|
| startupProbe | `/readyz` | 5초 간격 × 36회 (180초) 안에 200 이 되어야 함 | 모델 로딩이 멈추거나(예외 없이) 오래 걸리는 경우도 잡는다. 180초는 로컬 28초(Linux 컨테이너 실측 준비 시간 20~30초, 실행마다 변동)의 약 6배인 **임시 여유값**이고, EKS 에서 첫 기동 시간을 재서 정한다. startup 이 성공하기 전에는 liveness 가 쉬므로, 로딩 중인 Pod 가 liveness 로 죽지 않는다 |
| readinessProbe | `/readyz` | 5초 간격, 실패 3회 | 모델이 ready 일 때만 Service 에 들어온다. busy 는 반영하지 않는다(3.5) |
| livenessProbe | `/healthz` | 10초 간격, 실패 3회 | 로딩 포기(`gave_up`) 뒤 503 이 되어 컨테이너가 재시작된다. 요청 스레드가 모두 막힌 경우도 `/healthz` 가 응답하지 못해 잡는다 |

모델 로딩이 실패할 때의 흐름: 백그라운드 3회 시도(약 34초) → `/healthz` 503 → startup 이 아직 진행 중이면 liveness 는 쉬고 있어서 **startupProbe 가 180초에 실패해야 재시작**된다. 즉 로딩 실패의 재시작은 포기 시점(약 35초)이 아니라 startup 한도(180초) 근처에 일어난다. 포기 직후 바로 재시작하려면 startupProbe 를 `/healthz` 로 두는 대안이 있으나, 그러면 예외 없이 멈추는 로딩을 잡지 못한다. 두 대안 중 선택은 5-4 에서 하고, 선택 전에는 위 제안을 따른다.

재시작 정책: `restartPolicy: Always`(기본)에서 컨테이너 재시작은 지수 백오프(CrashLoopBackOff, 최대 5분)로 늘어나므로 무한히 빠른 재시도는 없다. 재시작해도 같은 원인(모델 파일 없음 등)이면 계속 실패하니, 이 경우 알림(대시보드의 재시작 횟수)으로 사람이 알아야 한다. `terminationGracePeriodSeconds` 는 `graceful_timeout`(30초) 보다 커야 한다(5-4 에서 확인).

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

| 노드 | 평상시 (CloudWatch add-on 현행) | CPU 요청 합 / 1,930m | 메모리 요청 합 |
|---|---|---|---|
| N1 | inference + api | 650 + 1,000 + 200 = 1,850m | 약 2.4 GiB |
| N2 | inference + api | 1,850m | 약 2.4 GiB |
| N3 | Argo CD·모니터링·frontend·컨트롤러, surge 자리, HPA 로 늘어난 Pod 자리 | 약 800m | 약 2~3 GiB |

- 제약은 CPU 요청이고 메모리는 여유가 크다. 실제 CPU 사용량은 요청보다 훨씬 작다(3.7.1).
- 배치 규칙과 확장 정책은 3.7.1, 3.9. 노드는 **공용 노드 그룹 3대**이고 API·AI 공존을 허용한다.
- 선택: api→inference 를 같은 AZ 로 우선 보내는 Service `trafficDistribution: PreferClose`(EKS 1.35 에서 사용 가능 [확인 필요]). 기본은 끄고 측정 뒤 결정.

### 3.7.1 3노드 배치 검토 (v4)

**결정 (사용자, 2026-10-30)**: Phase 1·Phase 2 모두 기존 공용 노드 그룹 3대를 쓴다. API 와 AI Pod 의 같은 노드 배치를 허용한다. 추가 노드는 계획에서 뺀다. v3 의 "테스트 때 main 4대 + 부하 노드 1대" 는 **취소**. Pod 를 분리했다고 노드까지 분리할 필요는 없다. 노드 그룹 분리는 향후 개선(11장)으로 둔다.

**요청값 대 실제 사용량 [확인]**

| 대상 | CPU 요청 | 실제 사용량 (유휴, `kubectl top`, 10-28) |
|---|---|---|
| CloudWatch add-on (`amazon-cloudwatch`) | 노드 2대 합 700m (10-27). 기본값 계산: 에이전트 250m + Fluent Bit 50m 를 노드마다, 매니저 100m 1개 → 2 × 300 + 100 = 700m 로 일치 | 에이전트 3m, Fluent Bit 2~3m |
| `kube-system` (aws-node, kube-proxy, pod-identity, CoreDNS ×2, metrics-server ×2, ALB 컨트롤러) | 노드 2대 합 700m (10-27) | Pod 당 1~4m |
| backend (Prototype) | 1,000m | 2m (메모리 982Mi) |
| 노드 전체 | — | 37~58m |

- **가장 큰 예약은 CloudWatch add-on 이다.** 노드마다 약 300m, 3대면 클러스터 매니저 포함 약 1,000m 다. 처리는 3.12(분석, 결정 대기).
- **inference 1,000m 는 부하 테스트 기준으로 적정하다.** torch 1스레드라 포화 시 약 1코어를 쓴다(E0 로 확인). 유휴 운영에서는 과한 예약이지만 무중단을 위한 min 2 결정과 묶여 있다.
- **api 200m 는 근거가 없다.** S0 의 실제 CPU 사용량으로 다시 정한다. 요청이 실측에 맞으면 HPA % 도 그에 맞게 의미를 갖는다(v3 에서 100m 안을 "HPA 가 민감해진다" 로 기각한 이유는 철회).
- CPU limit 은 두지 않는다. 요청은 예약일 뿐이고 노드 CPU 가 남으면 Pod 는 요청보다 더 쓴다. 노드당 실제 한계는 2 vCPU 다.

**노드를 많이 필요해 보이게 한 규칙과 v4 변경**

| v3 까지의 규칙 | 효과 | v4 |
|---|---|---|
| inference podAntiAffinity **required** (Prototype 에서 물려받음) | inference Pod 수 + 롤링 surge 1 = 필요한 노드 수 | **preferred**(가중치 100) + zone spread(ScheduleAnyway) |
| api 가 inference 노드를 피함 (v3) | 4번째 노드가 필요 | **삭제** |
| api 끼리 분산 | — | preferred 유지(가중치 100) |
| HPA max = 노드 수 | Pod 수와 노드 수 1:1 | **노드 수와 연결하지 않음.** 요청값과 노드 여유로 계산(3.9) |

**preferred 의 한계와 검증**: preferred 는 분산을 보장하지 않는다. 그리고 `IgnoredDuringExecution` 이라 이미 떠 있는 Pod 를 다시 옮기지 않는다. 롤링 업데이트나 노드 교체 뒤 inference 2개가 한 노드에 남을 수 있다. 그래서 다음을 테스트에 넣는다(5.2 V1~V3).

| 번호 | 확인 | 방법 |
|---|---|---|
| V1 | 배치 결과 | 배포 직후, 각 시나리오 전후, 롤링 뒤 `kubectl get pods -o wide` 로 inference·api 의 노드를 기록. 같은 노드에 몰리면 그 사실과 원인을 기록 |
| V2 | 노드 1대 장애 (분산된 상태) | 부하 중 inference 가 있는 노드 1대를 cordon + drain. 남은 Pod 로 서비스가 이어지는지, PDB 동작, 재배치·Ready 까지 시간 |
| V3 | 노드 1대 장애 (몰린 상태, 주입 실험) | 다른 노드를 잠시 cordon 해 inference 2개를 한 노드에 모은 뒤 그 노드를 drain. 탐지가 끊기는 시간과 관리자 API 영향. preferred 를 고른 대가를 숫자로 남긴다 |

**CPU 경합과 동기 대기를 추가 노드 없이 구분하는 방법**

1. **지표로 구분**: 동기 대기면 API 스레드 점유율과 `pg_ai_client_in_flight`, AI 호출 시간이 함께 오른다. CPU 경합이면 노드 CPU 사용률이 100% 근처이고, AI 를 부르지 않는 일반 API 라우트의 지연이 스레드 포화 없이 오른다.
2. **고정 배치 대조 회차 (P1)**: 한 회차에 한해 api 2개를 inference 가 없는 N3 에 고정해 S1 을 다시 돌린다(Argo CD 자동 sync 를 잠시 멈추고 patch, 끝나면 복구). 그 회차만 노드 단위 고가용성을 포기한다. N3 는 800 + 400 = 1,200m 로 들어간다.
3. 그래도 구분되지 않으면 그 사실을 기록한다. 노드 추가나 분리는 별도 결정이다(현재 계획 제외).

### 3.8 부하 발생기 위치 (v4)

| 실험 | k6 위치 | 이유 |
|---|---|---|
| E0~E3, T1 (inference 단독) | **클러스터 안 작은 k6 Job.** CPU 요청 100m / 메모리 128Mi [추정]. HPA 를 끈 실험이라 N3 여유로 충분 | inference 는 내부 Service 라 PC 에서 직접 닿지 않는다. port-forward 는 단일 터널이라 그 자체가 병목이다 |
| S0~S5, T2, X1, P1, V1~V3 (전체 경로) | **로컬 PC → ALB** | 장비와 같은 경로(end-to-end). 클러스터 자원을 쓰지 않는다. AWS 로 들어오는 전송은 과금되지 않는다 |

**로컬 PC 실행 조건과 병목 판정**

| 항목 | 기준 |
|---|---|
| 네트워크 | 유선 연결. 테스트 전 업로드 속도 측정. 최대 단계(1.5C) 필요량 약 9~14 Mbps [추정: C 를 PC 추론 속도로 가정] 의 3배 이상 |
| k6 실행기 | `constant-arrival-rate`. `dropped_iterations` > 0 이면 그 회차 무효(부하 발생기가 목표 도착률을 못 냄) |
| PC 자원 | k6 프로세스 CPU < 70%. 넘으면 무효 |
| 경로 오차 | k6 p95(클라이언트)와 서버 `http_request_duration_seconds` p95 의 차이를 단계마다 기록. 부하가 오를 때 차이만 커지면 네트워크·발생기 병목으로 보고 무효 |
| 연결 | keep-alive 사용. TLS 핸드셰이크·연결 시간(`http_req_tls_handshaking`, `http_req_connecting`)을 따로 기록 |
| 설치 | PC 에 k6 가 아직 없다 [확인]. 버전을 결과에 기록 |
| Phase 1·2 동일 조건 | 같은 PC, 같은 네트워크, 같은 k6 버전, 같은 시간대, 같은 스크립트·사진 |

### 3.9 확장 정책 (v4)

| | api | inference |
|---|---|---|
| 지표 | CPU 70% (요청 기준. 요청값은 S0 실측 뒤 확정) | CPU 70% (1,000m 기준. E0 실측 뒤 확인) |
| min / max | 2 / 3 [초기값] | 2 / 3 [초기값] |

> api·inference 모두 **최대 3 은 3노드 실험 환경의 시작 기준이고 운영 환경의 최종 확장 한도가 아니다**(사용자 결정, 2026-10-30). 실제 노드 자원과 Pod 배치(V1)를 확인해 확정한다. 처음에는 api 4 로 적었으나 api 4 + inference 3 은 CPU 요청상 들어가지 않는다.
| scaleDown 안정화 | 120초 [추정] | 300초 |
| PDB | minAvailable 1 | minAvailable 1 |
| 분산 | preferred (api 끼리) | preferred (inference 끼리) + zone spread |

- max 는 노드 수가 아니라 **노드별 CPU 요청 여유**로 계산한다. CloudWatch add-on 현행 기준 3대의 여유 합은 약 3,690m 다(1,280 + 1,280 + 1,130).
- **api 3 + inference 3 은 들어가지만 여유가 30m 뿐이다.** 노드마다 inference 1 + api 1 이면 1,200m / 1,280m 라 노드마다 80m 가 남고, 노드에 묶이지 않는 Pod(frontend 50m 와 확인 못 한 컨트롤러 요청)의 CPU 합이 **80m 를 넘으면 들어가지 않는다**(`check-placement.py` 민감도 표). 이전 서술(N3 여유 약 130m → "inference 3일 때 api 3번째부터 들어갈 노드가 없다")은 노드에 묶이지 않는 Pod 를 약 150m 로 잡은 가정이었고, 지금 계산은 50m 로 잡았다. **어느 쪽이 맞는지는 재구축 직후 노드별 요청 합으로 확인한다.** 이 최대 상태에서 롤링 업데이트를 하면 surge Pod 가 들어갈 자리가 없어 `maxUnavailable 0` 이라 서비스는 유지되고 롤링이 부하가 줄어 HPA 가 내려갈 때까지 기다린다(중단이 아니라 지연). Pending 은 숨기지 않고 S4 의 결과로 기록한다.
- 요청값을 실측으로 다시 정하면 이 계산을 다시 한다. CloudWatch add-on 결정(3.12)에 따라서도 바뀐다.

### 3.10 계측 [제안]

api 추가: `pg_ai_client_request_seconds{outcome}`(ok, timeout, connect_error, busy, server_error, invalid_image), `pg_ai_client_in_flight`, `pg_ai_client_rejected_total`, `pg_http_threads`(상수). inference 추가: `pg_inference_rejected_total{reason}`. PodMonitor 2개, `route` 라벨 규칙 유지. 대시보드 `phase-comparison` 재구성(부하 → 일반 API 분위수·오류 → 스레드 점유 → AI 호출 → 락 대기·대기 수·거절·처리량 → Pod 자원·Ready → HPA).

### 3.11 Phase 1 의 알려진 비용

네트워크 홉 1개와 사진 바이트 재전송, 이미지 2개·CI 1개 추가, 매니페스트 약 2배. inference 콜드 스타트는 그대로, api 는 작은 이미지로 빨라짐 [추정].

### 3.12 CloudWatch Observability add-on 분석 (결정 대기)

> 사용자 결정(2026-10-30): **제거·축소는 아직 승인하지 않는다.** 아래는 판단 자료다. 지금 구성은 그대로 둔다.
>
> 사용자 결정(2026-10-30, 종료 전): **O1(v7 중복 구성 요소 끄기)을 우선 선택.** 실제 변경(`addons.tf` 의 `configuration_values`)은 9.6 단계 5 에서 plan 을 보이고 별도 승인 뒤 적용한다.

**지금 구성 [확인]**

| 항목 | 내용 | 출처 |
|---|---|---|
| 설치 | EKS 관리형 add-on `amazon-cloudwatch-observability`. `addon_version` 을 고정하지 않아 **생성 시점의 기본 버전**이 설치된다 | `infra:cluster/addons.tf` |
| 끈 것 | Application Signals(APM 자동 계측) | 같은 파일 `configuration_values` |
| 로그 그룹 | `/aws/containerinsights/<클러스터>/{application, dataplane, host, performance}`, 보관 7일. **cluster 스택이 만들고 cluster destroy 때 함께 지워진다** | 같은 파일, 학습 교재 Part 5 조회 기록 |
| 알람 | 인프라 설계는 CloudWatch 알람 5개(ALB 5xx, p95 > 10초, UnHealthyHost, RDS 디스크, Pod 재시작)와 대시보드 1개를 계획했지만 **Terraform 에 알람이 하나도 없다**. Prometheus Alertmanager 도 꺼져 있다. 즉 지금 사람에게 가는 알림 경로가 없다 (Budgets 비용 알림만 있음). `values.yaml` 의 "알람은 CloudWatch 5개가 맡는다" 주석은 실제와 다르다 | `infra:docs/phase1/architecture.md` 307행, `grep aws_cloudwatch_metric_alarm` 0건, `config:platform/monitoring/values.yaml` |

**구성 요소별 기본 요청값** (add-on 설정 스키마, 현재 기본 버전 `v7.0.0-eksbuild.1`, 2026-10-30 읽기 전용 조회 [확인])

| 구성 요소 | 단위 | CPU / 메모리 요청 | 기본 켜짐 | 하는 일 |
|---|---|---|---|---|
| CloudWatch 에이전트 | 노드마다 | 250m / 128Mi | 예 | Container Insights 지표(노드·Pod·컨테이너 CPU·메모리·네트워크·디스크·재시작) → `performance` 로그 그룹과 `ContainerInsights` 지표 |
| Fluent Bit | 노드마다 | 50m / 25Mi | 예 | 컨테이너 stdout/stderr → `application`, kubelet·kube-proxy·containerd 등 → `dataplane`, 호스트 로그 → `host` |
| 매니저(오퍼레이터) | 1개 | 100m / 64Mi | 예 | 위 구성 요소와 웹훅 관리 |
| **nodeExporter** | 노드마다 | **256m** / 128Mi | **예** | 노드 지표 |
| **kubeStateMetrics** | 1개 | **256m** / 128Mi | **예** | Kubernetes 오브젝트 상태 |
| dcgmExporter, neuronMonitor | GPU·Neuron 노드에만 | — | 예 (해당 노드 없음) | GPU·Inferentia 지표 |

- 10-27 의 `amazon-cloudwatch` 네임스페이스 요청 합 700m(노드 2대)는 에이전트·Fluent Bit·매니저만으로 계산한 값과 정확히 맞는다. 10-28 `kubectl top` 에도 이 셋만 보였다. 그때 설치된 버전에는 nodeExporter·kubeStateMetrics 가 없었거나 꺼져 있었던 것으로 보인다 [추정].
- **정정 (2026-10-30 읽기 전용 스키마 비교, 5단계)**: v6.7.0 과 v7.0.0 의 설정 스키마를 비교하니 두 구성 요소(`nodeExporter`, `kubeStateMetrics`)의 `enabled` 기본값은 **두 버전 모두** true 로 적혀 있고, 차이는 Knative/KServe/vLLM 스크랩 옵션 추가뿐이다. 즉 "v7 부터 켜진다" 는 아래 서술의 근거는 스키마에 없다. 반면 10-27/28 의 v6.7.0 클러스터에서는 두 구성 요소가 실제로 뜨지 않았다. 스키마 기본값과 실제 배포 동작이 다를 수 있어(이유 미확인) **버전과 무관하게 명시적으로 끄고, 측정된 v6.7.0 으로 고정**했다(`addons.tf`, P1). 선택지와 근거는 `phase1/aws-review/aws-review.md` 6절.
- **재구축 위험**: 버전을 고정하지 않았으므로 다음 재구축 때 v7.0.0 이 설치될 수 있다. 그러면 노드마다 256m(nodeExporter)와 클러스터에 256m(kubeStateMetrics)가 더 예약될 수 있다. 3노드 배치(3.9)가 깨진다. kube-prometheus-stack 의 node-exporter·kube-state-metrics 와 **중복**이고, 둘 다 노드의 9100 포트를 쓰면 충돌할 수 있다 [확인 필요]. **재구축 직후 `kubectl -n amazon-cloudwatch get ds,deploy` 로 반드시 확인한다.** 이 확인은 AWS 변경이 아니다.
- 리소스 조정 가능 여부: 스키마에 `agents[].resources`, `containerLogs.fluentBit.resources`, `manager.resources`, `nodeExporter.enabled/resources`, `kubeStateMetrics.enabled/resources`, `containerInsights.enabled`, `containerLogs.enabled` 가 있다 [확인]. 즉 **요청값 조정과 구성 요소별 끄기가 add-on 설정만으로 가능하다.** `containerInsights.enabled=false` 일 때 에이전트 DaemonSet 이 사라지는지는 확인하지 못했다 [확인 필요].

**Prometheus/Grafana 와의 중복**

| 기능 | CloudWatch add-on | Prometheus/Grafana (kube-prometheus-stack) | 중복 |
|---|---|---|---|
| 노드 CPU·메모리·네트워크·디스크 | 에이전트 (v7: + nodeExporter) | node-exporter | ✔ |
| Pod·컨테이너 CPU·메모리 | 에이전트 | kubelet cAdvisor | ✔ |
| Pod 상태·재시작 | 에이전트 (v7: + kubeStateMetrics) | kube-state-metrics | ✔ |
| 앱 지표 (HTTP, 추론, 락 대기) | ✘ | PodMonitor | Prometheus 만 |
| **컨테이너 로그 (stdout/stderr)** | Fluent Bit → CloudWatch Logs | ✘ (Loki 등 없음) | **CloudWatch 만** |
| **노드·시스템 로그** | Fluent Bit | ✘ | **CloudWatch 만** |
| AWS 콘솔에서 보기 | ✔ | Grafana 는 port-forward 로만 | — |
| 알람 | 가능하지만 만든 것 없음 | Alertmanager 꺼짐 | 둘 다 없음 |
| 클러스터 해체 뒤 남는가 | 아니오 (로그 그룹이 cluster 스택 소유) | 아니오 (PVC 삭제) | 둘 다 세션 단위 |

**선택지 (결정 대기)**

| 안 | 내용 | 노드당 CPU 요청 변화 [추정] | 잃는 기능 | 변경 위치 |
|---|---|---|---|---|
| O0 | 지금 그대로 + 버전 고정 | 0 | 없음 | `addons.tf` 에 `addon_version` |
| O1 | 지금 그대로 + v7 중복 구성 요소 끄기 (`nodeExporter`, `kubeStateMetrics` = false) | 0 (v7 이 설치되면 −256m 방지) | 없음 (Prometheus 가 같은 지표를 가짐) | `configuration_values` |
| O2 | 에이전트·Fluent Bit 요청값을 실측에 맞춰 축소 (예: 에이전트 50m, Fluent Bit 25m) | 약 −225m | 없음. 경합 시 이 Pod 들의 CPU 몫이 줄어 지표·로그가 늦어질 수 있음 | `configuration_values` |
| O3 | Container Insights 지표 끄고 Fluent Bit 로그만 | 약 −250m | AWS 콘솔의 Container Insights 화면, Pod 재시작 지표 기반 CloudWatch 알람(계획만 있음) | `configuration_values` |
| O4 | add-on 제거 | 약 −300m (+ 매니저 100m) | **중앙 로그 전부.** 삭제·재생성된 Pod 의 로그를 다시 볼 수 없다. S5·V2·V3 같은 장애 실험의 사후 분석이 `kubectl logs` 로 그때그때 받아 두는 것에 의존한다 | `addons.tf`, IAM 역할, 로그 그룹 |

**운영에 필요한 최소 관측 기능 (판단 기준)**

1. 지표와 대시보드: Prometheus/Grafana 로 충족.
2. **중앙 로그**: 장애 Pod 가 사라진 뒤에도 로그를 볼 수 있어야 한다. 지금은 Fluent Bit 만 이것을 한다. O4 는 이 기능을 없앤다.
3. 헬스체크: ALB 와 kubelet 프로브로 충족.
4. **알림**: 지금 아무것도 없다. CloudWatch 알람이든 Alertmanager 든 최소 1개 경로가 필요하다(향후 개선 11장).
5. AWS 관리형 리소스(ALB, RDS) 지표: add-on 과 무관하게 CloudWatch 기본 지표로 남는다.

**확인 방법 (모두 읽기 전용, 클러스터를 켰을 때)**

```powershell
kubectl -n amazon-cloudwatch get ds,deploy -o wide
kubectl -n amazon-cloudwatch get pods -o custom-columns=NAME:.metadata.name,CPU:.spec.containers[*].resources.requests.cpu,MEM:.spec.containers[*].resources.requests.memory
aws eks describe-addon --cluster-name parking-guardian --addon-name amazon-cloudwatch-observability --query "addon.addonVersion"
```

비용: CloudWatch Logs 수집·보관과 Container Insights 지표 요금이 있다. 이 프로젝트의 로그 양과 단가는 확인하지 않았다 [확인 필요: Cost Explorer 서비스별 조회].

---

## 4. 소규모 실험 (Phase 1 본 테스트 전)

### 4.1 동시성 실험

inference 단독, k6 closed-loop.

| 번호 | 바꾸는 것 | 측정 | 비고 |
|---|---|---|---|
| E0 | 없음 (Pod 1, worker 1, torch 1, 동시 1) | 장당 서비스 시간 분포, 최대 처리량 C₁ | 이후 모든 값의 근거 |
| E1 | torch 스레드 1 → 2 | 처리량, p95, CPU | 남는 CPU 가 적어 이득이 없을 수 있다 [추정]. 단정하지 않음. **함께 기록:** 게이트가 디코드까지 직렬화한 영향(3.5). `pg_inference_stage_seconds{stage="decode"}` 가 장당 시간에서 차지하는 비중과, 디코드가 락 밖이던 Prototype 과의 처리량 차이. 비중이 작으면 그대로 두고, 크면 그때 디코드를 게이트 밖으로 뺄지 판단한다 |
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

**E0/T1 에서 확인할 관찰 항목 (값은 지금 바꾸지 않는다)**

- 대기 줄 끝 사진의 응답 시간이 API 마감에 얼마나 가까운가. 로컬 PC 에서 torch 스레드 1개일 때 동시 4장 중 대기 2장째 응답이 **9.85초**로 기본 마감(`AI_CLIENT_DEADLINE` 10초)에 근접했다. T3 식에 이 로컬 값(약 3.5초)을 넣으면 3.5 × (1 + 2) + 1 = 11.5초로 10초를 넘는다. 로컬 값은 결정 근거가 아니며, EKS 의 E0 p99 로 다시 계산한다.
- 마감을 넘기는 사진이 실제로 생기는지(`pg_ai_client_requests_total{outcome="timeout"}`), 마감 직전에 끝난 사진이 API 쪽에서는 이미 포기되어 낭비되는 비율.
- **같은 설정의 반복 실행에서도 시간이 크게 달랐다**(Linux 컨테이너, 스레드 1: 사진당 중앙값 1.98초와 3.24초, 준비 20.2초와 29.7초). E0 는 사전 등록의 3회 반복을 지키고 범위를 함께 기록하며, 로컬 한 번의 값은 크기 정도로만 쓴다.
- 스레드 수(E1)와 대기 상한(T1)이 서로 영향을 준다: 사진당 시간이 줄면 같은 대기 상한에서도 줄 끝 응답이 빨라진다.

T1~T3 결과로 정한 값이 **Phase 1 공식 구성**이 되고, 그 값으로 S0~S5 를 실행한다. 선택 규칙에 맞는 후보가 없으면 그 사실을 기록하고 가장 가까운 후보를 쓴다.

---

## 5. 부하 테스트 계획 (Phase 1 / Phase 2 공통 골격)

### 5.1 원칙

- 같은 k6 스크립트, 같은 사진(sample_a 정상, sample_b 위반), 같은 장비 키 5개, 같은 노드 사양·수(공용 노드 그룹 m7i-flex.large 3대), 같은 요청값. k6 위치는 3.8 (AI 단독 실험은 클러스터 안 작은 Job, 전체 경로는 로컬 PC).
- 일반 API: 로그인 1회 후 위반 목록, 대시보드 요약, 알림 조회 반복 + readyz 1 req/s 별도 기록.
- 사진 도착률 단계는 **E3 의 처리 용량 C 의 배수**(0.2C, 0.5C, 0.8C, 1.0C, 1.2C, 1.5C). 절대값 표도 남긴다.
- 각 시나리오 3회, 중앙값과 범위. 한 회차 15분 이내. node-exporter CPU steal 기록(m7i-flex 특성).
- **비교 A (동일 자원)**: HPA·KEDA 끔, api 2 + inference(또는 Worker) 2 고정, 같은 요청값. **비교 B (운영 자동 확장)**: HPA/KEDA 켬, 같은 노드 3대와 같은 max.
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
| V1 | 배치 확인 (v4) | 배포 직후·시나리오 전후·롤링 뒤 inference·api 노드 기록 | Worker·api 노드 기록 |
| V2 | 노드 1대 장애, 분산 상태 (v4) | 부하 중 inference 노드 1대 cordon + drain → 서비스 지속, PDB, 재배치 시간 | 같음 (Worker) |
| V3 | 노드 1대 장애, 몰린 상태 (v4, 주입 실험) | inference 2개를 한 노드에 모은 뒤 drain → 탐지 중단 시간, 관리자 API 영향 | 같음 (Worker). 큐 적체로 유실 0 인지 |
| P1 | 공존 경합 대조 (v4, 선택) | S1 을 api 2개 N3 고정으로 1회 → 기본 배치 회차와 비교 | Phase 2 에서 필요하면 같은 방식 |

### 5.3 지표

Phase 1: 일반 API p50/p95/p99·오류율, AI 호출 시간·outcome·타임아웃, **AI 응답 대기 중 API 의 DB 연결 점유**(`pg_stat_activity` 의 idle in transaction 수와 AI 호출 시간의 관계. `require_device` 가 연 트랜잭션이 AI 호출 동안 열려 있다 — 코드로만 확인, 미측정), API 스레드 점유율, AI 처리량(Pod 별·전체), 락 대기, 거절 수, 전파 정도(S5 동안 일반 API 대 S0), api·inference CPU·메모리, HPA 트리거 → Ready → 첫 성공 추론, **사진 성공률**.

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

- KEDA `aws-sqs-queue` 스케일러, 목표 대기 수/Pod 는 E0 처리량에서 계산. **min 1**(0 이면 첫 사진이 이미지 Pull + 모델 로딩을 기다린다). max 는 노드 수가 아니라 노드 CPU 요청 여유로 계산(3.9 와 같은 방식). 노드는 Phase 1 과 같은 공용 노드 그룹 3대. KEDA 는 Argo CD Application, SQS 권한은 Pod Identity.
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

> Prototype → Phase 2 전체의 큰 단계다. Phase 1 의 세부 작업 순서는 9.6 이 기준이다(v4). 노드 구성은 전 단계에서 공용 노드 그룹 3대다.

| 단계 | 내용 | 왜 / 해결하는 문제 | 난이도 | AWS 비용 |
|---|---|---|---|---|
| 0. 문서 보존 | 공개 문서 선택 추적 + 문서 전용 커밋 (10장, 2026-10-30 완료) | 설계 유실 방지, 사전 등록 증거 | 낮음 | 0 |
| 1. Prototype 보존 | 세 레포 태그 `prototype-v1`, `docs/architecture/prototype.md`, infra 문서 명칭 주석 | 이력 보존, 명칭 충돌 해소 | 낮음 | 0 |
| 2. 재측정 | 재구축 직후 노드 요청 합, api 이미지 RSS(로컬), RDS `max_connections` | 3.6·3.7 값 확정 | 낮음 | 클러스터 켜는 시간 |
| 3. 앱 Phase 1 | 9.1 의 app 파일 | 분리 본체 | 중간 | 0 |
| 4. CI | inference 이미지 | 배포 | 낮음 | 0 |
| 5. infra | ECR, Pod Identity (추가 노드 없음). CloudWatch add-on 은 3.12 결정에 따름 | 최소 권한 | 낮음 | 월 1달러 미만 |
| 6. config | 9.1 의 config 파일 | 배포 | 중간 | 0 |
| 7. 사전 등록 + E0~E3, T1~T3, X1 | 공식 구성 확정 | 근거 기반 값 | 낮음 | 세션당 1~3달러 |
| 8. S0~S5 + Phase 1 보고서 | 비교 A/B | Phase 1 의 실제 한계 | 중간 | 세션당 1~3달러 |
| 9. ADR 0019 확정 | 측정 결과로 6장 수정 | 근거 기반 결정 | 낮음 | 0 |
| 10. Phase 2 구현 | SQS FIFO + DLQ, inbox·증거 수명 주기, 테이블 4개, 재전송기, Worker, 장비 프로토콜, KEDA | 비동기 전환 | **높음** | 무료 범위 + NAT |
| 11. Phase 2 테스트 + 비교 보고서 | 같은 프로필 | 개선과 비용을 함께 증명 | 중간 | 세션당 1~3달러 |

---

## 9. Phase 1 착수 전 최종 정리 (v4)

### 9.1 변경 예정 파일 (Phase 1, 아직 아무것도 고치지 않음)

단계 번호는 9.6 의 구현 순서다.

**app (`capston/`)**

| 파일 | 변경 | 단계 |
|---|---|---|
| `.gitignore` | 허용 목록에 `docs/architecture/` 추가 | 1 |
| `docs/architecture/prototype.md` (신규) | Prototype 구조·설정·측정 기록, 태그 | 1 |
| `backend/inference_service/` (신규) | 앱 팩토리, `/v1/analyze`, `/healthz`, `/readyz`, 대기 상한, 메트릭 서버 | 2 |
| `backend/app/adapters/inference.py` | `InferenceResult.from_dict`, `HttpInference`, `build_inference` 에 `http` 분기, `YoloInference` 에 `TORCH_NUM_THREADS` | 2 |
| `backend/app/config.py` | `INFERENCE_URL`, `AI_CLIENT_*`(마감, 연결, 재시도, 동시 상한), `INFERENCE_MAX_WAITING`, `TORCH_NUM_THREADS`, 테스트 전용 `INFERENCE_FAULT_DELAY_MS` | 2 |
| `backend/app/api/health.py` | `remote` 상태 처리(API readiness 는 DB 만) | 2 |
| `backend/app/metrics.py` | AI 클라이언트 메트릭 4개, 추론 거절 1개 | 2 |
| `backend/wsgi_inference.py`, `backend/gunicorn_inference.conf.py` (신규) | AI 서비스 진입점과 웜업 훅 | 2 |
| `backend/gunicorn.conf.py` | 로컬 추론일 때만 웜업 | 2 |
| `backend/tests/test_inference_service.py`, `test_inference_http.py` (신규), `test_health.py`·`test_metrics.py` | 대기 상한, 상태별 응답, 계약, 타임아웃·재시도·동시 상한·예외 매핑, readyz | 2 |
| `backend/Dockerfile` | 멀티 타깃 `prototype`(마지막 = 기본값, 기존 빌드) / `api`(torch 없음) / `inference` | 3 |
| `backend/requirements-inference.txt` (신규) | inference 이미지 전용 의존성(Flask, dotenv, prometheus-client, `gunicorn==26.2.0`) | 3 |
| `docker-compose.phase1.yml` (신규) | `docker-compose.yml` 위에 겹쳐 쓴다: inference 서비스, backend 를 `api` 타깃·`INFERENCE_BACKEND=http` 로. **`docker-compose.yml` 은 바꾸지 않는다**(Prototype 실행 보존, 2026-10-30 사용자 지시) | 3 |
| `docs/measurements/phase1/local/` (신규) | 로컬 통합·회귀·시뮬레이터 결과 | 3, 4 |
| `.github/workflows/build-push.yml` | matrix 에 inference(타깃 지정), config 레포 태그 3줄 | 5 |
| `docs/adr/0018…`, `docs/measurements/phase1/README.md` | 측정 뒤 값·결과 | 6 |

**config (`parking-guardian-config/`)** — 단계 5 에서 검토, 승인 뒤 적용

| 파일 | 변경 |
|---|---|
| `apps/parking-guardian/backend-deployment.yaml` | initContainer·모델 볼륨 제거, 요청 200m/256Mi(S0 뒤 재조정), threads 8, required anti-affinity → **preferred(api 끼리)** 1개 + zone spread |
| `backend-hpa.yaml`, `backend-pdb.yaml` | min 2 / max 3 (실험 시작 기준), 안정화 120초, PDB minAvailable 1 |
| `inference-deployment.yaml`, `inference-service.yaml`, `inference-hpa.yaml`(min 2 / max 3), `inference-pdb.yaml`(minAvailable 1), `inference-podmonitor.yaml`, `serviceaccount-inference.yaml` (신규) | AI 서비스 한 벌. **preferred** anti-affinity + zone spread, Secret 마운트 없음 |
| `serviceaccount-backend.yaml` | 주석(모델 권한 제거) |
| `kustomization.yaml` | resources, images 3개, `ECR_INFERENCE` replacements, `inference-config` 생성기, `INFERENCE_BACKEND=http`, `INFERENCE_URL` |
| `config.env`, `config.env.example`, `scripts/export-config-env.py` | `ECR_INFERENCE` |
| `platform/monitoring/config/dashboards/build_dashboards.py` + JSON 3개 | 패널 재구성 |
| `loadtest/k6/*.js` (신규) | k6 스크립트 원본. 로컬 PC 실행과 클러스터 Job 이 **같은 파일**을 쓴다 |
| `apps/loadtest/` (신규), `argocd/apps/loadtest.yaml` (신규, **수동 sync**) | AI 단독 실험용 작은 k6 Job(요청 100m / 128Mi), 스크립트는 configMapGenerator 로 위 파일에서 생성 |
| `platform/monitoring/values.yaml` | 주석만: "알람은 CloudWatch 5개" → 실제(알람 없음) |
| `README.md` | 구조 갱신, `kubectl apply -k` 잔재 정리 |

**infra (`parking-guardian-infra/`)** — 단계 5 에서 plan 만, apply 는 승인 뒤

| 파일 | 변경 |
|---|---|
| `terraform/stacks/data/storage_ecr.tf`, `outputs.tf` | 저장소 집합에 `inference` (CI 푸시 권한은 자동 포함 [확인]) |
| `terraform/stacks/cluster/iam.tf`, `variables.tf` | inference 역할(모델 읽기) + Pod Identity 연결, backend 역할에서 모델 권한 제거 |
| `terraform/stacks/cluster/addons.tf` | **3.12 결정에 따름** (O0 버전 고정 / O1 중복 구성 요소 끄기 등). 결정 전에는 변경 없음 |
| `docs/phase1/architecture.md` (로컬 전용) | 머리말 "이 문서의 Phase 1 = 새 명칭의 Prototype", 알람 5개 미구현 표시 |

노드 그룹·노드 수 변경 없음. **device**: Phase 1 변경 없음.

### 9.2 예상 리소스 배치 (v4, 재측정 전 잠정)

> 아래 CPU·메모리 Request/Limit 값은 **추정이며 확정이 아니다.** 로컬 Linux 측정(inference 최대 RSS 1363 MiB, `phase1/local/docker-split.md`)은 참고일 뿐이고, EKS 의 E0/S0 를 실측한 뒤 조정한다. torch 스레드 수도 E0/E1 에서 정한다.

운영과 측정 모두 **공용 노드 그룹 m7i-flex.large 3대**다. 추가 노드 없음.

| 노드 | 평상시 / 비교 A (api 2 + inference 2 고정) | CPU 요청 합 / 1,930m |
|---|---|---|
| N1 | inference, api, DaemonSet | 1,850m |
| N2 | inference, api, DaemonSet | 1,850m |
| N3 | frontend, Argo CD, 모니터링, ESO, ALB 컨트롤러, (AI 단독 실험 때 k6 Job 100m) | 약 800~900m |

| 상황 | 배치 | 비고 |
|---|---|---|
| S4 프로필 1: inference 최대 3 + api 최대 2 | inference 3번째 → N3 (약 1,800m) | 들어간다 |
| S4 프로필 2: api 최대 3 + inference 최대 2 | api 3번째 → N3 (약 400m) | 들어간다 |
| S4 프로필 3: 둘 다 최대 (api 3 + inference 3) | 노드마다 inference 1 + api 1 (1,200m / 1,280m) | 들어간다, **여유 30m**. 노드에 묶이지 않는 Pod 가 80m 를 넘으면 안 들어간다 |
| inference 3 + api 3 이상 | api 3번째 Pending | CloudWatch add-on 현행 기준의 용량 한계. 결과로 기록 |
| P1 (선택) | api 2개 → N3 고정, inference N1·N2 | 그 회차만 노드 단위 고가용성 포기 |
| V3 (주입) | inference 2개 → 한 노드 | preferred 의 대가 측정 |

재구축 직후 노드별 요청 합을 다시 재고, CloudWatch add-on 이 v7 구성 요소(nodeExporter, kubeStateMetrics)를 추가로 띄웠는지 확인한 뒤 이 표를 갱신한다(3.12).

### 9.3 테스트 전략

| 층 | 무엇을 | 단계 |
|---|---|---|
| 단위 | AI 서비스 대기 상한·상태별 응답, `HttpInference` 의 타임아웃·재시도·동시 상한·예외 매핑, readyz | 2 |
| 계약 | API ↔ AI 서비스: 같은 프로세스에서 AI 앱의 test client 를 HTTP 대상으로 써서 요청·응답 형식 확인 | 2 |
| 회귀 | 기존 57개 + 신규 테스트, SQLite·PostgreSQL. device 17개 | 2, 4 |
| 로컬 통합 | Docker Compose(db, backend, inference, frontend). AI 정지·지연 시 관리자 API 정상, 탐지 503 이 마감 안에 나오는지 | 3 |
| 장비 시뮬레이터 | `device/pc_event_test.py` 로 sample_b(위반 확정 → 출차 종료), sample_c(정상) 시나리오. 결과를 `docs/measurements/PC_VERIFICATION.md` 의 Prototype 결과와 비교 | 4 |
| 클러스터 확인 | readyz, 장비 사진 2장, inference 0개일 때 관리자 API 정상, 대시보드 쿼리 전부 데이터, V1 | 6 |
| 실험 | E0~E3, T1~T3 (클러스터 안 k6 Job) | 6 |
| 부하 | S0~S5, X1, V2·V3, (선택) P1. 로컬 PC → ALB, 각 3회 | 6 |

### 9.4 비용 [추정]

| 항목 | 예상 |
|---|---|
| 클러스터 운영 (노드 3대 + 플랫폼 + NAT) | 시간당 약 0.52달러 (10-28 기준). 쓸 때만 켬 |
| 추가 노드 | **없음** |
| ECR `inference` | 월 수십 센트 이하 |
| k6 | 로컬 PC 0원. AWS 로 들어오는 전송 무과금. ALB 처리량 요금은 소량 |
| Phase 1 실험·테스트 전체 | 세션 2~3회 × 3~4시간 → 총 수 달러 |
| 재구축 | 매 세션 재구축·해체 약 40분이 과금 시간에 포함 |
| CloudWatch Logs·Container Insights | 사용량 기반, 미확인 [확인 필요] |

### 9.5 주요 위험

| 위험 | 영향 | 대응 |
|---|---|---|
| CloudWatch add-on 버전 미고정 | 재구축 때 v7 이 nodeExporter·kubeStateMetrics 를 추가로 띄우면 노드당 256m 가 더 예약되어 3노드 배치가 깨짐, 9100 포트 충돌 가능 | 재구축 직후 확인(3.12). 문제가 있으면 O0/O1 결정 요청 |
| N1·N2 CPU 요청 여유 약 80m | 요청 합이 조금만 늘어도 Pending | 실측으로 요청값 재설정, 3.12 결정 |
| preferred 배치의 쏠림 | 롤링·노드 교체 뒤 inference 가 한 노드에 몰려 그 노드 장애 때 탐지 중단 | V1 로 기록, V3 로 영향 측정. 몰리면 `kubectl rollout restart` 로 재분산 |
| 공존 시 CPU 경합 | 측정 해석 오염 | 3.7.1 의 지표 구분 + P1 |
| 로컬 PC 네트워크 변동 | 측정 흔들림, 부하 발생기 병목 | 유선, 사전 속도 측정, 3.8 의 무효 판정 기준 |
| api 이미지 메모리 미측정 | 요청값 오류 | 단계 3 에서 측정 |
| RDS 연결 수 | api 스레드 8 × Pod 4 + overflow 가 한도 초과 가능 | 단계 5 에서 `max_connections` 확인, 풀 크기 명시 |
| m7i-flex 성능 변동 | 측정 흔들림 | steal 기록, 3회 반복, 같은 시간대 |
| torch 스레드 안전성 미검증 | 락 제거 불가 | 락 유지, 동시성은 Pod 수로만 |
| gunicorn graceful timeout 30초 vs grace 130초 | S5(c) 결과 왜곡 | **결정(2-6):** inference 는 `graceful_timeout = 30` 을 설정 파일에 명시(가장 긴 정상 요청은 (1 + 대기 2) × 수 초). API 는 Prototype 동작 유지를 위해 바꾸지 않는다(기본 30). `terminationGracePeriodSeconds` 와의 정합은 5-4 에서 맞춘다 |
| **RDS 자동 재시작 (2026-11-04)** | 정지 7일 뒤 자동으로 켜져 과금 | 그 전에 재구축하거나 다시 정지 |
| CI 순서 | ECR `inference` 가 없는데 CI 가 먼저 바뀌면 빌드 실패 | ECR 생성 뒤 CI 변경을 push |
| 결과가 Phase 2 근거를 주지 않음 | 로드맵 변경 | 사전 등록 반대 결과 조항대로 |

### 9.6 구현 순서 (v4)

원칙: 단계마다 "적용해줘" 지시 뒤 진행. 커밋은 작게 나누되 **커밋·push 는 승인 뒤**. 🔒 = AWS 변경(승인 필요). 단계 1~4 는 로컬에서만 하고 AWS 를 쓰지 않는다.

**1. Prototype 보존** (로컬)

| 번호 | 작업 | 완료 기준 |
|---|---|---|
| 1-1 | 세 레포 상태 확인(작업 트리, 원격과의 차이. 앱 레포는 문서 커밋 `7d55142` 가 아직 push 전) | 차이 목록 |
| 1-2 | 주석 태그 `prototype-v1`: 앱 `d354c49`(Prototype 마지막 코드 커밋), config `749bf3a`(2026-10-30 HEAD, 대시보드 수정 포함), infra `55c49ef` | 태그 생성(push 는 승인 뒤) |
| 1-3 | `docs/architecture/prototype.md`: 구조, gunicorn 1 × 4, 리소스·HPA·PDB·anti-affinity 값, 측정 문서 링크, 태그. `.gitignore` 허용 목록 추가 | 민감정보 검사 통과 |
| 1-4 | infra 로컬 문서 머리말 주석(추적 안 됨) | — |

**2. Flask API 와 AI 추론 서비스 분리** (로컬, 테스트 먼저)

| 번호 | 작업 | 완료 기준 |
|---|---|---|
| 2-1 | 계약 고정: `InferenceResult.to_dict` / `from_dict` 왕복 테스트 | 테스트 통과 |
| 2-2 | `inference_service/` 앱: `/v1/analyze`, `/healthz`, `/readyz`. 백엔드는 `build_inference` 재사용(테스트는 Mock) | Mock 으로 200·400·503 테스트 |
| 2-3 | 대기 상한(`INFERENCE_MAX_WAITING`) → 503 busy + `Retry-After`, `pg_inference_rejected_total`, 테스트 전용 지연 주입 | 동시 요청 테스트에서 상한 초과분 즉시 거절 |
| 2-4 | `HttpInference`: 연결 타임아웃, 전체 마감, busy·연결 실패 1회 재시도, 동시 상한, 예외 매핑, AI 클라이언트 메트릭 4개 | 2-2 의 test client 를 대상으로 한 계약·실패 테스트 통과 |
| 2-5 | 설정 키, `build_inference` 의 `http` 분기, readyz `remote` | `device.py` 무변경으로 기존 장비 테스트 통과 |
| 2-6 | 진입점: `wsgi_inference.py`, `gunicorn_inference.conf.py`(웜업·메트릭 서버), `gunicorn.conf.py` 는 로컬 추론일 때만 웜업, `TORCH_NUM_THREADS`. graceful timeout 명시 여부 결정. **AI 서비스 worker 1개 전제(3.5) 강제 확인** — `AdmissionGate` 는 프로세스 안 제한이므로 worker 2개 이상이면 상한이 깨진다 | 로컬에서 두 프로세스로 실제 모델 판정 1장 |
| 2-7 | 전체 pytest (SQLite, 그리고 `docker compose up -d db` 뒤 PostgreSQL) | 전부 통과 |

**3. 로컬 Docker Compose 통합**

| 번호 | 작업 | 완료 기준 |
|---|---|---|
| 3-1 | Dockerfile 멀티 타깃. `api` 는 torch·YOLOv5·OpenCV 없음 | 두 이미지 빌드, `api` 이미지에서 `import torch` 실패 확인. **완료 2026-10-30**: api 343 MB / inference 2.74 GB / prototype 2.89 GB (`phase1/local/docker-split.md`) |
| 3-2 | docker-compose: inference(`/models` 마운트, 헬스체크), backend `INFERENCE_URL=http://inference:8000` | `docker compose up -d --build` 후 화면·API 정상. **inference 는 Linux gunicorn(`gunicorn_inference.conf.py`)에서 실제 YOLO 모델로 실행한다 — 2-6 에서는 Windows 실제 모델과 Linux mock 을 따로 확인했으므로 이 조합은 3단계에서 반드시 검증한다** |
| 3-3 | 측정: 이미지 크기, 유휴 RSS(`docker stats`), 첫 추론 시간, 사진 1장 왕복 시간. `scripts/check_inference_service.py` 와 같은 항목(판정 일치, 동시 4장, 모델 실패 경로, 스레드 1/2/4 의 CPU 사용 비율)을 컨테이너에서 다시 실행하고, **`OMP_NUM_THREADS` 가 torch import 전에 적용되었는지**(스레드 1일 때 CPU/벽시계 ≈ 1)와 torch 기본 스레드 수를 기록 | `docs/measurements/phase1/local/` 기록 |
| 3-4 | 장애 확인: `docker compose stop inference` → 관리자 API 정상, 탐지 503. 지연 주입 → 마감 안에 503. 재시작 → 복구 | 결과 기록. **완료 2026-10-30** (`phase1/local/compose-drills.md`). 3-2/3-3 도 같은 날 완료: Linux gunicorn + 실제 YOLO 판정 6/6 동일 (`phase1/local/linux-inference-check.md`) |

**4. 회귀 테스트와 PC 장비 시뮬레이터**

| 번호 | 작업 | 완료 기준 |
|---|---|---|
| 4-1 | backend pytest(SQLite·PostgreSQL), device 17개 | 전부 통과. **완료 2026-10-30**: backend 211(SQLite·PostgreSQL), device 17 (`phase1/local/step4-simulator.md`) |
| 4-2 | `send_detection.py sample_b ×2` → 위반 확정, 알림 1건 | Compose 환경에서 확인. **완료**: Prototype·Phase 1 모두 200 → 201, 열린 위반 1건. 이 전송은 장비에 열린 사건을 남기므로 시뮬레이터 실행 **뒤**에 한다 |
| 4-3 | `pc_event_test.py` sample_b(arrived → violation_pending → confirm → violation → left → 종료), sample_c(permitted) | Prototype 기록(PC_VERIFICATION)과 판정·사건 결과 동일 | **완료**: 사진 구간 900초로 arrived → confirm → violation → refresh → left 전체, 정상 차량 arrived → refresh → left. Prototype과 이벤트·응답·상태 변화·하트비트·서버 결과 동일(Phase 1 실행 2). 실행 1의 하트비트 차이와 버린 실행은 `step4-simulator.md` 에 구분해 기록
| 4-4 | (선택) `scripts/measure.py` event 시나리오로 Prototype 대비 서버 시간 비교 | 기록. **하지 않음**(선택 항목) |
| 4-5 | 결과 문서 정리, 민감정보 검사 | 커밋 승인 요청. 문서 정리·검사 완료, 커밋은 승인 대기 |

**5. AWS 인프라 변경 검토** (plan 만)

| 번호 | 작업 | 완료 기준 |
|---|---|---|
| 5-1 | 읽기 전용 확인: RDS `max_connections`, CloudWatch add-on 최신 기본 버전 | 9.5 확인 필요 항목 정리. **완료 2026-10-30** (`phase1/aws-review/aws-review.md` 6, 7절). RDS 는 정지 상태라 실제 값은 못 읽고 공식·상한(112)·시나리오로 계산, 기본 버전은 v7.0.0 |
| 5-2 | infra 변경 작성 + `plan -out`: ECR `inference`(data), inference 역할·Pod Identity·backend 권한 축소(cluster) | plan 결과 보고. **완료**: data 2 add / 1 change, cluster 34 add (기준 31 + 3). 체커 `infra/scripts/check-phase1-plan.py` 통과. apply 는 하지 않음 |
| 5-3 | CloudWatch add-on 결정 요청(3.12 의 O0~O4) | 사용자 결정. O1 은 이전에 선택됨. **구현**: v6.7.0 고정 + 두 구성 요소 명시적 끔(P1). **버전(P1 대 P2=v7.0.0) 확정은 사용자 결정 대기** |
| 5-4 | config 매니페스트 작성, `kubectl kustomize` 로 렌더링 확인. **inference livenessProbe 가 `/healthz` 를 보는지, 모델 로딩 포기(3회 실패) 시 컨테이너가 재시작되는 경로가 맞는지 검증** (3.5) | 렌더링 통과. **렌더링 완료 2026-10-30**: 21개 오브젝트, `config repo/scripts/check-phase1-render.py` 통과. 단 실제 `config.env` 에 `ECR_INFERENCE` 가 없어(ECR 생성 전) 저장소 그대로는 렌더링이 실패한다 — ECR apply 뒤 `config.env` 재생성 필요. Probe 값은 EKS 로딩 시간으로 재조정 |
| 5-5 | CI 변경 작성(ECR 생성 뒤 push 하는 순서) | 검토. **작성 완료**: backend 는 `api` 타깃, inference 추가, 태그 갱신 3줄. 정적 테스트 8개와 actionlint 통과. ECR apply 전에 push 하면 inference 잡이 실패하고 update-config 가 막힌다 |

**6. EKS 배포와 성능 측정** 🔒

| 번호 | 작업 |
|---|---|
| 6-1 | 🔒 data 스택 apply(ECR) → CI push → 이미지 2개 확인 |
| 6-2 | 🔒 재구축(기존 순서, cluster 스택에 Pod Identity 포함). 직후 노드 요청 합과 CloudWatch 구성 요소 확인(9.2) |
| 6-3 | config push → Argo CD 동기화 → 클러스터 확인(9.3), V1. **IAM 분리 검증**: inference 서비스어카운트로 만든 임시 Pod 에서 사진 버킷·Secrets Manager 가 AccessDenied 이고 모델 버킷은 읽히는지, backend 서비스어카운트에서 모델 버킷이 AccessDenied 인지 (`phase1/aws-review/deploy-checklist.md` G7). IMDS 와 RDS 5432 도달 가능성은 **해결하지 않는 위험의 증거**로 기록 |
| 6-3b | **장애 격리 검증 (EKS)** — 로컬 Compose 에서 한 시나리오를 EKS 에서도 한다 (`phase1/local/compose-drills.md` 의 표와 같은 순서): `kubectl scale deploy/inference --replicas=0`(inference 없음), Pod 삭제(모델 로딩 중), `rollout restart`·노드 drain(재시작 중), `INFERENCE_FAULT_DELAY_MS` 주입(마감 초과), k6 동시 요청. 각 시나리오에서 **관리자 API(로그인·목록·`/readyz`)** 와 **사진 탐지(503)** 를 따로 기록. 특히: 탐지 503 이 나오기까지 시간(로컬은 DNS 실패 시도당 약 4초 + 재시도로 8.3~8.6초였다. Service 이름은 엔드포인트 없이도 해석되므로 다를 것으로 예상, 미확인), 연결 타임아웃이 1초에 실제로 끊는지, 요청 손실 구간, PDB·readiness 효과 |
| 6-4 | 사전 등록 "고정" 커밋 → E0~E3, T1~T3 (클러스터 안 k6 Job) → 공식 구성 값 커밋 |
| 6-5 | 로컬 PC k6 → ALB: S0~S5, X1, V2·V3, (선택) P1 |
| 6-6 | 결과 커밋, Phase 1 보고서, ADR 0018 "결정됨", ADR 0019 수정 |

RDS 는 2026-11-04 에 자동으로 다시 켜진다. 단계 6 을 그 전에 시작하지 않으면 한 번 다시 정지해야 한다.

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

---

## 11. 향후 개선 계획 (미구현)

> 이 장의 내용은 **구현하지 않았고 이번 계획에도 넣지 않았다.** 측정 결과나 운영 상황이 근거를 줄 때 검토한다. 포트폴리오에서 "구현한 것" 과 구분해 적는다.

| 번호 | 개선 | 검토를 시작할 조건 | 내용 | 예상 비용·복잡도 |
|---|---|---|---|---|
| F1 | **API 전용 / AI 전용 노드 그룹 분리** | 트래픽 증가, 또는 측정(P1, 지표 구분)에서 AI 의 CPU 경합이 일반 API 성능 저하의 원인으로 확인될 때 | 노드 그룹 2개. AI 노드 그룹에 taint(`workload=inference:NoSchedule`)와 라벨, inference·Worker 에 toleration + nodeSelector. 워크로드별 인스턴스: API 는 작은 범용, AI 는 연산 최적화(c7i 계열) 또는 Graviton(arm64 이미지 필요) | 노드 그룹·IAM·Launch Template 추가, 최소 노드 수 증가. Free plan 인스턴스 제약 확인 필요 |
| F2 | AI 노드 Spot 사용 | F1 뒤, Phase 2 (큐가 있어 Worker 중단을 견딤) | Spot 회수 시 SQS 재전달로 복구 | 중단 처리(SIGTERM 시 Visibility 반환) 필요 |
| F3 | 알림 경로 | 지금 없음(3.12) | CloudWatch 알람(ALB 5xx, UnHealthyHost, RDS 디스크) 또는 Alertmanager → 이메일 | 작음 |
| F4 | 클러스터 해체 뒤에도 남는 로그·지표 | 장기 운영 | 로그 그룹을 cluster 스택 밖으로, Prometheus 원격 저장 | 저장 비용 |
| F5 | 다중 칸 장비 | 장비 1대가 여러 주차면을 볼 때 | 사건 상태를 Machine 에서 ParkingZone 으로, MessageGroupId 를 zone 으로 | 데이터 모델 변경 |
| F6 | inference 멀티프로세스 / 스레드 안전성 검증 | E2 에서 프로세스 수 효과가 클 때 | 모델 동시 호출 안전성 검증 뒤 락 범위 재검토, prometheus-client 멀티프로세스 모드 | 검증 비용 |
| F7 | **Pod 의 노드 IAM 역할 경로 차단 / RDS 포트 차단** | 보안 강화 단계, 또는 CloudWatch 에이전트·ALB 컨트롤러·EBS CSI 가 IMDS 를 쓰는지 확인된 뒤 | IMDS 홉 제한 1 (확인 전에는 깨질 수 있음), 또는 VPC CNI 네트워크 정책을 켜고 inference 에서 `169.254.169.254` 와 RDS 5432 이그레스 차단 | 애드온 설정 변경, 정책 검증 |
| F8 | **AI 호출 전에 DB 세션 반환** | AI 가 느릴 때 DB 연결 점유가 문제로 측정될 때 | `require_device` 가 연 트랜잭션을 AI 호출 전에 닫으면 AI 대기 중 연결 점유가 사라진다 | `device.py` 수정이라 판정·이벤트 규칙 불변을 테스트로 보장해야 함, 별도 승인 |
