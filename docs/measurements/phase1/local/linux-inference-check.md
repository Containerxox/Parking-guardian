# Linux gunicorn + 실제 YOLO 검증 (inference 이미지, 2026-10-30)

`python backend/scripts/check_inference_service.py --image parking-guardian-inference`의 출력 전문이다. 컨테이너가 이미지 자신의 시작 명령(`gunicorn -c gunicorn_inference.conf.py wsgi_inference:app`)으로 실행되고, 모델은 `backend/models`를 `/models`로, 샘플 사진은 `backend/uploads`를 `/samples`로 읽기 전용 마운트했다. 메모리·스레드·CPU 시간은 컨테이너 안 `/proc`에서 읽었다. 요약과 해석은 `docker-split.md`.

> 이 실행은 두 번째 실행이다. 첫 실행은 스크립트가 `docker exec` 출력을 Windows 기본 인코딩(cp949)으로 읽다 3절에서 멈춰(스크립트 결함, 수정함) 중단됐다. 첫 실행의 1~2절 값은 준비 20.2초, 로딩 14.6초, RSS 925→1077 MiB(최대 1259), 사진당 중앙값 1.98초, 동시 4장 1.87/3.37/5.02초였다 — 같은 설정에서 실행마다 시간이 이만큼 달랐다.

- python 3.12.16, torch 2.14.1+cpu, flask 3.1.3, gunicorn 26.2.0

## 1. Start-up, memory, threads

- /readyz 200 after **29.7 s** (process or container start included)
- RSS right after loading: **925 MiB**
- cores seen by this host: 4 physical / 8 logical

| photo | status | verdict | stickers | service ms | round trip s |
|---|---|---|---|---|---|
| sample_a.jpg | 200 | permitted | 1 | 2852 | 2.92 |
| sample_b.jpg | 200 | violation | 0 | 2695 | 2.74 |
| sample_c.jpg | 200 | permitted | 1 | 2925 | 3.32 |
| sample_d.jpg | 200 | violation | 0 | 4280 | 5.28 |
| sample_e.jpg | 200 | permitted | 1 | 2956 | 3.24 |
| sample_f.jpg | 200 | no_vehicle | 0 | 1666 | 3.01 |

- first photo 2.92 s, the other five median 3.24 s
- service CPU time / wall time per photo (second round): median **0.99** (about 1.0 means one thread did the work)
- RSS after 12 photos: **1181 MiB** (peak 1363 MiB)
- threads in the server process: 6

- metrics port answers: yes; pg_model_load_seconds 23.09333825111389

## 2. Four photos at once (INFERENCE_MAX_WAITING=2)

| status | Retry-After | error | seconds |
|---|---|---|---|
| 503 | 1 | busy | 0.07 |
| 200 |  |  | 3.46 |
| 200 |  |  | 6.71 |
| 200 |  |  | 9.53 |

- statuses [200, 200, 200, 503]: as designed (3 judged, 1 refused)

## 3. Same photos, judged in-process (the Prototype path)

| photo | over HTTP | in-process | window box equal | sticker boxes equal |
|---|---|---|---|---|
| sample_a.jpg | permitted | permitted | True | True |
| sample_b.jpg | violation | violation | True | True |
| sample_c.jpg | permitted | permitted | True | True |
| sample_d.jpg | violation | violation | True | True |
| sample_e.jpg | permitted | permitted | True | True |
| sample_f.jpg | no_vehicle | no_vehicle | True | True |

- photos whose verdict or boxes differ: **0** of 6
- in-process time per photo (torch default threads = 4): median **1.41 s**

## 4. TORCH_NUM_THREADS takes effect in the service (warm photos, median of 6)

| TORCH_NUM_THREADS | median s per photo | service CPU time / wall time |
|---|---|---|
| 1 | 3.24 | 0.98 |
| 2 | 1.67 | 1.69 |
| 4 | 1.95 | 2.15 |

## 5. A model that cannot load (3 attempts, 1 s base wait)

| seconds after start | /healthz | /readyz |
|---|---|---|
| 2.3 | 200 | 503 |
| 20.4 | 503 | 503 |

- a photo meanwhile: 503 {'error': 'model_unavailable'}
- /healthz body at the end: {'status': 'model_failed'}

## 6. OMP_NUM_THREADS is applied before torch is imported

| how torch is imported | result |
|---|---|
| after wsgi_inference (default) | `{"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "torch_threads": 1}` |
| after wsgi_inference, TORCH_NUM_THREADS=2 | `{"OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "torch_threads": 2}` |
| control: plain `import torch`, no env | `{"torch_threads": 4}` |
