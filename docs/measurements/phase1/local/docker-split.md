# 단계 3: Docker 이미지 분리와 로컬 통합 (2026-10-30)

대상: `backend/Dockerfile`(멀티 타깃), `backend/requirements-inference.txt`, `docker-compose.phase1.yml`. 모두 이 PC의 Docker Desktop(CPU 8개, 메모리 8 GiB)에서 실행한 값이다. EKS 값이 아니다. 근거 자료 두 개는 같은 폴더의 `linux-inference-check.md`(Linux gunicorn + 실제 YOLO)와 `compose-drills.md`(Compose 통합 드릴)이다.

## 구성

| 타깃 | 내용 | 시작 명령 |
|---|---|---|
| `prototype` (**마지막 스테이지 = 기본값**) | 지금까지의 빌드 그대로(API + torch + YOLOv5). `docker compose build backend`가 이것을 만든다 | `flask db upgrade` → gunicorn `wsgi:app` (그대로) |
| `api` | torch / YOLOv5 / OpenCV 없음. `INFERENCE_BACKEND=http` 기본 | 마이그레이션 + gunicorn `wsgi:app` (prototype과 같은 명령) |
| `inference` | 두 모델 + `inference_service`. DB·JWT·CORS·S3·테스트 패키지 없음, 마이그레이션 없음, 필요한 코드만 복사 | gunicorn `-c gunicorn_inference.conf.py wsgi_inference:app` (worker 1개 강제) |

`docker-compose.yml`은 바꾸지 않았다(Prototype 그대로 실행된다). Phase 1은 `docker-compose.phase1.yml`을 겹쳐서 쓴다:
`docker compose -f docker-compose.yml -f docker-compose.phase1.yml up -d --build`. 설계 9.1은 `docker-compose.yml` 수정으로 적었지만, Prototype 실행 방식을 보존하라는 지시에 따라 파일을 나눴다.

## 이미지 크기 (`docker images`)

| 이미지 | 크기 | 비고 |
|---|---|---|
| `parking-guardian-backend` (prototype) | **2.89 GB** | 이번 재빌드도 2.89 GB. 분리 전 기준 이미지(2026-10-25 빌드, ID 62b265efb1c3)도 같은 크기 |
| `parking-guardian-api` | **343 MB** | Prototype의 약 12%. 패키지 39개 (`image-packages-api.txt`) |
| `parking-guardian-inference` | **2.74 GB** | Prototype보다 약 150 MB 작을 뿐이다. 크기의 대부분이 torch와 YOLOv5 의존성(OpenCV, pandas 등)이라서다. 패키지 53개 (`image-packages-inference.txt`) |

### 감소율 (Prototype 이미지 대비, 두 가지 기준)

크기를 재는 방법이 둘이라 둘 다 적는다. `docker images` 값은 유효 숫자 3자리로 반올림되어 있어 ±0.5% 정도의 오차가 있다.

| 이미지 | 압축 전 디스크 사용량 (`docker images`) | 압축 콘텐츠 (`docker image inspect` 의 Size) |
|---|---|---|
| prototype | 2.89 GB | 661,592,038 B (약 661.6 MB) |
| api | 343 MB | 84,326,847 B (약 84.3 MB) |
| inference | 2.74 GB | 624,857,246 B (약 624.9 MB) |
| **api / prototype** | 11.9% → **88.1% 작음** | 12.7% → **87.3% 작음** |
| inference / prototype | 94.8% → 5.2% 작음 | 94.4% → 5.6% 작음 |

### 이미지 크기 감소를 인프라 비용 감소로 읽지 않는다

- 줄어든 것은 **API 이미지 하나의 크기**다. Phase 1은 API Pod와 inference Pod를 따로 띄우므로 Pod 수와 요청 자원의 합은 오히려 늘 수 있다(설계 9.2: 최소 api 2 + inference 2). 노드는 두 이미지를 모두 받는다.
- 인프라 비용은 EC2 노드 시간, NAT, ALB, RDS 등이 지배하고 이미지 크기와는 거의 무관하다. ECR 저장 비용은 이미지가 3개(frontend 제외 시 2개 → inference 추가)로 늘어 오히려 약간 늘 수 있다. 이번에 **비용은 계산도 측정도 하지 않았다.**
- 이미지가 작아져서 기대할 수 있는 것은 API Pod의 **Pull과 스케일 아웃 시작이 빨라질 가능성**이다. 이것도 이 PC의 로컬 빌드 크기에서 나온 추정이고, 실제 Pull 시간은 EKS에서 재야 한다(설계 2.1절 시작 시간 측정).

빌드 시간: 첫 빌드(api + inference, torch 다운로드 포함) 4분 54초. Prototype 재빌드는 AI 레이어를 재사용해 43초.

## 의존성 확인 (이미지 안에서 `import`)

| 패키지 | api | inference |
|---|---|---|
| torch, cv2, numpy, pandas | **없음** | 있음 (torch 2.14.1+cpu, opencv 5.0.0.93) |
| flask_sqlalchemy, sqlalchemy, flask_migrate, flask_cors | 있음 | **없음** |
| bcrypt, jwt (PyJWT), psycopg, boto3 | 있음 | **없음** |
| pytest | 있음(requirements.txt에 포함, 이번에 바꾸지 않음) | **없음** |
| gunicorn | 26.2.0 | 26.2.0 (`==26.2.0`으로 고정) |

## 재현성 (gunicorn 버전)

- inference는 `requirements-inference.txt`에서 `gunicorn==26.2.0`으로 고정했다. 이번 모든 확인이 이 버전이다.
- `requirements.txt`는 `gunicorn>=23` 범위 그대로다(api와 prototype이 쓴다). 지금은 26.2.0이 설치되지만 시점에 따라 달라질 수 있다. 바꾸면 Prototype 빌드 입력이 달라지므로 이번에는 건드리지 않았고, 잠금 파일(lock) 도입은 CI 단계에서 검토할 일로 남긴다.
- 나머지 패키지는 범위 지정이라, 위 두 `image-packages-*.txt`가 이번에 실제로 설치된 버전의 기록이다.

## Prototype 이미지 점검 (분리 뒤에도 동작하는가)

기본 compose로 다시 빌드한 이미지를 별도 DB(`parking_guardian_proto`, 검증 뒤 삭제)에서 실행했다.

- 마이그레이션 3단계가 자동 실행됨(`45440091f146 → fcc09ab94851 → a3c7e51d9b02`).
- 실제 모델: 모델 로딩 13.1초, `/readyz` 503(loading) 9.2초 → 200(ready) 24.9초.
- 판정: sample_a permitted, sample_b violation_pending → (연속 2회) **violation 생성(201)**, sample_c permitted, sample_f no_vehicle. 사진당 0.73~1.46초(torch 기본 스레드). 분리 전과 같은 판정·같은 응답 형식.

## 핵심 측정값 (Linux gunicorn + 실제 YOLO, inference 이미지)

| 항목 | 값 | 비고 |
|---|---|---|
| 모델 준비(`/readyz` 200)까지 | 29.7초 (같은 설정으로 앞서 중단된 실행에서는 20.2초) | 컨테이너 시작 포함. **같은 설정에서 실행마다 10초가량 차이** |
| 모델 로딩(`pg_model_load_seconds`) | 23.1초 (다른 실행 14.6초) | |
| 메모리 RSS | 로딩 직후 **925 MiB**, 사진 12장 뒤 **1181 MiB**, 최대 **1363 MiB** | Linux `/proc` 값(VmRSS, VmHWM). torch 스레드 1개 |
| 서버 프로세스 스레드 | 6 | |
| 실제 CPU 사용 | CPU시간/벽시계 0.99 (스레드 1개) | |
| 사진당 시간(스레드 1) | 중앙값 3.24초 (앞선 실행 1.98초) | **같은 설정에서 실행마다 최대 약 1.6배 차이**. 한 번의 측정값을 비교 근거로 쓰지 않는다 |
| 프로세스 안 판정(torch 기본 스레드 4) | 중앙값 1.41초 | 컨테이너가 보는 CPU 8개의 절반이 기본값이었다 |
| 판정 일치 | 샘플 6장 모두 판정·앞유리 상자·스티커 상자 **동일(차이 0/6)** | HTTP 결과 vs 프로세스 안 결과 |
| 스레드 1 / 2 / 4 | 3.24 / 1.67 / 1.95초 (CPU 비율 0.98 / 1.69 / 2.15) | 스레드 설정은 적용된다. 4가 2보다 빠르지 않았지만 1회 측정이라 해석하지 않는다 |
| `OMP_NUM_THREADS` | `wsgi_inference`를 import한 뒤 torch를 import하면 `torch_threads=1`(기본), `TORCH_NUM_THREADS=2`이면 2. 대조군(그냥 `import torch`)은 4 | 환경변수가 torch import 전에 적용된다 |
| 모델 로딩 실패 | 시도 중 `/healthz` 200 + `/readyz` 503 → 20.4초에 `/healthz` 503(`model_failed`), 사진은 `503 model_unavailable` | Windows 값(34.5초)과 시간만 다르고 동작은 같다 |
| 동시 4장 | 3장 처리 + 1장 503 busy(0.07초, Retry-After 1). 처리된 3장은 3.5 / 6.7 / 9.5초 | 설계대로 |

### 해석상 주의

1. **측정 변동이 크다.** 같은 이미지·같은 설정의 두 실행에서 사진당 시간이 1.98초와 3.24초, 준비 시간이 20.2초와 29.7초였다. 원인(다른 프로세스, CPU 클럭 등)은 확인하지 않았다. E0는 반복 3회 규칙(사전 등록)을 지켜야 하고, 이 문서의 숫자는 크기 정도만 본다.
2. **torch 기본 스레드는 컨테이너가 보는 CPU 수가 아니었다.** 이 컨테이너의 `os.cpu_count()` 기준 CPU는 8개로 보이는데 기본값은 4였다. 코어 수 기준으로 정해지는 것으로 보이나 규칙은 확인하지 않았다. 2 vCPU 노드(m7i-flex.large)에서 Prototype Pod의 기본값은 **측정하지 않았고, 코어 수의 절반이라면 1일 수 있다.** 그렇다면 설계의 서비스 시작값 1이 Prototype의 실제 값과 같을 가능성이 있다. 확인은 E0 시작 때 `torch.get_num_threads()` 기록으로 한다(이미 사전 등록에 있음).
3. **메모리 Request/Limit은 아직 확정하지 않는다.** 설계 9.2의 Request/Limit 값은 여전히 **추정**이다. 이 문서의 Linux 측정(최대 RSS 1363 MiB, 이 PC)은 크기 정도를 알려 줄 뿐이고, 값은 EKS에서 E0/S0를 실측한 뒤 조정한다(사전 등록 "요청값은 E0·S0 실측 뒤 정한 값"). 아래는 근거 정리다.
   - 최대 1363 MiB(VmHWM)는 설계의 inference 메모리 요청 추정치 1.5Gi에 아주 가깝다. 스레드를 늘리거나 큰 사진이 오면 더 늘 수 있고, Prototype은 limit 2Gi였다. limit을 1.5Gi 근처로 두면 OOM 위험이 있다.
   - 같은 이미지·같은 설정의 반복 실행에서 사진당 시간은 1.2초부터 3.2초까지, 준비 시간은 12.8초부터 약 31초까지 나왔다(`compose-drills.md` 두 실행, `linux-inference-check.md`). 메모리(사진 12장 뒤 RSS)도 두 실행에서 1077 MiB와 1181 MiB로 달랐다(로딩 직후는 둘 다 925 MiB).
   - torch CPU 스레드 수(시작값 1)도 같은 원칙이다: EKS의 E0/E1에서 최종 결정한다.
