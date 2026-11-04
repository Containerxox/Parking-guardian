# Phase 1 기준 처리량 C 재측정(B0) 사전 점검과 실행·복원 계획 (2026-11-04 밤, 아직 실행하지 않았다)

**이 문서는 계획이다.** 사진 전송, 부하 테스트, SQS 발행, Kubernetes·HPA·Argo 변경, 코드 수정, 커밋은 하지 않았다. 아래 "관찰"은 읽기 전용 조회로 직접 확인한 값이다.
표기: **[관찰]** 이번 점검에서 직접 읽음, **[기록]** 이전 문서·결과 파일에서 읽음, **[계산]**, **[추정]**, **[미확인]**.
로컬 전용 문서다(Git 에 올리지 않는 규칙). 근거 문서: config 리포 `docs/loadtest-execution-plan-2026-11-04.md`, 앱 리포 `docs/measurements/phase1/README.md`(사전 등록)·`results/2026-10-31-e3n2-SUMMARY.md`·`loadtest/tools/p3.py`·`loadtest/tools/e3_run.py`.

## 0. 한 장 요약
- **B0 = 현재 빌드 `60c2e43`에서 inference 단독 E3(Pod 2개, 동시 4, 클러스터 안 k6 Pod)를 3회 돌려 C 를 다시 재는 것**이다. 사진은 inference Service 로 직접 가며 backend·DB·S3·SQS 를 거치지 않는다. 이전 값 3.107 장/초는 이 실험의 C 로 쓰지 않고 비교 참고로만 둔다.
- **노드 추가는 필요 없다.** k6 Pod(100m)는 inference 가 없는 노드(여유 1090m)에 들어간다.
- **지금 클러스터는 Phase 1 B0 를 시작할 수 있는 상태다**: `p3.py status --expect baseline` 과 `--expect p2-baseline` 전부 PASS, 관찰자 `preflight` 문제 0, RDS available, 비밀번호 회전 다음 일정 11-12(오늘 회전은 끝남).
- **그대로는 못 돌리는 것이 두 가지** 있다. ① `e3_run.py` 가 옛 클러스터의 노드 이름과 옛 스냅샷 경로를 코드에 박아 두었다(6절). ② HPA 를 고정하지 않으면 부하 중 inference 가 3개로 늘어난다(1.2절). 둘 다 별도 승인이 필요하다.

## 1. 재측정 구성 확인
### 1.1 현재 구성 [관찰]
| 항목 | 값 |
|---|---|
| 빌드 | backend·frontend·inference·worker·relay 모두 `60c2e43`, config 리포 HEAD `ad00d85` |
| 노드 | m7i-flex.large × 3 (할당 가능 CPU 약 1930m). 유휴 CPU 4~5 % |
| Phase 1 경로 | backend 2 (요청 200m/256Mi, HPA 2~3) → HTTP → inference 2 (요청 **1000m**/1.5Gi, 메모리 상한 2Gi, CPU 상한 없음, HPA 2~3, `TORCH_NUM_THREADS=1`, `INFERENCE_MAX_WAITING=2`) |
| Phase 2 경로 | relay 1 (50m), worker **0** (요청 1000m/1.5Gi, 메모리 상한 2Gi), KEDA ScaledObject min 0 / max 1 (폴링 15 s, 쿨다운 300 s, fallback replicas 1) |
| Argo CD | 6개(keda, monitoring, monitoring-config, parking-guardian, platform, root) 전부 Synced/Healthy, 자동 동기화(prune+selfHeal) 켜짐 |
| 큐·DB | SQS 본 큐·DLQ 0, RDS `db.t4g.micro` available |
| 배치 | node-19: inference + backend + 모니터링 일부, node-23: inference + relay + frontend + **Prometheus**, node-7: backend + grafana (inference 없음) |
| 노드별 CPU 요청 합 / 여유 | node-19 1740m / 190m, node-23 1655m / 275m, node-7 840m / 1090m |
| 지표 수집 | Prometheus 가동. inference PodMonitor 15 s, kubelet(cAdvisor) 10 s, node-exporter(steal 포함). TSDB 약 8시간치 |

### 1.2 inference 2개 고정과 backend HPA 2/2
- **방법**: `p3.py pause --apply` 가 ① Argo `root` 와 `parking-guardian` 의 `syncPolicy.automated` 를 제거하고 ② HPA `backend`·`inference` 를 min=max=2 로 패치한다(Deployment·이미지·Git 은 건드리지 않는다). 순서: 사전 점검(`baseline` 전부 PASS) → 스냅샷 → root → 자식 → HPA → 60초 관찰 → `fixed` 검사.
- **왜 필수인가 [계산]**: inference 두 Pod 는 부하에서 CPU 1.0 코어에 포화한다(이전 0.98~1.0) = 요청의 100 %. HPA 목표는 70 % 라서 계산상 desired = ceil(2 × 100/70) = 3 이다. 지금 node-7 에 1090m 여유가 있어서 **세 번째 inference 가 실제로 스케줄된다**. 즉 고정하지 않으면 측정 도중 Pod 수가 바뀐다(10-31 S0 에서 backend 가 2→3→2 로 움직여 분류 변경한 전례).
- backend 는 B0 동안 한가하다(약 2m). 그래도 S0·S1 과 같은 고정 상태를 유지하려고 같이 고정한다.
- 고정 상태에서 **parking-guardian 은 OutOfSync 가 정상**(HPA 가 Git 과 다름). `root` 도 자식 Application 의 spec 이 달라져 OutOfSync 가 될 가능성이 있다 [미확인, pause 직후 확인]. 관찰자는 `--allow-outofsync root,parking-guardian` 으로 실행해야 거짓 ABORT 가 안 난다.

### 1.3 Argo CD 자동 동기화와의 충돌 가능성
| 상황 | 영향 | 대응 |
|---|---|---|
| 자동 동기화를 끄지 않고 HPA 만 패치 | selfHeal 이 몇 분 안에 HPA 를 2~3 으로 되돌림 | pause 가 root·child 를 먼저 끈다 |
| root 를 안 끄고 child 만 끔 | root 가 child 의 automated 를 되살림 | **root 먼저** 끄는 순서를 지킨다(p3 가 그렇게 한다) |
| 고정 중 앱 리포 push(`backend/**`·`frontend/**`) → CI → config 리포 `newTag` 커밋 | 고정 중에는 반영 안 됨. **복원 순간 자동 동기화가 켜지며 새 이미지로 롤링**(Pod 교체, UID 변경 → 복원 검사 실패) | 시험 중·복원 확인 전에는 `backend/`·`frontend/` 를 push 하지 않는다. `loadtest/**`·`docs/**` push 는 CI 를 트리거하지 않는다 [관찰: `build-push.yml` paths]. 복원 직전 config HEAD 가 스냅샷 시점(`ad00d85`)과 같은지 확인 |
| 다른 4개 앱(keda, monitoring, monitoring-config, platform) | 자동 동기화 그대로 켜져 있다. 이 앱들은 inference·backend 를 건드리지 않는다 | 그대로 둔다(`fixed` 검사도 "그대로"를 기대) |
| worker Deployment 의 replicas | parking-guardian 앱이 `ignoreDifferences` 로 무시 [관찰] | 영향 없음 |
- `kubectl diff -f argocd/root.yaml` → 차이 없음 [관찰]. 즉 복원 때 `root.yaml` 적용은 지금 상태와 같아서 부작용이 없다.

### 1.4 KEDA Worker 와 Relay 가 Phase 1 측정에 주는 영향
- Worker 0 개라 CPU 를 쓰지 않는다. KEDA operator 가 15초마다 SQS 속성을 읽는다(요청 50m 짜리 Pod, 한가함). Relay 는 DB 를 주기적으로 읽는 50m 짜리 Pod 로 node-23(inference 와 같은 노드)에 있다.
- B0 경로(k6 → inference)는 DB·SQS 와 무관하다. 그래서 **측정값에 구조적 영향은 없을 것으로 본다 [추정]**. 다만 이전 C(10-31)에는 KEDA·Relay·CloudWatch 에이전트가 없었던 구성이라 "환경 차이"로 기록한다.
- **위험 하나**: KEDA fallback 은 SQS 를 연속 3번 못 읽으면 Worker 를 1개 띄운다(replicas 1). SQS·권한이 정상이면 일어나지 않는다. 그래도 Worker 가 뜨면 node-7 의 1000m 자리를 쓰므로(k6 100m 이 있으면 Pending) 중단 규칙에 넣는다(4.2절).
- 선택 사항: ScaledObject 에 `paused-replicas: "0"` 어노테이션을 붙여 Worker 를 0 으로 못 박을 수 있다. **권고하지 않는다**(Kubernetes 변경이 늘고 복원 때 제거를 잊을 위험). 필요하면 별도 승인.

### 1.5 AI CPU requests 2000m 비교 조건 [관찰]
- Phase 1: inference 2 × 1000m = 2000m. Phase 2(비교 구성의 Worker 2): 2 × 1000m = 2000m. 두 쪽 모두 메모리 요청 1.5Gi·상한 2Gi, CPU 상한 없음, `TORCH_NUM_THREADS=1`. 요청이 같다 → 이 점은 공정하다.
- 요청은 예약일 뿐 사용 상한이 아니다. 실제 사용량은 B0 의 cAdvisor 누적 카운터로 따로 재고, 같은 방법을 Phase 2 에도 쓴다.

### 1.6 추가 노드 없이 가능한가
- **B0: 가능.** inference 2(node-19, node-23) + k6 100m(node-7, 여유 1090m). 세 번째 inference 가 자리를 차지하지 못하게 HPA 를 고정한다.
- S0·S1(PC 의 k6 → ALB): 가능. 클러스터 안에 k6 가 없다.
- 공존 상태에서 Worker 2개 이상은 불가능(1000m 자리 1개). 그것은 Phase 2 비교 구성(inference 제거) 단계의 일이다.

## 2. 기준 처리량 C 재측정 계획 (B0)
### 2.1 이전 측정(10-31)의 조건 [기록]
| 항목 | 값 |
|---|---|
| 실행기 | `e3_run.py` + `k6/inference.js`, k6 `grafana/k6:2.3.0` 클러스터 안 임시 Pod(100m/128Mi), 노드 A(inference 없는 노드) |
| 부하 | closed-loop **VUS=4**, `--no-connection-reuse`(요청마다 새 연결 → kube-proxy 가 Pod 를 확률로 선택), **3분**, 워밍업 5장(`setup()`, 지표 제외) |
| 사진 | `sample_a`·`sample_b` 를 번갈아(허가/위반 판정 사진 반반) inference 로 직접. DB·위반·알림에 닿지 않음 |
| 구성 | 이미지 `c14a7f0`, inference 2 Pod(HPA 2 고정), `INFERENCE_MAX_WAITING=2`, `TORCH_NUM_THREADS=1`, 요청 1000m |
| 결과 | 3.126 / 3.101 / 3.107 장/초 → **C = 3.107(중앙값), 범위 3.101~3.126**. busy 503 15.6~19.8 %, 서버 추론 평균 약 620 ms, 두 Pod CPU 0.98~1.0, 분배 53~54 / 46~47 % |
| 의미 | VUS=4 와 대기 상한 2 에서 잰 **성공 처리량**이다. 무손실 최대 처리량이 아니다(busy 503 포함) |
| 1회차 | 요약 파싱 버그로 자동 판정이 "무효"였으나 k6 는 정상 종료 → 로그·Prometheus 로 재분석한 값 |

### 2.2 `60c2e43`에서 달라진 조건
**같은 것** [관찰]: 요청 1000m·메모리, `TORCH_NUM_THREADS=1`, `INFERENCE_MAX_WAITING=2`, 노드 종류·개수, HPA 2~3(고정은 pause 로), 사진 2장, k6 스크립트(`inference.js` 변경 없음).
**달라진 것**:
| 구분 | 내용 | 영향 판단 |
|---|---|---|
| 코드 | `c14a7f0..60c2e43` 사이에 inference 서비스 파일(`inference_service/`, `wsgi_inference.py`, `gunicorn_inference.conf.py`, `requirements-inference.txt`, `Dockerfile`)은 **변경 없음** [관찰: git diff]. 이미지에 들어가는 `backend/app` 쪽만 바뀜: `adapters/inference.py` +61줄(Worker 용 `preload`), `metrics.py` +5줄(첫 추론 시간 게이지) | 요청 처리 경로는 같다 [추정, 호출 경로 미추적]. 그래도 이미지 레이어는 다르므로 재측정한다 |
| 클러스터 | 11-04 에 새로 만든 클러스터·새 노드 인스턴스 | 인스턴스 편차 가능 [미확인] |
| 애드온 | CloudWatch Container Insights(에이전트·Fluent Bit·operator, 노드마다 요청 합 약 150m), KEDA, Relay, ESO | 노드 CPU 요청 합이 달라짐. 실제 사용 CPU 는 작음 |
| 배치 | **Prometheus 가 inference 한 Pod 와 같은 노드(node-23)** 에 있고 CPU 요청이 없다. Relay 도 같은 노드 | 이전 배치와 같은지 모름 [미확인]. node-23 의 CPU 경쟁 가능성 → 노드별 CPU·steal 기록 |
| 관찰자 | 이번에는 `p2_watch.py` 가 동시에 돈다(읽기 전용) | 3절 |
| 지표 | 클러스터가 갓 뜬 상태라 inference 카운터(`pg_inference_requests_total` 등)가 아직 없다 [관찰]. 첫 요청 후 생긴다 | 워밍업 5장이 만든다. 첫 회차 쿼리 결과 확인 필요 |

### 2.3 절차 (이전 절차를 그대로 따른다)
1. 준비: 시험 전 상태 검사·기준선 저장(4.1절), 관찰자 시작, 유휴 기준선 5분.
2. `p3.py pause --apply`(승인). 스냅샷 경로 기록.
3. `e3_run`(수정판, 6절) 3회. 회차마다: 사전 점검(P3 fixed + inference 엔드포인트 ready 2 + 남은 k6 Pod 없음) → k6 Pod 생성 → 10초마다 P3 fixed 재검사 → 종료 후 25초 뒤 Prometheus 질의(종료 시각 기준 마지막 180초) → `result.json`·`conditions.md`·SHA256SUMS 저장. 회차 사이 60초 휴식.
4. 복원(4.4절).
- **워밍업 / 측정 구간**: `setup()` 이 `/readyz` 200 을 최대 4분 기다린 뒤 워밍업 5장을 `phase:warmup` 으로 보내고 지표에서 뺀다. 측정은 그 뒤 3분(`phase:measure`).
- **반복**: 3회(중앙값과 범위). 이전 값은 사용하지 않는다.

### 2.4 처리량 계산·판정
- **처리량** = 측정 구간의 성공(HTTP 200) 건수 / 초. C = 유효 회차의 중앙값(범위 함께).
- 함께 기록: busy 503 비율, `inference_failed`, `inference_unavailable`, 클라이언트 p50/p95/p99, 서버 추론 시간, Pod 별 처리 비율, Pod 별 CPU 평균·최대(cAdvisor), 메모리 최대, 실행/대기 큐 최대, 락 대기 p95, 단계 평균(decode·window·sticker), **노드 사용률·steal**, backend CPU, HPA desired 최대, k6 Pod CPU.
- **포화 판정**: 두 inference Pod 의 평균 CPU 가 모두 ≥ 0.8 코어이고 처리 비율이 40~60 % 안일 때 "양쪽 포화·분배 균형".
- **무효(그 회차를 폐기하고 같은 조건으로 다시)**: `inference_failed` > 0, `inference_unavailable` > 0, 측정 중 Pod 재시작·Pending·새 Pod, HPA 변화(`ScalingLimited` 포함 기록), P3 고정 검사 실패(자동 중단), k6 요약을 읽지 못함, Worker Pod 출현, Argo `Degraded`. **추가 제안(실행 전 고정)**: k6 Pod CPU 최대 ≥ 0.7 코어, 두 Pod 중 하나가 CPU < 0.8 인데 처리량이 이전 대비 크게 낮을 때는 "포화 아님"으로 별도 표기. 무효 회차도 폴더에 남긴다.
- 무효 3연속이면 중단하고 원인을 보고한다.

### 2.5 테스트 데이터 증가량 [계산]
| 항목 | 증가 |
|---|---|
| DB 행(작업·결과·세션·위반·알림·감사) | **0**. inference 만 호출한다 |
| S3 객체·SQS 메시지 | **0** |
| 장비 키·계정 | 변경 없음(장비 7·8·9 도 사용하지 않음) |
| inference 요청 수 | 회차당 약 680~720건(이전 성공 572~577 + busy 106~141) × 3 + 워밍업 15 ≈ **2,100건**. 사진 평균 353 KB → 클러스터 안 트래픽 약 **0.74 GB** |
| 임시 리소스 | `loadtest` 네임스페이스, ConfigMap(스크립트·사진 약 0.95 MB), k6 Pod 3개 → 끝나면 모두 삭제 |
| 로컬 결과 | 회차 폴더 3개(각 수십 KB), Prometheus 응답, 관찰자 타임라인 |

## 3. 관찰자 영향 통제
### 3.1 DB 수집이 B0 를 교란하는가 — 판단
**B0 에서는 교란 가능성이 작다 [추정].** 이유: ① B0 는 DB 를 쓰지 않아 DB 수집이 보는 값이 없고, 영향은 backend Pod CPU(+16m)뿐이다. ② 그 Pod(`backend-…l87lr`)가 node-19 에서 inference 한 개와 같은 노드에 있다. node-19 는 2 vCPU 중 inference 가 약 1.0, 나머지가 0.1~0.2 정도라 여유가 약 0.8 코어 있어, 16m(0.8 %)는 경쟁을 만들지 않는다. ③ 이전 C 의 회차 간 편차(3.101~3.126, 약 0.8 %)보다 영향이 작다고 보지만 **측정한 적은 없다 [미확인]**. 그래서 사용자 지시대로 DB 수집 30초를 기본으로 두고, 영향 한계를 가늠할 **선택 통제 회차**를 둔다.
- **통제 회차(선택, 승인 필요)**: 3회 완료 후 관찰자를 `--db-period 0` 으로 다시 띄워 1회 더(약 5분). 결과가 3회 범위 안이면 "DB 수집 영향 검출 안 됨"으로, 밖이면 "영향 가능성"으로만 기록한다. **어느 쪽이든 수치에서 임의로 빼거나 보정하지 않는다.**
- 대안(필요할 때만): `--db-period 60`(영향 절반), DB 수집 끔, 상시 실행 세션(코드 변경 필요, 별도 승인). 이번 계획에서는 쓰지 않는다.
- **S0·S1 에서는 다시 판단한다**: 그때는 backend 가 실제 부하를 받으므로 +16m 이 의미가 커질 수 있다.

### 3.2 기록하고 통제할 것
| 항목 | 방법 |
|---|---|
| Backend CPU 오버헤드 | 관찰자 cAdvisor 로 Pod 별 평균 기록. DB 수집 켠 구간에서 약 18m, 끈 구간 약 1.8m 이 이미 측정됨(오버헤드 약 16m). 이번에도 같은 방식으로 기록만 한다 |
| 유휴 CPU 기준선 | pause 전 5분: 노드·Pod CPU(관찰자), Prometheus 5분 평균. inference ≈ 0, backend ≈ 2m, 노드 4~5 % [관찰] |
| 관찰자 PC CPU | B0 는 PC 가 k6 를 돌리지 않아 영향 없음. 관찰자 자체는 PC 의 약 0.5~1.9 % [측정]. S0·S1 에서는 k6 CPU < 70 % 규칙과 함께 PC CPU 를 기록 |
| k6 목표 요청률 | B0 는 closed-loop 라 해당 없음. S1 은 `dropped_iterations` 0 확인 |
| 예상치 못한 HPA 확장 | 고정으로 막고(max=2), 시도 자체를 `ScalingLimited` 조건·Pod 수·desired 로 기록. **inference/backend Pod 가 3개가 되거나 desired > 2 이면 즉시 중단** |
| 임의 보정 금지 | 오버헤드를 결과에서 빼지 않는다. 결과 표에 "관찰자 DB 수집 ON(16m)"을 조건으로 적는다 |

## 4. 사전 보호와 복원
### 4.1 시험 전 보호 [읽기 전용 + 로컬 파일]
1. `p3.py status --expect baseline`, `--expect p2-baseline`(오늘 PASS) 재확인.
2. `p2_protect.py snapshot` → 기준선 저장(DB 읽기 전용 SELECT). 시험 후 `verify` 로 "기존 행 불변·새 행 0"을 확인한다.
3. 관찰자 `preflight` 문제 0, SQS 본 큐·DLQ 0, RDS available, 회전 일정 확인(다음 11-12) [오늘 확인함].
4. config HEAD(`ad00d85`) 기록. 시험 중 `backend/`·`frontend/` push 금지.
5. `p3.py snapshot` 은 pause 가 자동으로 만든다. 저장물: Application·HPA·Deployment JSON, Pod 목록(UID·재시작), 메타(config HEAD, Argo 리비전), SHA256SUMS.

### 4.2 중단 규칙 (e3_run 이 10초마다 `p3 status --expect fixed` 를 검사하고, 관찰자가 15초마다 보강)
| 신호 | 조치 |
|---|---|
| P3 고정 검사 실패(Pod 재시작·Pending·새 Pod·HPA 변화·Application 상태) | k6 Pod 삭제 → 그 회차 무효 기록 → 다음 회차로 가지 않음 |
| HPA desired/current > 2 (inference·backend) | 위와 같음 + 원인 보고 |
| Worker Pod ≥ 1 (KEDA fallback 포함) | 위와 같음. 큐가 비었는지 확인 |
| 관찰자 ABORT(시간 기반이 아닌 사실 기반) | 위와 같음 |
| inference 메모리 상한 접근·OOMKilled | 위와 같음 |
| 사용자 중단 요청 | `kubectl delete namespace loadtest` → 복원 |

### 4.3 중단 절차
1) `kubectl delete namespace loadtest --wait=false`(k6 Pod 제거) → 2) 관찰자는 켜 둔 채 상태 기록 → 3) 4.4 복원 → 4) 보고하고 멈춤(재시도는 승인 후).

### 4.4 복원 (Phase 2 baseline 으로)
1. `p3.py restore --snap <스냅샷> --apply --observe 300`: ① 앞서 확인한 대로 차이가 없는 `argocd/root.yaml` 적용 ② 15초마다 검사(최대 6분): root·child 자동 동기화, HPA 2/3, 6개 Application Synced/Healthy, Application·HPA spec 이 스냅샷과 같음, Pod UID·재시작 불변 ③ 복구 뒤 300초 동안 1분마다 새 Pod·재시작 없는지.
2. `p3.py status --expect p2-baseline`(공존 구성: inference 2, ScaledObject 0/1, 일시정지 없음, 자동 동기화 켜짐) 전부 PASS.
3. 확인: backend 2/2·inference 2/2·frontend 1/1·relay 1/1·worker 0/0, KEDA Ready=True, SQS·DLQ 0, `loadtest` 네임스페이스 없음, `p2_protect verify` 기존 데이터 불변.
4. **복원 직전 config HEAD 가 `ad00d85` 인지 확인**(아니면 복원 순간 롤링이 일어나므로 보고하고 판단을 받는다).

### 4.5 복원이 실패하면 (수동 복구, 승인 후 실행)
```
kubectl -n argocd patch app root --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl -n argocd patch app parking-guardian --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl -n parking-guardian patch hpa backend   --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":3}}'
kubectl -n parking-guardian patch hpa inference --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":3}}'
kubectl delete namespace loadtest --ignore-not-found
```
(`p3.py restore --emergency` 가 같은 일을 한다.) 이후 `status --expect p2-baseline`. 세 번째 inference 가 생겼다면 HPA 복구 후 scaleDown 안정화 300초 뒤 자동으로 2개가 된다. 그래도 어긋나면 스냅샷 JSON 과 `kubectl diff` 로 차이를 보고하고 멈춘다. Application spec 이 스냅샷과 다르면 스냅샷의 `app-*.json` 을 기준으로 사용자 승인 후 되돌린다.

## 5. 실행 전 결정 사항과 승인 구분
### 5.1 지금까지 읽기 전용 관찰만으로 해결된 것 (이미 확인)
Phase 1/2 구성·요청값·배치, p3 `baseline`·`p2-baseline` PASS, `root.yaml` diff 없음, 관찰자 preflight 문제 0, 회전 일정, CI 트리거 조건, 이전 C 조건, inference 서비스 파일 변경 없음, Prometheus 지표 가용성(첫 요청 전이라 일부 카운터 없음).

### 5.2 별도 승인이 필요한 쓰기 작업
| # | 작업 | 종류 | 되돌림 |
|---|---|---|---|
| W1 | `e3_run.py` 로컬 수정(6절)·로컬 시험 | 로컬 파일 | 되돌릴 수 있음(`.bak`) |
| W2 | 관찰자 실행 | 읽기 전용(AWS·Kubernetes) | — |
| W3 | `p3.py pause --apply` | **Kubernetes 쓰기**: Application 2개 patch, HPA 2개 patch | `restore` |
| W4 | `e3_run` 실행 | **Kubernetes 쓰기**: namespace·ConfigMap·Pod ×3 생성·삭제. Docker Hub 에서 k6 이미지 pull | 네임스페이스 삭제 |
| W5 | `p3.py restore --apply` | **Kubernetes 쓰기**: `root.yaml` 적용 | — |
| W6 | (선택) DB 수집 OFF 통제 1회 | W4 와 같음 | |
| W7 | (선택) KEDA 일시정지 어노테이션 | Kubernetes 쓰기 | 어노테이션 제거 |

### 5.3 규모·시간·비용 [계산]
- **사진 전송**: 앱 접수 경로로는 **0장**. inference 직접 호출 약 2,100건(0.74 GB, 클러스터 안). SQS·S3·DB 0.
- **시간**: 준비·유휴 기준선 15분 → pause 3분 → 측정 3회 × 약 4.5분 + 휴식 2분 ≈ 16분 → (선택 통제 5분) → 복원·관찰 10~12분. **합계 약 45~55분(선택 포함 약 60분)**.
- **비용**: 새 리소스 없음. 클러스터 유지비(약 $0.60/h)만 해당 → 약 $0.5~0.6. NAT(k6 이미지 pull 약 60 MB)·AZ 간 전송(약 0.74 GB)은 합쳐 1센트 안팎 [추정]. 노드 추가 비용 0.
- **위험**: k6 이미지를 Docker Hub 에서 받는다(익명 pull 제한 가능성 [미확인]). 막히면 회차가 시작되지 않을 뿐 데이터 손상은 없다.

### 5.4 사용자 결정이 필요한 항목
1. 위 5.2의 W1~W5 승인 순서(권고: W1 → 로컬 시험 보고 → 별도 승인으로 W2~W5).
2. 선택 통제 회차(W6) 수행 여부 — 권고: 수행.
3. 무효 기준 추가 제안(2.4절 "추가 제안")을 실행 전에 고정할지.
4. 결과 폴더 이름(`b0-<번호>` 권고; 이전 `e3n2-*` 와 구분).
5. 시험 후 Phase 2 비교 구성 전환은 이 계획의 범위 밖(별도 승인).

## 6. `e3_run.py`에서 실행 전에 고쳐야 할 것 (**W1 완료, 2026-11-04**: 아래 표는 수정 전 문제이며 모두 반영했다. 원본은 `e3_run.py.bak-before-b0`, 시험은 `tools/test_e3_run.py` 56개)
| 위치 | 문제 [관찰] | 수정 안 |
|---|---|---|
| `SNAP = …s1-p3-snapshot-20261031-222457` | 옛 스냅샷. `p3_fixed()` 가 이 경로로 비교하면 Pod UID·Application 수가 달라 **항상 FAIL → 사전 점검에서 중단** | 새 스냅샷 경로를 인자로 받는다 |
| `NODE_A = ip-10-0-20-149…` | 존재하지 않는 노드(현재 노드는 …7-63, …19-217, …23-139). 이 이름으로 nodeSelector 를 걸면 Pod 가 Pending | inference Pod 가 없는 노드를 실행 시점에 골라 기록한다(현재 node-7) |
| 폴더 이름 `e3n2-{n}` | 이전 결과와 혼동 | `b0-{n}` |
| 새 중단 신호 | Worker 출현·`ScalingLimited`·k6 Pod CPU 를 안 본다 | 4.2·2.4절대로 추가(읽기만) |
| 질의 anchor | 첫 회차에 일부 카운터가 새로 생김 | 워밍업 뒤 시리즈 존재 확인을 넣거나, 실패 시 `ERR` 로 남기고 무효 처리 |
수정은 로컬 도구 변경이고 클러스터를 건드리지 않지만, 이번 승인 범위(계획 작성)에 포함되지 않으므로 하지 않았다. 수정 후에는 가짜 kubectl 로 로컬 시험하고 conftest 안전장치를 유지한다.

## 7. 점검 시점 요약 (2026-11-04 약 22:20 KST) [관찰]
| 항목 | 결과 |
|---|---|
| Argo 6개 | Synced/Healthy, 자동 동기화 켜짐 |
| backend / inference / frontend / relay / worker | 2/2 / 2/2 / 1/1 / 1/1 / 0/0 |
| HPA | backend 2~3 (2/2), inference 2~3 (2/2) |
| 노드 CPU | 4~5 % |
| SQS 본 큐·DLQ | 0 |
| RDS | available, 비밀번호 회전 마지막 11-04 13:24, 다음 11-12 |
| 관찰자 preflight | 문제 0, 가장 느린 소스 2.5초 |
| `kubectl diff -f root.yaml` | 차이 없음 |


---
# 8. 실행 전 최종 점검 (2026-11-04 밤, W1 반영 후) — 읽기 전용 점검과 계획 수정만 했다

표기: [관찰] 이번에 직접 읽음, [예측] 코드·구조로 추론(실제 변경 뒤에야 확인), [미확인]. 아래 4절(복원)·5절(승인)을 이 절이 보강·대체하는 곳은 "→ 8.x" 로 표시한다.

## 8.1 스냅샷 두 종류의 역할 (질문 1)
| 구분 | 만드는 시점·방법 | 역할 | 쓰는 곳 | 쓰면 안 되는 곳 |
|---|---|---|---|---|
| **S0 baseline 스냅샷** | `p3.py pause --apply` 가 `baseline` 사전 점검 통과 직후, 아무것도 바꾸기 전에 자동 생성(`docs/measurements/phase1/eks/s1-p3-snapshot-<시각>/`). 사전에 `p3.py snapshot --out …-S0-manual` 로 한 벌 더 떠 두는 것을 권고(읽기 전용, 비교용) | **복원 기준**(원본 Application·HPA 설정)이자 고정 상태 검증 기준(Pod UID·재시작·Deployment 템플릿이 "고정 전과 같은가") | `e3_run.py --snap S0`, `p3.py status --expect fixed --snap S0`, `p3.py restore --snap S0` | — |
| **S1 고정 후 증거 스냅샷**(선택) | 고정 후 60초 검사 통과 뒤 `p3.py snapshot --out …-S1-FIXED` | 고정이 적용된 모양의 **증거 기록**(자동 동기화 꺼짐, HPA 2/2) | 결과 보고서에 첨부 | **`--snap`·`restore` 에 절대 쓰지 않는다** (고정 상태를 "원본"으로 착각하게 된다) |
- 현재 도구는 S0 와 S1 을 **구별하지 못한다** [관찰: `verify_snapshot` 은 해시·시각만 본다]. S1 을 `--snap` 으로 넘기면 사전 점검은 통과할 수 있고, `restore` 는 "Application spec 이 백업과 같음" 검사에서 실패하는 쪽으로 멈추지만 원인이 헷갈린다. → 제안 W1b-2(8.9절): `e3_run` 이 스냅샷 안의 `app-root.json`(automated 있음)·`hpa-*.json`(min≠max)을 확인해 baseline 형이 아니면 거부.
- **S0 가 담고 있는 것** [관찰: 지금 클러스터에서 임시로 떠서 확인, 파일 17개, 비밀 값·`password`·`secretKeyRef` 없음]: Application 6개 전체 JSON(root·parking-guardian 의 `syncPolicy.automated {prune, selfHeal}`, parking-guardian 의 `retry`·`syncOptions` 포함), HPA backend·inference·keda-hpa-worker 전체 JSON(원본 min 2 / max 3, metrics·behavior), Deployment backend·inference·frontend·relay·worker, ScaledObject worker(min 0 / max 1), `pods.json`(backend·inference·frontend 의 UID·재시작·노드), `meta.json`(촬영 시각, **config 리포 HEAD `ad00d85`**, Argo 리비전), `SHA256SUMS`. → **pause 가 바꾸는 값(Application 2개의 automated, HPA 2개의 min/max)의 원본이 모두 들어 있다.**
- **담기지 않는 것**: `argocd/root.yaml` 본문(복원 때 config 리포 작업 폴더의 파일을 쓴다), PDB·Service·ConfigMap, relay·worker Pod 의 UID, Prometheus 데이터. → 복원 전에 **config HEAD 가 `meta.json` 의 `config_repo_head` 와 같은지** 비교한다(8.6절 R3).
- 보존: 두 폴더(`eks/`, `results/`)는 `.gitignore` 로 제외되어 있다 [관찰: `git check-ignore`]. 스냅샷의 Deployment JSON 에 ECR 주소(계정 ID)가 있으므로 **절대 커밋·공유하지 않는다**. 읽기 전용(444) 복사본을 한 벌 더 두는 것을 권고.

## 8.2 `p3.py pause --apply` 가 바꾸는 Kubernetes 리소스 (질문 2, 4)
쓰기는 **정확히 4건**의 `kubectl patch` 뿐이다 [관찰: 코드]. 로컬 파일 쓰기: 스냅샷 폴더 1개.
| # | 리소스 | 필드 | 변경 전 → 후 | 방식 |
|---|---|---|---|---|
| 1 | Application `root` (argocd) | `spec.syncPolicy.automated` | `{prune: true, selfHeal: true}` → 제거 | merge patch(`null`) |
| 2 | Application `parking-guardian` (argocd) | `spec.syncPolicy.automated` | 같음 → 제거(`retry`·`syncOptions` 는 그대로) | 같음 |
| 3 | HPA `backend` (parking-guardian) | `spec.minReplicas/maxReplicas` | 2/3 → 2/2 | merge patch |
| 4 | HPA `inference` | 같음 | 2/3 → 2/2 | 같음 |
- **바뀌지 않는 것**: Deployment·Pod·Service·Ingress·이미지·ConfigMap·Secret·ScaledObject·KEDA·PDB, Git, Terraform, 다른 Application 4개(keda, monitoring, monitoring-config, platform 은 자동 동기화 그대로), HPA 의 metrics·behavior.
- 이후 `e3_run` 이 만드는 것(별도 W4): 네임스페이스 `loadtest`, ConfigMap `k6-scripts`, Pod `b0-1..3`(→ 삭제). `restore`(W5) 가 하는 것: `kubectl apply -f argocd/root.yaml`(지금 `kubectl diff` 결과 **차이 없음** [관찰]) 한 건.
- 권한 [관찰: `kubectl auth can-i`]: Application patch·apply, HPA patch, namespace/pod/configmap 생성, namespace 삭제 모두 yes.

## 8.3 고정 순서 (질문 3) — 코드의 실제 순서
`baseline` 사전 점검(전부 PASS 아니면 변경 없이 종료) → 스냅샷(S0) → **① root automated 제거 → ② parking-guardian automated 제거 → ③ HPA backend 2/2 → ④ HPA inference 2/2** → 60초 대기 → `status --expect fixed --snap S0` → 복구 명령 출력.
- 순서의 이유: root 를 먼저 꺼야 root 의 selfHeal 이 child 의 변경을 되돌리지 못한다. child 를 꺼야 child 의 selfHeal 이 HPA 패치를 되돌리지 못한다. 각 패치 사이는 약 1초.
- 남는 경쟁 가능성 [예측]: ①과 ② 사이에 Argo 컨트롤러가 root 의 옛 캐시(automated 있음)로 child 를 되돌리면 ②의 효과가 사라져 `fixed` 검사가 "parking-guardian 자동 동기화 꺼짐" 에서 FAIL 한다. 데이터 손상은 없고 FAIL 로 드러난다 → 8.10절 시나리오 F2.
- backend 와 inference 는 각각 독립 패치라 하나만 적용된 중간 상태가 가능하다(F4). inference 쪽이 늦게 적용되는 구간에도 HPA 는 부하가 없어서 확장하지 않는다.

## 8.4 Argo CD 중단 범위와 OutOfSync 허용 범위 (질문 4)
- **자동 동기화를 끄는 곳**: `root`, `parking-guardian` 두 개뿐. 나머지 4개는 켜진 채 유지된다(`fixed` 검사도 그렇게 기대한다).
- **예상되는 OutOfSync**:
  - `parking-guardian`: HPA 2개가 Git(2/3)과 달라서 OutOfSync, Health 는 Healthy [관찰: p3 의 `fixed` 정의가 이것을 정상으로 본다].
  - `root`: root 는 `argocd/apps/*.yaml`(자식 Application 5개)을 배포한다 [관찰]. 자식 `parking-guardian` 의 spec 이 Git 과 달라지므로 OutOfSync 가 될 가능성이 크다 [예측, **pause 직후에 확인**]. Argo 에 Application 종류의 spec 차이를 무시하는 기본 설정이 있는지는 [미확인].
  - 나머지 4개(keda, monitoring, monitoring-config, platform)는 Synced 여야 한다.
- **관찰자 설정**: `p2_watch.py run … --allow-outofsync root,parking-guardian`. 관찰자는 OutOfSync 를 기본으로 ABORT 로 올리므로 허용하지 않으면 pause 직후 거짓 ABORT 가 난다 [관찰: 코드]. Healthy 가 아닌 Application 은 허용 여부와 상관없이 ABORT 다.
- 허용 범위를 벗어난 OutOfSync(위 4개) 또는 Degraded 가 나타나면 중단 규칙에 따른다.

## 8.5 Worker 0/0 및 KEDA 와 충돌 여부 (질문 5)
- pause 는 ScaledObject·KEDA·worker Deployment·`keda-hpa-worker` 를 **건드리지 않는다**. `keda` 앱은 자동 동기화 그대로, parking-guardian 앱이 멈춰도 ScaledObject 는 현재 상태 그대로 남는다. worker Deployment 의 replicas 는 원래 Argo 가 무시한다(ignoreDifferences) [관찰].
- 고정 중에도 큐가 비어 있고 KEDA 가 SQS 를 15초마다 읽으므로 Worker 는 0 으로 유지된다 [예측]. 예외는 KEDA fallback(SQS 읽기 3회 연속 실패 시 replicas 1) — `e3_run` 가드가 Worker Pod·replicas > 0 을 즉시 중단 사유로 본다(W1).
- `keda-hpa-worker` 는 `p3.py` 의 고정 대상(`HPAS = backend, inference`)이 아니다. 따라서 `fixed` 검사와 충돌하지 않는다.
- 복원 후 `p3.py status --expect p2-baseline`(ScaledObject 0/1, 일시정지 없음, 자동 동기화 켜짐)로 최종 확인한다.

## 8.6 명령 순서 (질문 6) — 아직 실행하지 않았다. 모든 쓰기 단계는 별도 승인
**환경**: `$env:AWS_PROFILE="parking-guardian"`, 작업 폴더 `C:\parking_guardian\capston`, `$PY = backend\venv\Scripts\python.exe`.
| 단계 | 명령 | 종류 | 합격 기준 |
|---|---|---|---|
| R0 | `& $PY loadtest\tools\p3.py status --expect baseline` 그리고 `--expect p2-baseline` | 읽기 | 전부 PASS (오늘 22시 PASS 확인) |
| R1 | `git -C ..\parking-guardian-config rev-parse --short HEAD` 가 `ad00d85`, 앱 리포 `backend/`·`frontend/` 에 미반영 변경 없음 | 읽기 | 같음 |
| R2 | `& $PY loadtest\tools\p2_protect.py snapshot …` (DB 기준선) 와 SQS·DLQ 0, `p2_watch.py preflight` 문제 0, RDS 회전 일정(다음 11-12) | 읽기 | 문제 0 |
| R3 | `& $PY loadtest\tools\p3.py snapshot --out docs\measurements\phase1\eks\b0-S0-manual` | 읽기+로컬 파일 | 17개 파일, `SHA256SUMS` 확인 |
| R4 | 관찰자 시작(별도 터미널): `p2_watch.py run --out <결과>\timeline.jsonl --queue-url … --dlq-url … --secret-id … --interval 15 --duration 4500 --db-period 30 --cadvisor-period 30 --allow-outofsync root,parking-guardian` — 5분 유휴 기준선 | 읽기 | 경고·ABORT 0 |
| **W3** | `& $PY loadtest\tools\p3.py pause` (계획만 출력) → 확인 후 `… pause --apply` | **쓰기 4건** | 출력된 S0 경로 기록, 60초 뒤 `fixed` 전부 PASS |
| V1 | `& $PY loadtest\tools\p3.py status --expect fixed --snap <S0>` | 읽기 | 전부 PASS |
| V2 | `kubectl get app -n argocd` — root 가 OutOfSync 인지 기록, 나머지 4개 Synced | 읽기 | 8.4절 |
| V3 | **게이트**: `& $PY loadtest\tools\e3_run.py 1 3 --snap <S0> --dry-run` | 읽기 | 8.7절 모든 항목 PASS |
| S1(선택) | `p3.py snapshot --out docs\measurements\phase1\eks\b0-S1-FIXED` | 읽기+로컬 | 보고서 첨부용(복원에 쓰지 않음) |
| **W4** | `& $PY loadtest\tools\e3_run.py 1 3 --snap <S0>` | **쓰기** | 8.8절(첫 실행 문제)·8.9절(유효 기준) |
| R5(복원 전) | `git -C ..\parking-guardian-config rev-parse --short HEAD` == `<S0>\meta.json` 의 `config_repo_head`; 앱 리포 `backend/`·`frontend/` push 없음; `kubectl get ns loadtest` 없음; SQS 0 | 읽기 | 같음 |
| **W5** | `& $PY loadtest\tools\p3.py restore --snap <S0>` (계획 출력) → `… restore --snap <S0> --apply --observe 300` | **쓰기** | 검사 전부 PASS, 300초 안정 |
| V4 | `p3.py status --expect restored --snap <S0>`, `--expect p2-baseline`, `p2_protect.py verify --baseline …`, SQS 0, worker 0/0 | 읽기 | 전부 PASS |
- `restore` 의 동작: `kubectl apply -f argocd/root.yaml`(차이 없음) → 15초마다 `restored` 검사(최대 6분): root·child 자동 동기화 켜짐, HPA 2/3, Application 6개 Synced/Healthy, **Application·HPA spec 이 S0 와 같음**, Pod UID·재시작 불변, Deployment 템플릿 불변 → 통과 후 300초 동안 1분마다 재검사.
- S0 가 두 개(수동 `b0-S0-manual` 과 pause 가 만든 것)라면, 둘 다 같은 baseline 상태에서 떴으므로 `app-*.json`·`hpa-*.json` 의 spec 이 같아야 한다. 다르면(예: 그 사이 변경) 즉시 중단하고 원인을 본다.

## 8.7 HPA 고정 후 `e3_run.py --dry-run` 에서 모두 통과할 수 있는가 (질문 7)
**예상: 통과한다 [예측, pause 뒤에야 확인].** 지금(고정 전) 결과와 고정 후 예상:
| 항목 | 지금 | 고정 후 예상 | 근거 |
|---|---|---|---|
| 스냅샷 검증 | OK | OK | 방금 만든 S0, 6시간 이내 |
| k6 노드 선택 | node-7 | node-7 | 고정은 Pod 배치를 바꾸지 않는다 |
| inference/backend 각 2개 Ready, Worker 0, Pending 없음 | PASS | PASS | |
| HPA backend/inference min=max=2 | **FAIL(2/3)** | PASS | pause 가 2/2 로 만든다 |
| HPA current/desired <= 2 | PASS | PASS | |
| 재시작·OOM 없음 | PASS | PASS | |
| `p3 status --expect fixed --snap S0` | 4항목 FAIL | PASS | root·child 자동 동기화 꺼짐, HPA 2/2, parking-guardian Healthy |
- 통과하지 못할 수 있는 지점: ① 8.3절의 경쟁(child 자동 동기화가 되돌려진 경우) ② pause 직후 HPA `desiredReplicas` 가 아직 2 로 수렴하지 않은 짧은 구간(60초 대기로 해소) ③ parking-guardian 의 Health 가 Progressing 으로 보이는 경우 [미확인].
- 이 dry-run 은 **W4 의 게이트**다: 하나라도 FAIL 이면 k6 Pod 를 만들지 않고 8.10절의 복원 절차로 간다.

## 8.8 첫 실행에서 발생할 수 있는 문제 (질문 8)
| # | 문제 | 상태 | 영향·대응 |
|---|---|---|---|
| P1 | k6 이미지 `grafana/k6:2.3.0` 을 Docker Hub 에서 pull(익명 한도) | [미확인] | 막히면 Pod 가 `ImagePullBackOff` — 10분 제한 후 무효 기록·정리. 데이터 영향 없음 |
| P2 | ConfigMap 크기 | [관찰] 약 948 KB / 한도 1 MiB (여유 100 KB, 9.6 %) | 사진을 바꾸면 한도 초과 가능. 지금 구성은 통과 |
| P3 | nodeSelector 값 | [관찰] 노드의 `kubernetes.io/hostname` 라벨 = 노드 이름 | 일치 |
| P4 | Prometheus `pg_inference_requests_total`·stage 히스토그램이 아직 없다 | [관찰] 첫 요청 전이라 시리즈 없음 | 워밍업 5장이 만든다. 1회차에서 Pod 별 성공 수·분배가 비면 재질의(8.9 W1b-1) |
| P5 | **CFS throttling 지표가 없다** | [관찰] inference·backend 에도 `container_cpu_cfs_*` 시리즈가 없음(CPU limit 이 없는 컨테이너). k6 Pod 도 limit 이 없다 | 현재 코드는 이를 "확인 불가" 경고로 처리해 매 회차 경고가 나온다. 의미상 limit 이 없으면 throttling 은 일어나지 않는다 → W1b-3 에서 "해당 없음(0)"으로 정리. 병목 증거 (a)는 이 클러스터에서 작동하지 않고, (b) 노드 사용률·(c) inference 포화에 의존 |
| P6 | node-exporter 인스턴스 라벨 | [관찰] `10.0.7.63:9100` 형식, 코드는 `<InternalIP>:` 접두어 비교 | 일치 |
| P7 | k6 Pod 의 cAdvisor 시리즈는 Pod 가 생긴 뒤 10초 스크레이프로 생긴다 | [관찰] 지금 `loadtest` 시리즈 0 | k6 CPU 쿼리는 첫 회차에서 처음 검증된다. Pod 삭제(`--wait=false`) 후 25초 뒤 질의 — 종료 중인 Pod 의 시리즈는 조회 가능할 것으로 예상 [예측] |
| P8 | 질의 시점 anchor(`offset`) | [예측] 종료 시각 기준 180초 창이 k6 의 측정 구간(3분)과 약간 어긋날 수 있다(워밍업·종료 지연 수 초) | 창 경계 효과로 평균 CPU 가 낮게 나올 수 있음 → 평균은 "≈" 로 해석, 포화 판정은 0.8 코어 기준과 최대값을 함께 본다 |
| P9 | 포트 19093 이 이미 사용 중이거나 port-forward 실패 | [예측] | Prometheus 값이 `ERR` — 질의는 읽기라 나중에 같은 시각 기준으로 다시 할 수 있어야 한다(W1b-1) |
| P10 | `kubectl top`·metrics-server 와 k6 CPU 해상도 | [예측] | k6 CPU 를 Prometheus 에서만 읽으면 10초 해상도 |
| P11 | 관찰자와 동시 실행 시 거짓 ABORT | [관찰: 코드] | `--allow-outofsync root,parking-guardian` 필수 |
| P12 | 회차 사이 60초 휴식 중 HPA 상태 | [예측] | 고정이라 영향 없음 |

## 8.9 Prometheus CPU 지표가 없거나 불완전할 때 공식 결과를 유효로 인정할 기준 (질문 9) — 제안(실행 전 고정 대상)
처리량의 원천은 **k6 요약**(Prometheus 와 독립)이다. CPU 는 "포화했는가"와 "부하 생성이 병목인가"를 판정하는 보조 증거다.
| 등급 | 조건 | 쓰임 |
|---|---|---|
| **A. 공식 유효** | ① k6 요약 파싱 성공, `inference_failed`=0, `inference_unavailable`=0 ② 가드 감시 표본이 15초 이내 간격으로 연속(공백 > 30초 없음), 위반·재시작·OOM 없음 ③ **두 inference Pod 의 구간 평균 CPU 를 독립적으로 알 수 있고 둘 다 ≥ 0.8 코어**(출처: Prometheus cAdvisor, 없으면 관찰자 타임라인의 cAdvisor 누적 카운터, 없으면 노드 `/metrics/resource` 표본) ④ k6 병목 판정이 `invalid` 가 아님(k6 CPU 가 없으면 ③의 포화로 대체: 서버가 포화했으면 생성기가 병목이 아니다) | C 산정(유효 회차 중앙값) |
| **B. 참고(처리량만)** | ①②는 만족하지만 ③을 알 수 없음(세 출처 모두 없음) | 처리량 기록은 하되 C 에는 쓰지 않는다. 같은 시각 기준 재질의로 ③을 채울 수 있으면 A 로 승격(Prometheus 보존 15일 [관찰]) |
| **C. 무효** | ①② 중 하나라도 위반, 부하 생성 병목 확인, 구성 이탈 | 폴더에 남기고 재실행 |
- **Pod 별 처리 비율(40~60 %)** 은 유효 조건이 아니라 기록 항목으로 둔다(이전 사전 등록과 같음). 알 수 없으면 "분배 미확인"으로 적는다.
- 없는 값을 **추정으로 채우지 않는다**: 비어 있으면 "미확인" 으로 쓴다.
- C 는 등급 A 가 3회 모일 때까지 한다(5회 시도 한도). 한도 안에 3회가 안 되면 "C 미확정" 으로 보고하고 멈춘다.
- 이 기준은 사용자 승인 후 사전 등록 변경 기록에 남기며, 결과를 본 뒤에는 바꾸지 않는다.

### W1b — 위 기준을 도구에 반영하기 위한 제안(코드 수정 필요, 이번에는 하지 않았다, 별도 승인)
1. `result.json`·`prometheus.json` 에 `finished_ts` 와 사용한 PromQL 전문을 저장해 **나중에 같은 시각 기준으로 재질의**할 수 있게 한다.
2. `--snap` 이 baseline 형인지 검사한다(`app-root.json` 에 automated 가 있고 `hpa-backend/inference.json` 의 min≠max).
3. throttling: CPU limit 이 없는 컨테이너는 "해당 없음(0)" 으로 기록하고 경고를 내지 않는다.
4. 가드 루프(10초)마다 k6 노드의 `/api/v1/nodes/<노드>/proxy/metrics/resource`(읽기, 약 25 KB)를 읽어 k6 Pod·inference Pod 의 CPU 누적 카운터를 저장한다 — Prometheus 가 없어도 구간 평균·10초 해상도 최대를 얻는 독립 출처.

## 8.10 적용 도중 실패했을 때 안전한 복원 (질문 10)
**원칙**: 먼저 어디까지 바뀌었는지 읽기 전용으로 확인하고(R-state), 바뀐 만큼만 되돌린다. 복원은 항상 같은 S0 를 기준으로 한다. 상태 판별 명령:
```
& $PY loadtest\tools\p3.py status --expect baseline          # 아무것도 안 바뀐 상태?
& $PY loadtest\tools\p3.py status --expect fixed --snap <S0>  # 완전히 고정된 상태?
kubectl get app -n argocd -o custom-columns=NAME:.metadata.name,AUTO:.spec.syncPolicy.automated.selfHeal,SYNC:.status.sync.status
kubectl get hpa -n parking-guardian
```
| 시나리오 | 상태 | 복원 |
|---|---|---|
| **F1** 사전 점검 실패(`baseline` FAIL) | 변경 없음 | 아무것도 하지 않는다. 원인 보고 |
| **F2** root 패치 실패 또는 ①②경쟁으로 child 자동 동기화 유지 | root 만 꺼졌거나 child 가 되살아남 | `kubectl apply -f ..\parking-guardian-config\argocd\root.yaml` → 자동 복구. 확인: `status --expect baseline` |
| **F3** root·child 는 꺼졌고 HPA 패치 전 | 자동 동기화만 꺼짐 | `restore --snap <S0> --apply` (HPA 는 이미 원본이라 문제 없음) |
| **F4** HPA 하나만 2/2 | 부분 고정 | `restore --snap <S0> --apply`. 급하면 `kubectl -n parking-guardian patch hpa <이름> --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":3}}'` 후 restore |
| **F5** 4건 모두 적용됐으나 60초 검사 FAIL | 고정 완료·검증 실패 | 실패 항목 보고. 측정은 하지 않고 `restore` |
| **F6** 측정 중 중단(가드·P3 FAIL, HPA 초과, Worker 출현, OOM) | k6 Pod 삭제됨(자동), 고정 유지 | `kubectl delete ns loadtest --ignore-not-found` → 원인 기록 → `restore` |
| **F7** `e3_run` 비정상 종료(kill, 터미널 닫힘) | `loadtest` NS/Pod 남을 수 있음 | `kubectl delete ns loadtest` → `restore` |
| **F8** `restore` 6분 안에 통과 못 함 | 원인 불명 | `restore --snap <S0> --apply --emergency`(직접 패치). 그래도 실패하면 아래 수동 복구 |
| **F9** 복원 중 config HEAD 가 S0 와 다름(새 커밋) | 복원 순간 Argo 가 새 리비전으로 롤링할 수 있음 | R5 에서 막는다. 이미 일어났으면 `restore … --replaced inference,backend` 로 교체 Pod 를 허용하고 보고 |
**수동 복구(최후 수단, S0 의 JSON 이 기준)**:
```
kubectl -n argocd patch app root --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl -n argocd patch app parking-guardian --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl -n parking-guardian patch hpa backend   --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":3}}'
kubectl -n parking-guardian patch hpa inference --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":3}}'
kubectl delete namespace loadtest --ignore-not-found
```
복구 후: `status --expect baseline`, `status --expect p2-baseline`, `kubectl get app -n argocd`(6개 Synced/Healthy), Pod 목록이 S0 `pods.json` 과 같은지(UID·재시작), `p2_protect.py verify`, SQS 0. 세 번째 inference 가 생겼다면 HPA 복구 뒤 scaleDown 안정화 300초가 지나면 2개가 된다.
**하지 않을 것**: Pod 직접 삭제, Deployment 수정, `kubectl scale`, 스냅샷 JSON 의 `kubectl apply`(상태 필드가 섞여 있다).

## 8.11 B0 실행 전 최종 체크리스트
- [ ] 사용자 승인: W3(pause), W4(e3_run), W5(restore), (선택) W6, (선택) W1b 코드 수정. 8.9 의 유효 기준 고정.
- [ ] 8.6 R0~R5 전부 합격. 특히 config HEAD `ad00d85`, `backend/`·`frontend/` push 없음.
- [ ] 관찰자 실행 중(`--allow-outofsync root,parking-guardian`), 유휴 기준선 5분 기록.
- [ ] S0 경로 기록·읽기 전용 복사. S1(고정 후)은 `--snap`·`restore` 에 쓰지 않음.
- [ ] pause 직후 V1~V3(특히 `e3_run --dry-run` 전부 PASS)를 통과해야 k6 Pod 를 만든다.
- [ ] 중단 규칙·복원 명령을 눈앞에 둔다(8.10).
- [ ] 시험 중 Git push, 장비 키 회전, 사진 전송, Terraform 금지.
- [ ] 끝나면 V4 전부 PASS 와 결과 폴더(`…-b0-*`) SHA256 확인.


---
# 9. W1b 반영과 측정 유효 기준 사전 등록 v1 (2026-11-04 밤, 측정 전)

W1b-1~3 을 `e3_run.py` 에 구현했고(W1b-4 는 구현하지 않고 검토만), 8.9절 기준을 코드와 비교했다. 아래 "고정 대상" 항목은 사용자 결정을 받은 뒤 `docs/measurements/phase1/README.md` 변경 기록과 함께 확정한다(결과를 보기 전).

## 9.1 구현 요약
| 항목 | 내용 | 코드 |
|---|---|---|
| W1b-1 재질의 | 첫 결과에 평가 시각·식·원본 응답·상태(ok/empty/error)·누락 사유·Pod 식별(UID·노드)을 저장. `--requery <폴더>` 는 **새 파일만** 만든다(`prometheus-requery-N.json`, `grade-upgrade-N.json`, `requery-N.sha256`). B→A 는 재질의에서 두 Pod 의 CPU 를 실제로 얻었을 때만 | `build_queries`, `prom_status`, `requery_folder`, `evaluate_upgrade` |
| W1b-2 S0 검증 | `--snap` 은 변경 전 baseline(S0)만. SHA256SUMS·6시간·필수 파일·복원 메타·HPA 원본 2/3·자동 동기화 켜짐·config HEAD 일치. 고정 후(S1)·이미 바뀐 상태는 거부 | `verify_baseline_snapshot` |
| W1b-3 throttling | 4상태 구분: CPU limit 없음+지표 없음 / limit 있음+누락 / 조회 오류 / 관측. limit 이 없어도 "병목 없음"으로 판정하지 않는다(caveat 를 결과에 항상 기록) | `classify_throttle`, `assess_k6` |
| 등급 | A 공식 유효 / B 참고 / C 무효, A 3회·최대 5회, 안전 중단은 즉시 정지 | `compute_grade`, `next_action`, `summarize_attempts` |
| (추가) 감시 주기 | 토큰 캐시로 가드 한 바퀴 약 56초 → 약 15초 [측정] | `enable_token_cache` |
| (추가) ConfigMap | 로컬에서 먼저 크기 계산, 초과 위험·생성 실패 시 k6 Pod 없이 정리 | `configmap_budget` |

## 9.2 사전 등록 v1 — A/B/C 정의 (코드와 일치)
**공통 입력**: 처리량의 원천은 k6 요약(성공 200 건수/초). CPU 는 포화 확인의 보조 증거다. **CPU 평균 0.8 코어는 포화 확인의 사전 기준일 뿐 최대 처리량의 단독 증거가 아니다.**
- **C 무효**(그 회차를 폴더에 남기고 재실행)
  - 안전 중단(`safety_stop`): 가드·HPA(current/desired>2)·Worker 출현·고정 상태 위반으로 중단, 종료 시점 고정 상태 위반, Pod 재시작, OOMKilled, 사전 점검 실패.
  - 측정 무효(`measurement_invalid`): k6 요약을 읽지 못함, `inference_failed`>0, `inference_unavailable`>0, 부하 생성 병목 확인(k6 최대 CPU ≥0.7 이면서 [throttling ≥5 % **관측** 또는 k6 노드 사용률 ≥90 % 또는 inference Pod 중 하나의 평균 CPU <0.8]), 가드 감시 표본 <5개, 감시 공백 >30초, k6 VUS 최대 <4.
- **B 참고**(처리량 기록은 하되 C 산정에 쓰지 않음): 위가 모두 통과했으나
  - `insufficient_evidence`: 두 inference Pod 의 평균 CPU 를 모두 알 수 없다(지표 누락·불완전). 재질의로만 승격 가능.
  - `not_saturated`: 두 Pod 의 CPU 를 알지만 어느 하나가 0.8 코어 미만.
- **A 공식 유효**: 위 전부 통과 + 두 Pod 평균 CPU 가 모두 ≥0.8 코어.
- **시도 제한**: A 3회가 모이면 종료, 시도 최대 5회. 안전 중단·사전 점검 실패는 즉시 종료. C(측정 무효) 3연속이면 종료. C(처리 용량)는 A 등급 3회의 처리량 **중앙값**(범위 함께). 3회가 안 되면 "C 미확정".
- **재질의 승격 규칙**: 원본 `result.json`·`prometheus.json` 은 바꾸지 않는다. 재질의 파일과 승격 평가(`grade-upgrade-N.json`)가 증거이고, 재질의에도 두 Pod 의 CPU 가 없으면 B 로 남는다. 없는 값을 추정하지 않는다.

## 9.3 8.9절 기준 ↔ 코드 비교, 차이와 결정 사항
| 항목 | 8.9절 기준 | W1 코드(이전) | W1b 코드(지금) | 차이·결정 |
|---|---|---|---|---|
| k6 목표 부하·달성률 | 명시 없음 | 확인 안 함 | closed-loop 의 목표는 VUS=4. 로그의 `vus … max=` 를 읽어 4 미만이면 C. 읽지 못하면 감점하지 않고 "미확인" | **결정 D3**: VUS 를 못 읽을 때 B 로 낮출지(지금은 감점 안 함) |
| 요청 실패율 | `inference_failed`=0, `inference_unavailable`=0 | 동일 | 동일. **busy 503 비율은 등급에 영향 없음**(이전 C 측정은 15.6~19.8 %, 대기열 상한의 정상 거절) | **결정 D4**: busy 503 비율 상한을 둘지(권고: 두지 않고 기록만) |
| 모델 추론 성공 | 위와 같음 + k6 요약 파싱 | 동일 | 동일 | 일치 |
| inference Pod 별 CPU | 두 Pod 모두 알고 ≥0.8 | 알면 `both_saturated` 기록만, 미달이어도 유효 | 두 Pod 를 모두 알아야 A. 모르면 B(`insufficient_evidence`), 알지만 <0.8 이면 B(`not_saturated`) | **차이 D2**: 8.9절은 "알지만 <0.8"을 정의하지 않았다. 코드는 B 로 처리. 결정 필요 |
| k6 CPU·노드 사용률 | 병목 `invalid` 가 아님. k6 CPU 가 없으면 포화로 대체 | 0.7 경고, 증거 1개 이상이면 무효 | 같음. throttling 은 "관측"일 때만 증거. k6 CPU 가 없으면 `warn`(무효 아님) → 등급은 ③ 포화로 결정 | 일치 |
| HPA·Worker 가드 | (W1) 즉시 중단 | 10초 루프라고 적었으나 실제 약 56초 | 토큰 캐시로 약 15초. HPA current/desired>2·Worker 출현·가드 위반 즉시 중단. 가드 조회가 3회 연속 실패하면 감시 불능으로 중단 | **차이 D1**(아래) |
| 재시작·OOM | C | 동일 | 동일(k6·inference·backend·relay·worker) | 일치 |
| 지표 누락 시 | B | Prometheus 못 읽으면 무효/경고 | CPU 누락은 B, 그 밖의 누락은 `prometheus_missing` 로 기록(등급 영향 없음) | 일치 |
| A 3회 / 최대 5회 | 있음 | 첫 무효에서 중단(재시도 없음) | `--target-a 3 --max-attempts 5`. 안전 중단 즉시, C 3연속 중단 | 일치(C 3연속은 계획 2.4절 규칙) |
| 가드 표본·공백 | "15초 이내 간격(공백 >30초 없음)" | 기록만 | 표본 ≥5, 공백 ≤30초를 C 조건으로 구현 | **결정 D6**: 숫자(5개·30초) 확정 |
| 승격 증거 | 재질의 또는 관찰자 타임라인 | 없음 | 재질의만(관찰자 타임라인 경로는 W1b-4 에서 결정) | **결정 D7** |

**D1 — 감시 주기 [측정]**: 가드 한 바퀴는 대기 + `read_guard` + `p3 status` 인데, `kubectl` 이 호출마다 `aws eks get-token`(약 3초)을 실행해 **p3 status 약 38초, read_guard 약 8.2초 → 한 바퀴 약 56초**였다. 3분 측정에서 표본이 4개이고 HPA 초과 감지가 최대 1분 늦다(사전 등록 "즉시 중단"과 불일치, 공백 30초 기준도 항상 위반). 토큰 캐시(관찰자의 `KubeAuth`를 `KUBECONFIG` 로 프로세스 트리에 적용)로 **p3 status 약 8초, read_guard 약 1.4초 → 한 바퀴 약 15초** [측정]. 권고: 채택. 대안: 캐시를 쓰지 않고 공백 기준을 90초로 완화(권고하지 않음). 토큰은 임시 폴더 파일에만 있고 종료 시 삭제·환경변수 복원(시험으로 확인).

**고정 대상(결정 후 사전 등록)**: D1 채택 여부, D2 `not_saturated` 를 B 로 둘지, D3, D4, D6, D7.

## 9.4 W1b-4 검토 — inference Pod 별 CPU 를 믿을 수 있게 확보할 수 있는가 (구현 안 함)
| 출처 | 상태 [관찰] | 해상도 | 평가 |
|---|---|---|---|
| Prometheus kubelet cAdvisor (`container_cpu_usage_seconds_total{container="inference"}`) | 두 Pod 시리즈 있음, 스크레이프 10초, 보존 15일 | 누적 카운터 → 구간 평균 정확, 최대는 10초 단위 | **1차 출처.** 단 port-forward·Prometheus 가동에 의존 |
| kubelet `/metrics/resource` (`kubectl get --raw`, 관찰자가 30초마다) | Prometheus 와 같은 시각의 누적값이 **일치**(두 Pod 모두 차이 0.000 CPU초, 시각차 5~9초) | 30초 | **독립 2차 출처.** 관찰자 타임라인(`cadvisor` 필드, `summarize_cadvisor`)이 이미 inference 를 담는다 |
| 노드 `/metrics/resource` 를 가드 루프에서 직접 읽기(W1b-4) | 가능(약 25 KB, 0.3 CPU초/회) | 루프 주기(약 15초) | Prometheus 없이도 구간 평균·15초 단위 최대 확보. **미구현** |
| `kubectl top` (metrics-server) | 사용 가능 | 15~60초 평균 | 짧은 버스트를 놓친다. 증거로 부적합 |
| node-exporter | 노드 사용률·steal | 15초 | 노드 경쟁 확인용(포화 증거 아님) |
- **결론**: 1차(Prometheus)와 2차(관찰자 타임라인)는 값이 일치하므로 신뢰할 수 있다. 지금 구현만으로는 **관찰자 타임라인을 등급 판정에 자동 반영하지 않는다**(재질의는 Prometheus 만). Prometheus 가 실패하는 경우의 안전망은 관찰자를 `--cadvisor-period 30` 으로 같이 돌리고, 사용자가 그 타임라인을 증거로 B→A 승격을 판단하는 수동 경로다.
- **추가 구현이 필요하다면 예상 범위(W1b-4)**: `e3_run.py` 에 노드 `/metrics/resource` 읽기·파싱(`p2_sampler.parse_cadvisor` 재사용)과 가드 루프 연동 약 60~80줄, 결과에 `cpu_independent` 필드 약 20줄, `evaluate_upgrade`/`compute_grade` 가 이 출처를 받도록 약 20줄, 시험 약 10개. 읽기 전용 호출만 늘어난다(kubectl get --raw). 승인 후 약 반나절.

## 9.5 ConfigMap 용량 [관찰, 로컬 계산(`--dry-run=client`, 서버 호출 없음)]
| 항목 | 값 |
|---|---|
| 키 | `lib.js`(1,878 B), `inference.js`(4,304 B), `sample_a.jpg`(369,857 B), `sample_b.jpg`(336,086 B) — 중복 없음 |
| 서버 검증이 세는 합(키+디코드한 값) | **713,071 B = 한도 1 MiB(1,048,576 B)의 68.0 %** |
| JSON 본문에서 값 길이의 합(이진 값이 base64 로 4/3 증가한 최악 가정) | 947,484 B = 90.4 %. kubectl 이 보낼 JSON 전체 949,078 B |
| 판정 | 안전한 쪽(최악 가정)으로도 한도 안. 90 % 초과라 경고 표시 |
- **이전 보고의 "여유 9.6 %" 는 base64 최악 가정이었다.** 서버가 세는 값은 raw 라서 실제 여유는 약 32 %(335 KB)다 [근거: Kubernetes 검증은 `binaryData` 를 디코드한 길이로 센다는 코드 지식 — 최종 확인은 서버 응답뿐].
- **이미지(사진) 변경 시**: 사진 하나(약 350 KB)를 더하면 raw 1.06 MB 로 한도를 넘는다. `configmap_budget` 이 시작 전에 막는다(시험: 800 KB 파일 추가 시 중단).
- **첫 실행 실패 시 안전 중단**: ① 시작 전 로컬 계산에서 최악 가정이 한도를 넘으면 쓰기 없이 종료 ② 생성이 서버에서 거부되면 `loadtest` 네임스페이스를 삭제하고 k6 Pod 를 만들지 않은 채 종료(종료 코드 3).
- 이번에 **ConfigMap 을 만들지 않았다** (`loadtest` 네임스페이스도 없음).

## 9.6 갱신된 실행 명령 (W3 승인 후)
`& $PY loadtest\tools\e3_run.py 1 5 --snap <S0>` — 시도 최대 5회, A 3회에서 종료, 안전 중단 즉시 종료. 재질의: `e3_run.py --requery docs\measurements\phase1\results\<날짜>-b0-<n>`. `--no-token-cache` 는 감시 주기가 느려지므로 쓰지 않는다.


---
# 10. 사전 등록 v1 확정과 W3 실행 준비 (2026-11-04 밤) — 9.3절의 결정 항목은 아래로 확정됐다

**상태**: 측정 전. 아래 기준을 `docs/measurements/phase1/README.md` 변경 기록에 확정으로 올렸고, `e3_run.py` 판정 로직과 일치함을 시험(`test_e3_w1b.py`)으로 확인했다. 결과를 본 뒤에는 바꾸지 않는다(바꾸면 그렇다고 적는다).

## 10.1 사용자 결정 (2026-11-04)
| 번호 | 결정 | 코드 반영 |
|---|---|---|
| D1 | `e3_run.py` 의 KubeAuth 토큰 캐시 채택 | 기본 켜짐(`--no-token-cache` 로만 끔). 가드 한 바퀴 약 56초 → 약 15초 [측정] |
| D2 | inference CPU 가 0.8코어 미만이면 B(`not_saturated`) | 일치(변경 없음) |
| D3 | k6 VUS 가 4 미만으로 **확인되면 C**, **관측값이 없으면 B(판정 불가)** | **변경함**: 이전에는 관측값이 없으면 감점하지 않았다. 지금은 B(`load_unverified`) |
| D4 | busy 503 비율에 자동 무효 상한 없음. 503 건수·비율은 반드시 기록하고 성공·실패 처리량을 구분 | 일치 + **집계 보강**(10.3절) |
| D6 | 가드 표본 최소 5개, 표본 간격 최대 30초 | 일치(변경 없음) |
| D7 | 자동 B→A 승격은 저장된 Prometheus 재질의 증거가 있을 때만 | 일치(변경 없음). `load_unverified` 는 재질의로 올릴 수 없다 |
| W1b-4 | 추가 CPU 수집 구현 보류 | 구현 안 함 |

## 10.2 사전 등록 v1 최종 기준 (코드 `compute_grade`/`next_action`/`summarize_attempts` 와 일치)
**처리량(C) 의 정의**: A 등급 회차의 **k6 성공(HTTP 200) 건수 / 전체 실행 시간(setup 포함)** 의 중앙값(범위 함께). busy 503·실패·시도율은 처리량이 아니다.
**C(무효)** — 해당 회차를 폴더에 남기고 재실행
- 안전 중단(`safety_stop`, 즉시 정지): 가드 위반, HPA current/desired > 2, Worker 출현, 종료 시점 고정 상태 위반, Pod 재시작, OOMKilled, 감시 불능(가드 조회 3연속 실패), 사전 점검 실패.
- 측정 무효(`measurement_invalid`): k6 요약 파싱 실패, `inference_failed` > 0, `inference_unavailable` > 0, 부하 생성 병목 확인(k6 최대 CPU ≥ 0.7 이면서 [throttling ≥ 5 % **관측** / k6 노드 사용률 ≥ 90 % / inference Pod 중 하나의 평균 CPU < 0.8]), 가드 표본 < 5개, 가드 감시 공백 > 30초, k6 VUS 최대 < 4.
**B(참고)** — 처리량은 기록하되 C 산정에 쓰지 않는다. C 사유가 없고, 아래 중 하나 이상(우선순위 순으로 범주 하나를 기록하고 사유는 모두 적는다)
1. `load_unverified`: k6 VUS 관측값이 없다(판정 불가). 재질의로 올릴 수 없다.
2. `insufficient_evidence`: 두 inference Pod 의 평균 CPU 를 모두 알 수 없다. **재질의 증거가 저장될 때만** A 로 승격한다.
3. `not_saturated`: CPU 를 알지만 어느 Pod 가 0.8 코어 미만.
**A(공식 유효)**: 위 전부 통과 + 두 Pod 평균 CPU 가 모두 ≥ 0.8 코어 + VUS 최대 ≥ 4.
**시도 규칙**: A 3회가 모이면 종료, 시도 최대 5회, 안전 중단·사전 점검 실패는 즉시 종료, C(측정 무효) 3연속이면 종료. A 가 3회 안 되면 "C 미확정".
**해석 한계**: CPU 평균 0.8 코어는 포화 확인의 사전 기준일 뿐 **최대 처리량의 단독 증거가 아니다**. CPU limit 이 없어 throttling 지표가 없는 것은 병목이 없다는 증거가 아니다(노드 CPU 경쟁은 노드 사용률로만 본다).

## 10.3 busy 503 과 성공 처리량 집계 (D4) — 과대평가 방지
- 결과 `throughput` 에 구분해서 기록한다: 성공 건수·성공/초, busy 503 건수·건/초·**비율**, 실패, 모델 불가, 시도 합계·시도/초. 등급은 busy 503 비율에 영향받지 않는다(이전 측정은 15.6~19.8 %).
- **C 산정값 = `success_per_s`(k6 성공 건수 / 전체 실행 시간).** setup(워밍업) 구간이 분모에 들어가므로 측정 구간 평균(`success_per_s_over_measure_window` = 성공/180초)보다 작거나 같다 = 보수적이며 이전 C(3.107)와 같은 방식이다. 둘 다 기록한다.
- 서버(Prometheus) 집계의 성공 수와 교차 확인한다: 10 % 넘게 다르면 경고(등급 영향 없음). 둘 다 기록하고 C 에는 k6 성공 처리량을 쓴다.
- 워밍업 요청은 `phase:warmup` 으로 태그되어 `inference_done` 에 들어가지 않는다 [코드 확인: `inference.js`].

## 10.4 B0 와 S1 의 목적·지표 정의 구분
| 구분 | B0 (이번) | S1 (이후) |
|---|---|---|
| 목적 | **용량 C 재산정**: inference 2 Pod 가 포화했을 때의 성공 처리량 | **혼합 부하 응답**: 일반 API + 사진 부하를 0.2C…1.5C 도착률로 걸 때의 지연·성공률·거절 |
| 경로 | 클러스터 안 k6 Pod → inference Service 직접 (backend·DB·S3 없음) | PC k6 → ALB → backend → inference (DB·S3 포함) |
| 부하 방식 | **닫힌 루프**: 동시 VUS=4 가 응답 즉시 다음 요청 → 처리량이 서버 능력에 의해 정해진다 | **열린 루프**: 도착률 고정 → 서버가 못 따라가면 거절·지연으로 나타난다 |
| 핵심 지표 | 성공 처리량(건/초, C), busy 503 비율, Pod 별 CPU(≥0.8 포화 확인), 서버 추론 시간 | 단계별 일반 API p95/오류율, 사진 성공률(장비가 200/201 을 받은 비율), 접수→판정 지연, 503 거절 |
| "성공"의 뜻 | inference 가 200 으로 판정을 돌려줌 | 장비가 200/201 을 받음(Phase 1 은 판정까지 끝난 응답) |
| busy 503 | 정상 거절(대기열 상한). 처리량에 넣지 않고 비율만 기록 | 성공률을 떨어뜨리는 실패. 한도(통과 기준)에 직접 영향 |
| 무효 기준 | 이 문서 10.2절 | Phase 1 사전 등록 3절(`dropped_iterations`>0, k6 CPU ≥70 %, …) |
| 산출 | C (S1 도착률의 기준값) | 단계별 곡선 |
B0 의 C 는 S1 의 도착률 단계(0.2C…1.5C)를 정하는 **입력값**이다. 따라서 B0 의 A 등급 3회가 확정된 뒤에만 S1 을 설계·실행한다.

## 10.5 W3 실행 준비 — 최종 확인 (아직 실행하지 않았다)
**(1) S0 백업과 복원 절차**
- S0 = 변경 전 baseline 스냅샷. `p3.py pause --apply` 가 `baseline` 사전 점검 직후 자동 생성(`docs\measurements\phase1\eks\s1-p3-snapshot-<시각>\`, 파일 17개, SHA256SUMS). 사전에 수동으로 한 벌 더(`b0-S0-manual`) 떠 둔다(읽기 전용).
- S0 에 pause 가 바꾸는 값의 원본이 모두 있다: Application root·parking-guardian 의 `syncPolicy`(automated·retry·syncOptions), HPA backend·inference 전체 spec(2/3·metrics·behavior), Deployment, Pod UID, config HEAD `ad00d85`.
- `e3_run.py --snap` 은 S0 만 받는다(S1 거부). 복원: `p3.py restore --snap <S0> --apply --observe 300`.
**(2) `p3.py pause` 변경 대상 4개** [pause 계획 출력으로 재확인]: ① Application `root` ② Application `parking-guardian` 의 `syncPolicy.automated` 제거(merge patch null), ③ HPA `backend` ④ HPA `inference` 를 min=2·max=2. 그 밖의 쓰기 없음.
**(3) HPA 고정 방법**: 위 ③④의 `kubectl patch hpa … --type merge -p '{"spec":{"minReplicas":2,"maxReplicas":2}}'`. 순서는 ① root → ② child → ③ backend → ④ inference → 60초 대기 → `status --expect fixed --snap`. 복원은 Git(Argo)이 2/3 으로 되돌린다.
**(4) Argo CD 범위**: 자동 동기화는 root·parking-guardian 두 개만 해제. 나머지 4개(keda, monitoring, monitoring-config, platform)는 유지. OutOfSync 허용 범위: parking-guardian(HPA 차이, 확실), root(자식 spec 차이, 가능성 — pause 직후 확인). 그 밖의 OutOfSync·Degraded 는 중단 사유.
**(5) `e3_run.py --dry-run` 통과 조건**: 스냅샷(S0) OK(S1·변조·6시간 초과·config HEAD 불일치는 FAIL) / ConfigMap 예산 OK / k6 노드 선택됨(현재 node-7) / inference 2·backend 2 Ready, Worker 0, Pending 없음 / **HPA backend·inference min=max=2** / HPA current·desired ≤ 2 / 재시작·OOM 없음 / `p3 status --expect fixed --snap <S0>` 전부 PASS. 고정 전에는 HPA 2항목과 `p3 fixed` 4항목이 FAIL 인 것이 정상(오늘 확인), 고정 후 전부 PASS 여야 한다 [예측].
**(6) 복원 실패 시 대응**: 8.10절 표(F1~F9)와 수동 복구 5줄. 상태 판별은 `p3.py status --expect baseline|fixed|p2-baseline`, `kubectl get app -n argocd`, `kubectl get hpa -n parking-guardian`.
**(7) W3 이후 정확한 명령과 중단 조건**

```powershell
cd C:\parking_guardian\capston
$env:AWS_PROFILE = "parking-guardian"; $env:PYTHONUTF8 = "1"
$PY = "backend\venv\Scripts\python.exe"

# ── 시작 전 (읽기 전용) ──
& $PY loadtest\tools\p3.py status --expect baseline
& $PY loadtest\tools\p3.py status --expect p2-baseline
git -C ..\parking-guardian-config rev-parse --short HEAD          # ad00d85 여야 함
git status --short backend frontend                                # 비어 있어야 함
$Q = aws sqs get-queue-url --queue-name parking-guardian-judgement.fifo --query QueueUrl --output text
$D = aws sqs get-queue-url --queue-name parking-guardian-judgement-dlq.fifo --query QueueUrl --output text
$S = aws secretsmanager list-secrets --query "SecretList[?starts_with(Name,'rds!')].Name | [0]" --output text
& $PY loadtest\tools\p2_watch.py preflight --queue-url $Q --dlq-url $D --secret-id $S
& $PY loadtest\tools\p2_protect.py snapshot --source kubectl --out docs\measurements\phase2\results\<날짜>-b0-protect\baseline.json
& $PY loadtest\tools\p3.py snapshot --out docs\measurements\phase1\eks\b0-S0-manual

# ── 관찰자 (별도 터미널, 같은 환경변수) ──
& $PY loadtest\tools\p2_watch.py run --out docs\measurements\phase2\results\<날짜>-b0-observer\timeline.jsonl --queue-url $Q --dlq-url $D --secret-id $S --interval 15 --duration 4500 --db-period 30 --cadvisor-period 30 --allow-outofsync root,parking-guardian --aws-profile parking-guardian
#   5분 유휴 기준선을 본 뒤 계속

# ── W3 (승인 후) ──
& $PY loadtest\tools\p3.py pause                                   # 계획만 출력
& $PY loadtest\tools\p3.py pause --apply                          # 변경 4건. 출력되는 S0 경로를 기록
& $PY loadtest\tools\p3.py status --expect fixed --snap "<S0>"    # 전부 PASS
kubectl get app -n argocd                                          # root OutOfSync 여부 기록, 나머지 4개 Synced
& $PY loadtest\tools\e3_run.py 1 5 --snap "<S0>" --dry-run        # 게이트: 전부 PASS 여야 다음 단계

# ── W4 (별도 승인 후) ──
& $PY loadtest\tools\e3_run.py 1 5 --snap "<S0>"                  # A 3회에서 종료, 최대 5회
# 필요하면: & $PY loadtest\tools\e3_run.py --requery docs\measurements\phase1\results\<날짜>-b0-<n>

# ── W5 (별도 승인 후) ──
git -C ..\parking-guardian-config rev-parse --short HEAD          # S0 의 meta.json config_repo_head 와 같은지
kubectl get ns loadtest                                            # 없어야 함
& $PY loadtest\tools\p3.py restore --snap "<S0>"                  # 계획만 출력
& $PY loadtest\tools\p3.py restore --snap "<S0>" --apply --observe 300
& $PY loadtest\tools\p3.py status --expect restored --snap "<S0>"
& $PY loadtest\tools\p3.py status --expect p2-baseline
& $PY loadtest\tools\p2_protect.py verify --source kubectl --baseline docs\measurements\phase2\results\<날짜>-b0-protect\baseline.json
```

**중단 조건** (하나라도 해당하면 `e3_run` 이 k6 Pod 를 삭제하고 그 회차를 C(`safety_stop`)로 기록한 뒤 다음 회차로 가지 않는다. 사용자는 8.10절 복원으로 간다)
- HPA backend·inference 의 current 또는 desired 가 2 초과, HPA 가 min=max=2 가 아님
- inference 또는 backend Pod 가 2개가 아님/Ready 아님, Pending Pod, Worker Pod·Deployment replicas > 0
- 새 Pod, 재시작 증가, OOMKilled, `p3 status --expect fixed` 실패
- 가드 조회 3회 연속 실패(감시 불능)
- (관찰자) 사실 기반 ABORT(Argo 허용 범위 밖 OutOfSync·Degraded, KEDA Not Ready, DLQ 증가, Pod 재시작)
- 사용자 중단 요청 / 터미널 종료(F7: `kubectl delete ns loadtest` 후 복원)

## 10.6 W3 적용 전 체크리스트
- [ ] 사용자 승인: W3(pause) — W4·W5 는 각각 별도.
- [ ] 위 "시작 전" 읽기 점검 전부 합격 (오늘 22시 이후 값: baseline·p2-baseline PASS, config HEAD `ad00d85`, backend·frontend 변경 0건, SQS 0, RDS available, 다음 회전 11-12).
- [ ] 관찰자 실행 중 + 5분 유휴 기준선 + `--allow-outofsync root,parking-guardian`.
- [ ] S0 두 벌(수동 + pause 자동) 기록, 읽기 전용 복사. S1 은 증거용.
- [ ] 시험 중·복원 확인 전 `backend/`·`frontend/` push 금지, 장비 키 회전·사진 전송·Terraform 금지.
- [ ] pause 직후 `fixed` 검사와 `e3_run --dry-run` 이 전부 PASS 인 것을 확인한 뒤에만 W4 승인을 요청한다.
