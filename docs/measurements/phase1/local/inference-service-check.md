# 추론 서비스 실제 모델 검증 (단계 2-6, 2026-10-30)

`backend/scripts/check_inference_service.py` 의 출력과 gunicorn 확인 결과. **이 PC(Windows, 물리 4코어 / 논리 8코어)의 값**이다. EKS 노드(m7i-flex.large, 2 vCPU)와 Linux 컨테이너의 값이 아니므로 절대 수치로 쓰지 않고, 같은 PC 안에서의 비교와 동작 확인에만 쓴다.

## 무엇을 어디서 확인했나

| 확인 | 환경 | 실제 모델 | 결과 |
|---|---|---|---|
| 서비스 동작, 판정, 메모리, 스레드, 모델 로딩 실패 | Windows, `python wsgi_inference.py` (werkzeug 스레드 서버) | **YOLO 실제 모델** | 아래 1~5 |
| gunicorn 설정(worker 1개 강제, 스레드 경고, 훅, 동시 요청, 프로브) | Linux 컨테이너(`python:3.12-slim`, gunicorn 26.2.0, gthread) | mock (torch 없음) | 아래 6 |
| **실제 모델을 gunicorn 아래에서** | – | – | **아직 안 함.** 3단계(Compose, Dockerfile 멀티 타깃)에서 한다 |

gunicorn 은 Windows 에서 `grp` 모듈이 없어 import 도 되지 않는다. 그래서 위 둘로 나누었다.

## 1. 시작, 메모리, 스레드 (실제 모델)

- `/readyz` 200 까지 **28.4초** (프로세스 시작과 torch import 포함). 서비스 메트릭 `pg_model_load_seconds` 는 이 실행에서 18.8초였다. 앞선 실행에서는 30.9초가 나왔다(디스크 캐시 차이로 보이나 원인은 확인하지 않음).
- RSS 모델 로딩 직후 **692 MiB**, 사진 12장 뒤 **748 MiB**, 최대 작업 집합 **929 MiB**. 서버 프로세스 스레드 6개.
- 사진 1장 약 **3.1~4.6초**(torch 스레드 1개, 서비스 시간 `elapsed_ms` 와 왕복 시간 차이는 0.1초 안팎).
- 서비스 CPU 시간 / 벽시계 시간 중앙값 **0.93**: 사진 한 장을 처리하는 동안 코어 약 1개만 쓴다(스레드 1개가 실제로 적용됨).

| 사진 | 상태 | 판정 | 스티커 | 서비스 ms | 왕복 s |
|---|---|---|---|---|---|
| sample_a | 200 | permitted | 1 | 3255 | 3.35 |
| sample_b | 200 | violation | 0 | 3042 | 3.11 |
| sample_c | 200 | permitted | 1 | 3547 | 3.81 |
| sample_d | 200 | violation | 0 | 4143 | 4.56 |
| sample_e | 200 | permitted | 1 | 3708 | 4.04 |
| sample_f | 200 | no_vehicle | 0 | 1741 | 2.52 |

## 2. 동시 요청 4개 (INFERENCE_MAX_WAITING=2, 실제 모델)

| 상태 | Retry-After | 오류 | 초 |
|---|---|---|---|
| 503 | 1 | busy | 0.05 |
| 200 | | | 3.28 |
| 200 | | | 6.48 |
| 200 | | | 9.85 |

설계대로 3장 처리(실행 1 + 대기 2), 1장 즉시 거절. 처리된 3장이 순서대로 약 3.3초씩 걸렸다. **대기 2장째의 응답이 9.85초**라서 API 쪽 기본 마감 10초(`AI_CLIENT_DEADLINE`)에 거의 닿는다. 스레드 1개 기준의 값이며, 마감과 대기 상한은 T1/T3(설계 4.2)에서 정한다.

## 3. HTTP 로 받은 판정 = 같은 사진을 프로세스 안에서 판정한 결과

6장 모두 판정, 앞유리 상자, 스티커 상자가 **완전히 같다**(차이 0 / 6). Prototype 의 판정 규칙이 서비스를 거쳐도 유지된다.

프로세스 안(Prototype 경로, torch 기본 스레드 4개) 사진당 중앙값 **1.49초**.

## 4. TORCH_NUM_THREADS 가 서비스에 실제로 적용되는가

| TORCH_NUM_THREADS | 사진당 중앙값 s | CPU 시간 / 벽시계 시간 |
|---|---|---|
| 1 | 3.52 | 0.94 |
| 2 | 2.20 | 1.77 |
| 4 | 1.51 | 3.37 |

설정이 의도대로 적용된다(CPU 사용 비율이 스레드 수를 따라간다). 같은 PC 에서 **스레드 1개는 Prototype(기본 4개)보다 사진당 약 2.4배 느리다.** 스레드 4개 서비스는 프로세스 안 판정(1.49초)과 같은 속도다.

### 이번에 알게 된 두 가지

1. **설계 3.5 의 "지금 Pod 안 값 = 1 [확인]" 은 틀렸다.** Prototype 은 torch 스레드를 어디에서도 설정하지 않는다(코드, Dockerfile, 매니페스트 확인). torch 기본값(보이는 코어 수)으로 돈다. 이 PC 는 4, EKS 노드는 2일 것으로 추정되나 **Pod 안 값은 측정하지 않았다**. 그래서 서비스의 초기값 1은 "Prototype 값 유지"가 아니라 "E0 의 기준 가설"이다. Prototype 코드는 이번에도 바꾸지 않았다(`TORCH_NUM_THREADS` 기본 0 = 건드리지 않음).
2. **`torch.set_num_threads()` 만으로는 부족하다.** 한 스레드에서 호출하면 그 뒤에 만들어진 스레드에는 적용되지만, 이미 torch 를 쓴 스레드는 이전 값을 유지하는 것을 따로 측정으로 확인했다(호출한 스레드가 아닌 쪽의 `get_num_threads()` 가 스레드마다 달랐다). gunicorn 은 요청을 스레드 풀로 처리하므로, `wsgi_inference.py` 가 torch 를 import 하기 전에 `OMP_NUM_THREADS`/`MKL_NUM_THREADS` 환경변수를 설정한다. 위 표는 이 조합으로 측정한 값이다.

## 5. 모델을 못 읽을 때 (모델 파일 없음, 시도 3회, 대기 기본 1초)

| 시작 후 초 | `/healthz` | `/readyz` |
|---|---|---|
| 2.1 | 200 | 503 |
| 34.5 | 503 (`model_failed`) | 503 |

- 재시도하는 동안은 `/healthz` 200, `/readyz` 503 → Service 가 트래픽을 보내지 않고, liveness 는 살려 둔다.
- 3번 모두 실패한 뒤에만 `/healthz` 가 503 이 된다(kubelet 이 컨테이너를 재시작하는 경로). 이 구간에 들어온 사진은 `503 model_unavailable` 이고 모델을 다시 읽으려 하지 않는다.
- 3번 시도에 34초가 걸렸다. 대부분은 첫 시도의 torch import 와 실패 처리 시간이며, 대기 시간(1초 + 2초)은 일부다. 실제 배포에서는 `startupProbe` 가 이 시간 안에 `/healthz` 가 200 인 것을 기다려야 한다(5-4 에서 확인).

## 6. gunicorn (Linux 컨테이너, mock 모델)

`gunicorn -c gunicorn_inference.conf.py wsgi_inference:app`, gunicorn 26.2.0, `INFERENCE_FAULT_DELAY_MS=1500`.

| 확인 | 결과 |
|---|---|
| 기동 | worker `gthread` **1개**, 메트릭 서버 :9102 시작 |
| `/readyz`, `/healthz` | 200, 200 |
| 동시 요청 4개(각 1.5초) | 503(Retry-After 1) 0.01초, 200 1.51초, 200 3.02초, 200 4.52초 → **실행 1 + 대기 2 + 거절 1** 이 gunicorn 스레드 풀에서도 성립 |
| 사진 3장이 서비스 안에 있을 때 프로브 | `/healthz` 200, `/readyz` 200, 0.02초 (스레드 4개 중 1개를 프로브용으로 남겨 둔 효과) |
| `--workers 3` (명령줄) | 기동 거부: "must run exactly one worker (got 3)". 명령줄이 설정 파일을 덮어쓰므로 `on_starting` 훅이 gunicorn 의 실제 값을 본다 |
| `GUNICORN_THREADS=3` | 경고 로그 (실행 1 + 대기 2 + 프로브 1 = 4 미만), 기동은 함 |

## 7. 메모리와 CPU 설정 검토

| 항목 | 현재 계획값(설계 9.2, 추정) | 이번에 본 것 |
|---|---|---|
| inference 메모리 요청 / limit | 1.5Gi / (미정) | 이 PC 에서 RSS 692~748 MiB, 최대 작업 집합 929 MiB. 앞선 Prototype 기록(Linux, 최대 910 MB)과 비슷한 크기. Windows 값이라 Linux 에서 다시 재야 한다 |
| inference CPU 요청 | 1000m | 스레드 1개면 코어 약 1개(0.93)를 쓴다. 스레드를 늘리면 사진당 시간은 줄지만 요청값보다 더 쓴다. 2 vCPU 노드에서 inference 2개가 공존하면 서로 영향 |
| 스레드 수 | 4 | 1 running + 2 waiting + 1 probe 가 최소. 설정 파일과 시작 훅이 이를 점검 |

판단은 E0/E1 이후로 미룬다. 지금은 값을 바꾸지 않았다.

## 후속 검증 항목 (2026-10-30 사용자 지시 반영)

| # | 항목 | 현재 상태 | 언제, 어디서 |
|---|---|---|---|
| 1 | **torch CPU 스레드 수**: 1을 최종값으로 확정하지 않는다. EKS E0/E1 에서 정한다. Linux gunicorn 에서 `OMP_NUM_THREADS` 가 torch import 전에 적용되는지 확인 | 시작값 1. Windows 에서 1/2/4 = 3.52/2.20/1.51초 | 3-3 (Linux 컨테이너: 스레드 1일 때 CPU/벽시계 ≈ 1 인지), 6-4 (EKS E0/E1) |
| 2 | **AI 응답 지연**: 대기 줄 끝 사진 9.85초 vs 마감 10초. 타임아웃·대기 상한은 임의로 바꾸지 않는다 | 값 변경 없음. 관찰 항목으로 설계 4.2 와 사전 등록 E0 에 기록 | 6-4 (E0, T1, T3) |
| 3 | **모델 로딩 시간과 Probe**: 로컬 준비 완료 **28.4초**(앞선 실행 `pg_model_load_seconds` 30.9초, 이번 18.8초). startup/readiness/liveness 구성, 로딩 실패 시 재시작 | 검토안을 설계 3.5.1 에 기록(매니페스트 아님) | 5-4 (매니페스트 검토), 6-2 (EKS 실측) |
| 4 | **Linux 에서 실제 YOLO**: Windows 실제 모델과 Linux mock gunicorn 을 따로만 확인했다. Docker 이미지 분리 단계에서 Linux gunicorn + 실제 YOLO 통합 검증을 **반드시** 수행 | 미수행 | 3-2, 3-3 (설계 9.6 의 완료 기준에 반영) |
| 5 | **테스트 안정성**: 간헐 실패의 근본 원인은 확정되지 않았다(`TEST_COUNTS.md`). 재발하면 테스트 구조와 공유 상태를 다시 분석 | 테스트 구조 보강, 8회 연속 통과 | 계속 |
| – | Linux 에서의 메모리, 사진당 시간, 연결 거절 시간, SIGTERM 뒤 `graceful_timeout`(30초) 동작, Pod 안 torch 기본 스레드 수 | 미측정 | 3-3, 6-2 |
