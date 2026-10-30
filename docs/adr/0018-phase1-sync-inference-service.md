# 0018. AI 추론을 별도 서비스로 분리하고, API 는 타임아웃과 동시 호출 상한을 둔 동기 HTTP 로 부른다

- 상태: **제안** (2026-10-30). 설계만 확정했다. 구현과 측정이 남았다. 측정 뒤 값과 결과를 채우고 "결정됨"으로 바꾼다.
- 배경: 아키텍처 단계 재정의(2026-10-30). 지금까지 EKS 에 올린 "Flask API + YOLO 한 Pod" 구조를 **Prototype** 으로 부르고, 이 결정이 만드는 구조를 **Phase 1**(첫 공식 비교 기준)으로 부른다. 인프라 설계 문서(`parking-guardian-infra/docs/phase1/`, 비공개)와 Ingress 태그 `Phase=1` 은 옛 명칭으로 Prototype 을 가리킨다.
- 전체 설계: `docs/design/ARCHITECTURE_EVOLUTION.md` 3장(상세), 4장(실험), 5장(부하 테스트).
- 유지하는 결정: 0005(판단은 서버), 0006(재압축 금지), 0007(추론은 Adapter 뒤), 0013(모델 파일은 이미지 밖), 0014(이벤트 전송 프로토콜. 장비는 바뀌지 않는다).

## 문제

Prototype 은 API 와 추론이 한 프로세스에 있다. gunicorn worker 1 × thread 4 가 관리자 API, 장비 사진, 헬스체크를 함께 처리하고, 추론은 `threading.Lock` 하나로 직렬 실행된다. 그래서 세 가지가 묶여 있다.

- **자원**: Pod 하나가 CPU 1코어, 메모리 1.5 GiB, 이미지 2.9 GB, 모델 412 MB 다. 관리자 API 용량을 늘리려 해도 이 단위로 늘려야 한다.
- **확장 지표**: CPU 기준 HPA 는 관리자 트래픽과 추론 부하를 구분하지 못한다.
- **장애 범위**: 추론이 스레드를 붙잡으면 같은 스레드 풀의 관리자 API 와 `/readyz` 가 함께 기다린다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| 같은 Pod 의 사이드카 컨테이너 | 프로세스는 갈라지지만 스케줄링·확장 단위가 같아 자원·확장 분리가 안 된다 |
| TorchServe, NVIDIA Triton, GPU | 범위 밖(학습 비용, Free plan 인스턴스 제약) |
| 바로 비동기(S3 + SQS) | 동기 분리의 한계를 재기도 전에 큐, 아웃박스, 장비 프로토콜 변경이 한꺼번에 들어온다. Phase 2(0019)로 둔다 |
| **inference 를 별도 Deployment·Service 로 분리하고 API 는 HTTP Adapter 로 동기 호출** | 채택 |

## 결정

- inference 서비스: 기존 `YoloInference` 를 그대로 쓰는 Flask 앱. `POST /v1/analyze`(원본 바이트), `/healthz`, `/readyz`(모델 ready 일 때만 200), 메트릭 9102. DB 에 접속하지 않고 Secret 을 마운트하지 않는다. AWS 권한은 모델 버킷 읽기뿐이다.
- **락은 유지한다.** hub 모델의 스레드 안전성을 검증한 기록이 없다. Pod 당 프로세스 1개, 모델 1개, 동시 추론 1개에서 시작하고 처리량은 Pod 수로 늘린다.
- inference 는 **대기 상한**(`INFERENCE_MAX_WAITING`)을 넘는 요청을 락에서 기다리게 하지 않고 즉시 `503 busy` 로 돌려준다.
- API: `HttpInference` Adapter. 연결 타임아웃, 전체 마감, busy·연결 실패에 한해 1회 재시도, **동시 호출 상한**(`AI_CLIENT_MAX_CONCURRENCY`). 실패는 기존 `InferenceError`(503 `inference_unavailable`)로 바꿔 `device.py` 와 장비의 계약을 그대로 둔다.
- **API 의 `/readyz` 는 AI 상태를 보지 않는다.** AI 장애가 관리자 API 의 readiness 를 끌어내리지 않게 한다.
- 상한 값(스레드 절반, 대기 2 등)은 고정값이 아니다. 설계 문서 4.2절의 **선택 규칙을 테스트 전에 등록**하고 실험 결과로 정한다. 상한을 끈 대조 실험(X1)은 공식 구성과 분리해 보고한다.
- 확장 정책을 분리한다. api 와 inference 가 각자의 HPA·PDB·PodMonitor 를 갖는다.
- **배치(2026-10-30 사용자 결정)**: 기존 공용 노드 그룹 3대를 그대로 쓰고 API 와 AI Pod 의 같은 노드 배치를 허용한다. Pod 를 분리했다고 노드까지 분리하지 않는다. 같은 종류끼리의 분산은 required 가 아니라 preferred 로 하고, HPA 최대값은 노드 수가 아니라 노드별 CPU 요청 여유로 정한다. preferred 가 분산을 보장하지 않는 점은 배치 기록과 노드 장애 실험(V1~V3)으로 확인한다. 노드 그룹 분리는 구현하지 않고 향후 개선으로만 둔다(설계 문서 11장 F1).
- 이름: API 는 기존 `backend` 이름을 유지한다(Ingress, Service, ALB 헬스체크, 마이그레이션 Job 이 그대로). 새로 `inference-*` 를 더한다.

## 이유

- Ports & Adapters(0007) 덕분에 API 가 아는 것은 `analyze(bytes, filename) -> InferenceResult` 하나다. 분리의 코드 변경이 Adapter 하나와 진입점 하나로 끝난다.
- 타임아웃과 동시 상한이 없는 동기 클라이언트는 운영 구성이 아니다. 그것을 빼고 측정하면 Phase 1 을 일부러 약하게 만들어 Phase 2 를 돋보이게 하는 것이 된다.
- 상한을 두면 한계는 "일반 API 지연" 대신 "과부하 시 사진 거절" 로 나타날 수 있다. 장비에는 재시도가 없으므로(0014 MVP) 거절은 다음 refresh 까지의 유실이다. 이것이 Phase 2 의 근거가 되는지는 측정이 정한다.

## 결과

- 얻을 것(예상, 측정 전): API Pod 가 작은 이미지와 작은 요청값으로 줄어든다. AI 장애가 관리자 API 로 번지지 않는다. 확장 지표가 각 워크로드를 대변한다.
- 감수할 것: 네트워크 홉 1개와 사진 바이트 재전송, 이미지 2개와 CI 1개, 매니페스트 약 2배. **동기 대기라는 제약은 남는다.**
- 측정할 것: 설계 문서 5장 S0~S5. 비교 A(동일 자원)와 비교 B(자동 확장)를 나눈다. 사전 등록은 `docs/measurements/phase1/README.md`.

## 근거

- 코드: `backend/app/adapters/inference.py`(포트, 락, 상태), `backend/app/api/device.py`(예외 계약), `backend/app/api/health.py`(readyz), `backend/app/metrics.py`(별도 포트 메트릭).
- 노드 자원(2026-10-28 `kubectl describe nodes`): allocatable 약 1,930m / 7.2 GiB, 비-backend 요청 약 650m. 배치 검토는 설계 문서 3.7절.
- 추론 시간은 PC 측정(평균 557~866 ms)뿐이고 EKS 값은 없다.

## 한 줄 설명

추론을 내부 서비스로 떼어 자원·권한·확장을 나누되, 동기 호출에는 타임아웃과 동시 상한을 걸어 안전한 운영 구성에서 동기 방식의 실제 한계를 재기로 했습니다.
