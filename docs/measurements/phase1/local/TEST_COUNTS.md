# Phase 1 구현 중 백엔드 테스트 개수 기록

`python -m pytest tests -q` (SQLite) 의 통과 개수. 파일별 개수는 `--collect-only` 로 센 값이다. 단계마다 늘어난 수를 남겨 두어, 나중에 "어느 단계에서 무엇이 늘었는가" 를 확인할 수 있게 한다. PostgreSQL 실행은 단계 2-7 에서 하고 그 결과는 이 표 아래에 따로 적는다.

## 단계별 누적

| 단계 | 커밋 | 늘어난 수 | 누적 | 늘어난 내용 |
|---|---|---|---|---|
| Prototype (`prototype-v1`) | `d354c49` | – | 57 | 기준 |
| 2-1 계약 고정 | `888832c` | +16 | 73 | `test_inference_contract.py` 16 |
| 2-2 추론 서비스 앱 | `fa719cc` (일부) | +20 | 93 | `test_inference_service.py` 20 |
| 2-2 보완 | `fa719cc` | +6 | 99 | 의존성 격리 1, 모델 로딩 재시도 3, 서비스 healthz 2 (실패 상태 테스트 1개는 교체라 증가 없음) |
| 2-3 대기 상한 | `edcd784` | +9 | 108 | `test_inference_admission.py` 9 |
| 2-4 HttpInference | `f7d8a0f` | +47 | 155 | `test_inference_http.py` 45, 게이트가 프로세스 단위임을 보이는 테스트 1(admission), 파일명 디코딩 1(service) |
| 2-5 http 백엔드·readyz | (미커밋) | +21 | 176 | `test_http_backend.py` 21 |

## 176개의 파일별 내역

| 구분 | 파일 | 개수 |
|---|---|---|
| Prototype 기존 (57) | `test_config_and_seed_secrets` 5, `test_episodes` 12, `test_event_mode_integration` 2, `test_health` 6, `test_metrics` 5, `test_operator_indicators` 1, `test_permissions` 3, `test_storage_s3` 4, `test_tenant_isolation` 7, `test_violation_flow` 12 | 57 |
| Phase 1 신규 (119) | `test_inference_contract` 16, `test_inference_service` 23, `test_inference_service_isolation` 1, `test_inference_model_load` 3, `test_inference_admission` 10, `test_inference_http` 45, `test_http_backend` 21 | 119 |

Prototype 기존 57개는 Phase 1 작업 중 한 번도 수정하지 않았고 모두 통과한다 (Prototype 판정·응답 규칙 유지 확인).

## 아직 없는 것

- PostgreSQL 에서의 전체 실행 (2-7)
- 실제 모델(YOLO)로 두 프로세스를 띄운 판정 1장 (2-6)
- 닫힌 포트 연결 거절 시간: Windows 루프백에서 한 번에 약 2초가 걸리는 것을 raw socket 으로 확인했다. Linux 컨테이너에서의 값은 측정하지 않았다.
