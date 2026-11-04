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
