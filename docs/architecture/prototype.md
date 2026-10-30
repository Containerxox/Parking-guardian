# Prototype 구조 (태그 `prototype-v1`)

2026-10-30 단계 재정의에 따라 지금까지의 구조를 **Prototype** 이라 부른다. Flask API 와 YOLO 추론이 **한 Pod 안**에서 돌아가는 구조다. 이 문서는 Phase 1(API / AI 서비스 분리)과 비교할 때 되돌아볼 기록이며, 공식 비교 기준선(baseline)으로 쓰려는 것은 아니다. 값은 태그 시점의 코드와 매니페스트에서 옮겼다.

## 태그

| 레포 | 태그 | 커밋 | 비고 |
|---|---|---|---|
| 앱 (`Parking-guardian`) | `prototype-v1` | `d354c49` | Prototype 마지막 코드 커밋. 이후 커밋은 문서뿐 |
| config (`parking-guardian-config`) | `prototype-v1` | `749bf3a` | 배포 태그 `94c294f` 뒤의 대시보드 수정 포함 |
| infra (`parking-guardian-infra`) | `prototype-v1` | `55c49ef` | |

## 구조

```text
Raspberry Pi ──HTTPS──▶ ALB ──▶ Service backend:5000 ──▶ backend Pod (Flask + YOLO 두 모델 + 락)
                                                          ├─ RDS PostgreSQL
                                                          ├─ S3 사진 버킷
                                                          └─ initContainer: S3 에서 모델 412 MB 다운로드
```

- 판정(앞유리 탐지 → 스티커 탐지)이 API 프로세스 안에서 일어난다. 추론은 `threading.Lock` 으로 한 번에 하나만 실행한다.
- 모델은 백그라운드로 로딩한다(`YoloInference.warm_up`). 로딩 중에는 `/readyz` 가 503 이라 트래픽을 받지 않는다.
- DB 마이그레이션은 Pod 시작이 아니라 `backend-migrate` Job(Argo CD sync-wave 1)이 한다. Deployment 는 wave 2.

## 설정값 (태그 시점)

| 항목 | 값 | 출처 |
|---|---|---|
| gunicorn | worker 1 × thread 4, timeout 120초 | `backend/Dockerfile` CMD 기본값 (`WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT`) |
| 노드 | m7i-flex.large × 3 (공용 노드 그룹) | infra `cluster` 스택, desired 는 CLI 로 3 |
| Pod 자원 | 요청 CPU 1 / 메모리 1.5Gi, 메모리 limit 2Gi, CPU limit 없음 | `backend-deployment.yaml` |
| HPA | 2 ~ 3, CPU 평균 70%, 확장 안정화 0초(1 Pod/60초), 축소 안정화 300초 | `backend-hpa.yaml` |
| PDB | `minAvailable: 50%` (올림: 2개→1, 3개→2) | `backend-pdb.yaml` |
| 배치 | podAntiAffinity **required**(hostname) + zone spread(ScheduleAnyway) | `backend-deployment.yaml` |
| 롤링 업데이트 | maxSurge 1 / maxUnavailable 0 | `backend-deployment.yaml` |
| 종료 유예 | `terminationGracePeriodSeconds` 130 | `backend-deployment.yaml` |
| 프로브 | startup `/healthz` 5초 × 12, readiness `/readyz` 5초 × 3, liveness `/healthz` 10초 × 3 | `backend-deployment.yaml` |
| 메트릭 | 별도 포트 9102, PodMonitor, 대시보드 3개 | `app/metrics.py`, config `platform/monitoring/` |
| 이미지 | backend 약 2.89 GB (torch 포함) | `docs/measurements/OCR_REMOVAL.md` |

## 알려진 비용 (Phase 1 에서 다룰 문제)

- 확장 단위가 크다. 일반 API 부하만 늘어도, 추론이 늘어도 모델(Pod 당 약 1 GiB 이상 상주)을 포함한 Pod 전체가 늘어난다.
- API 와 추론이 한 Pod 라서 추론이 CPU 를 다 쓰면 로그인·목록 조회 같은 관리자 API 도 같은 Pod 의 스레드 4개를 나눠 쓴다.
- `/readyz` 가 모델 상태에 묶여 있다. 모델 로딩 중인 Pod 는 관리자 API 도 받지 못한다.
- 새 Pod 하나가 큰 이미지 Pull 과 모델 다운로드·로딩을 모두 치른다.

위 항목은 구조에서 나온 추론이다. 수치로 확인된 것은 아래 측정 문서에 있고, 분리 뒤의 비교 실험은 `docs/measurements/phase1/README.md` 의 사전 등록 계획을 따른다.

## 관련 기록

| 문서 | 내용 |
|---|---|
| `docs/measurements/README.md` | interval / event 모드 Before/After |
| `docs/measurements/PC_VERIFICATION.md` | PC 장비 시뮬레이터 검증 (Phase 1 회귀 비교 대상) |
| `docs/measurements/DOCKER_SIMULATION.md` | Docker 환경 통합 검증 |
| `docs/measurements/OCR_REMOVAL.md` | OCR 제거 전후 이미지 크기·메모리 |
| `docs/adr/0013`, `0016`, `0017` | 컨테이너 구성, S3 사진 저장, OCR 제거 |
| `docs/design/ARCHITECTURE_EVOLUTION.md` | Prototype → Phase 1 → Phase 2 설계 |

## 되돌아보는 방법

```text
git checkout prototype-v1        # 각 레포에서
```
