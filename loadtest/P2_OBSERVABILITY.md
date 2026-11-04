# 본 시험 전 관측 도구 개선 (2026-11-04, 스모크 결과 반영) — 로컬 코드·문서·모의 시험만 했다

스모크 결과: [P2_SMOKE_PLAN.md 6절](P2_SMOKE_PLAN.md). 도구 개요: [P2_TOOLS.md](P2_TOOLS.md). 표기: **[관찰]** 실제 AWS 스모크 기록·코드·로컬 측정에서 읽은 값, **[시험]** 로컬 모의 시험, **[추정]**, **[미확인]**.
이번 작업에서 AWS·클러스터·DB 에는 접속하지 않았다(조회도 하지 않았다). 아래 "새 방식의 실제 소요"는 모두 **다음 실제 실행에서 처음 측정된다**.

## 1. 관찰자 표본 간격이 늘어난 원인 [관찰]
| 항목 | 값 |
|---|---|
| 설정 | 15 s (`--interval 15`) |
| 실제 표본 간격 (스모크 타임라인, 표본 13개, 간격 12개) | 39.2 ~ 65.2 s, 평균 **48.1 s** (마지막 3개는 61~65 s) |
| 표본 1회 소요 (평균 간격 − 설정) | 약 33 s |
| 한 표본의 외부 호출 | 9개, **순차**: kubectl 5(pods·keda·app·top·exec) + aws 4(sqs 2·cloudwatch·secretsmanager) |
| 호출 하나의 시작 비용(로컬 측정, 클라우드 접속 없음) | `aws --version` 2.8~3.6 s, `kubectl version --client` 0.33~0.40 s, Python 시작 0.34~0.36 s |
- 해석 [추정, 호출별 시간은 미측정]: `aws.exe` 가 시작만 약 3 s 라서 AWS 호출 4개가 12 s 이상이고, `kubectl exec` 는 backend Pod 안에서 앱을 import 해 DB 를 읽는 Python 이라 수 초, 나머지 kubectl 호출 4개가 합쳐 수 초 → 합계 약 33 s.
- 부작용 1: 표본 시각은 **시작 시각**이고 값은 그 뒤 최대 30 s 사이에 읽혀 서로 어긋났다(예: DB 값은 표본 시각보다 늦게 읽힌다).
- 부작용 2: 어떤 호출이 느려지면 표본 전체가 밀린다(소스 하나가 모두를 붙잡는다). 오래된 값으로도 ABORT 규칙이 평가됐다.
- 부작용 3 [추정]: `kubectl exec` 가 매 표본마다 backend Pod 에서 앱을 import 한다. 스모크의 backend CPU 최대 223m(요청 200m)가 이것 때문이었을 수 있다. 이 호출은 시험 대상(backend)을 흔든다.

## 2. 개선된 수집 방식 (`p2_sampler.py`, `p2_watch.py`) [시험]
1. **소스별 독립 폴러 + 고정 주기 틱**: 소스(pods, keda, apps, top, sqs, dlq, sqs_age, secret, db, 선택 cadvisor·worker_logs)마다 스레드가 자기 주기로 읽는다. 느린 소스가 빠른 소스를 붙잡지 않는다. 틱(기본 15 s)이 "그 시점의 최신 값"을 모아 표본 한 줄을 만든다(표본을 만드는 데 I/O 가 없다).
2. **소스별 시각 기록**: 표본의 `sources[이름]` 에 시작·완료 시각, 소요(ms), 성공 여부, 오류, 값의 나이(`age_s`), 낡음 여부, 연속 실패 수가 남는다. `tick` 에 예정 시각·늦은 정도(`late_s`)·건너뛴 틱 수(`missed`)·표본 만드는 시간(`build_ms`)이 남는다.
3. **외부 호출마다 제한 시간**: 호출 15 s, `kubectl exec` 40 s(실행기가 자식 프로세스를 죽인다). boto3 는 연결·읽기 5 s, 재시도 1회.
4. **AWS 는 프로세스 없이 boto3**: 읽기 전용 래퍼(`ReadOnlyAws`)가 메서드 3개(`sqs_attrs`, `metric_points`, `secret_dates`)만 연다. 쓰기·비밀 값 메서드는 존재하지 않는다 [시험]. boto3 가 없으면 CLI 로 되돌아간다.
5. **부분 실패**: 한 소스가 실패하면 그 소스만 표본에서 빠지고(`errors` 에 이유), 마지막 성공 값의 나이로 낡음을 판단한다. 핵심 소스(pods, db)가 연속 3회 실패하면 ABORT, 그 밖의 소스는 경고만.
6. **수집 지연으로 인한 거짓 ABORT 방지**: 핵심 소스가 낡았거나 틱이 2 interval 넘게 늦은 표본은 `degraded` 로 표시한다. 그 표본에서는 **시간에 기반한** ABORT(Worker 정지, KEDA 확장 실패, Pod Pending, Relay 정지)를 경고로 낮춘다 - 상태 사실에 기반한 ABORT(DLQ, 재시작, 회전, Argo, KEDA Ready)는 그대로 낸다. DB 값이 없거나 낡으면 정지·확장 실패를 판단하지 않는다. 정지 판단의 기준 시각은 표본 시작이 아니라 **DB 를 실제로 읽은 시각**이다 [시험].
7. **누락·지연의 명시**: 틱을 놓치면 밀리지 않고 격자로 돌아오며 `missed` 를 센다. `p2_watch.py summarize` 가 표본 간격 분위수, 소스별 호출 소요·실패, 건너뛴 틱, degraded 표본 수를 낸다.
8. **AWS 호출량 상한** [시험: 폴러 주기에서 계산]: SQS 속성 분당 8회(본 큐+DLQ 각 15 s), CloudWatch 분당 1회(60 s), Secrets Manager 분당 0.2회(300 s) = 분당 9.2회. `kubectl exec`(DB)는 분당 2회(30 s).

### 달성 가능한 수집 간격과 한계
- **표본(틱) 간격은 15 s 로 고정**된다(소스 호출이 틱을 막지 않으므로). 시험으로 확인한 것은 가짜 소스에서의 규칙성이다 - 실제 소스의 소요는 **[미확인]**이며 첫 실제 실행에서 `summarize` 로 확인한다.
- 그러나 **값의 신선도는 소스마다 다르다**: kubectl 계열·SQS 속성 ≤ 15 s(+호출 소요), DB ≤ 30 s(+`exec` 소요), CloudWatch ≤ 60 s(+게시 지연), 회전 정보 ≤ 300 s. 표본 한 줄에 서로 다른 나이의 값이 섞이므로 `sources[*].age_s` 를 함께 읽는다.
- 15 s 보다 촘촘하게(예 5 s) 하면 apiserver 호출이 3배가 되고 `exec` 가 시험 대상을 더 흔든다 - 권하지 않는다. DB 주기 `--db-period` 는 15 s 까지 낮출 수 있으나 backend Pod 부하가 두 배가 된다.
- DB 값의 시각 불확실성 = `exec` 소요의 절반(읽은 시각을 호출 중간으로 둔다).
- 실제 달성 간격은 첫 실행에서 측정한다: 만약 핵심 소스 한 호출이 interval 보다 오래 걸리면 그 소스는 격자보다 드물게 갱신되고 `age_s` 로 드러난다(경고만 낸다).

## 3. Worker 준비 상태 측정 (`p2_phases.py`) [시험]
Kubernetes Ready 는 모델 준비와 같지 않다: Worker 에 readiness probe 가 없어(R3) 컨테이너가 시작하면 바로 Ready 가 되고 모델 로딩(약 9 s [관찰: 스모크 구성 추정])은 그 뒤에 한다. 단계를 구분해 근거 표시와 함께 보여 준다. **Worker 코드와 probe 는 바꾸지 않았다.**
| 단계 | 근거 | 비고 |
|---|---|---|
| KEDA 확장 결정 | `observed(KEDA 로그)` — `kubectl logs -n keda deploy/keda-operator` 의 `Successfully updated ScaleTarget … New Replicas Count: N` | `p2_watch.py keda-log` 로 저장(파드가 있는 동안만) |
| Pod 생성 · 노드 배치 · init(모델 내려받기) 종료 · 컨테이너 시작 · Ready | `observed(K8s)` — Pod 객체의 시각을 표본마다 타임라인에 저장 | Pod 가 사라져도 남는다 |
| 모델 로딩 완료 | 로그에 완료 줄이 있으면 `observed(로그)`(새 Worker Pod 로그에서 시작·로딩 관련 줄만 모아 두며, 비밀로 보이는 줄은 버린다), 없으면 `estimated(첫 판정 완료 − 그 추론 시간 = 상한)` | 로그 줄 형식은 **[미확인]** |
| 첫 판정 완료 | `observed(DB)` | 수집기 보고서 `workers_detail` |
출력에는 단계 간 소요, 콜드 스타트 총합(결정→첫 판정), Ready 와 모델 준비 사이의 간격(`k8s_ready_vs_model_loaded_s`), 빠진 단계 목록이 들어간다. 직접 못 본 것은 `estimated(...)` 로 적힌다.

## 4. CPU·메모리 관측 검토
**실제 사용량과 requests 를 구분한다**: requests(예약, 스케줄러가 자리를 잡는 값)와 CPU limit(없음)과 실제 사용량은 서로 다르다. "requests 초과"는 **사용량이 요청보다 큰 구간이 있었는가**이며 limit 위반이 아니다.
| 방법 | 해상도·지연 | 스모크에서 [관찰] | 평가 |
|---|---|---|---|
| `kubectl top` (metrics-server) | 평균값, 설정에 따라 15~60 s [미확인: 이 클러스터의 `--metric-resolution`] | **Worker CPU 최대 1m** (실제로는 약 10 s 추론 구간이 있었다) | 짧은 버스트를 놓친다. 지속 부하의 교차 확인용 |
| **cAdvisor 누적 카운터** (`kubectl get --raw /api/v1/nodes/<node>/proxy/metrics/cadvisor`, 읽기 전용, `p2_watch.py run --cadvisor-period 30`) | **누적 CPU 초**라서 호출 간격이 길어도 구간 합계가 정확하다. 평균 CPU = Δ초/Δ시간, 구간 최대는 호출 간격 단위 | 아직 실제 클러스터에서 실행하지 않았다 | **권고**. 지속 부하 단계의 평균·합계·"CPU 초 / 완료 사진 수". 출력 형식과 `nodes/proxy` 권한은 **[미확인]** |
| Container Insights (CloudWatch Agent 애드온, 이미 설치) | 기본 약 60 s [미확인: 이 설정], 수집 지연 수 분 | `/aws/containerinsights/parking-guardian/performance` 로그 그룹이 있고(보존 7일) 지난 E2E 에서 수신 확인 | 사후 백업·교차 확인용. 지표 이름·차원은 이 계정에서 확인하지 않았다 **[미확인]**. 도구에는 아직 읽는 코드가 없다 |
| Prometheus (kube-prometheus-stack, Argo 앱 Synced) | 스크레이프 주기(기본 30 s 안팎 [미확인]) | Phase 1 에서 사용 | `rate(container_cpu_usage_seconds_total[1m])` 가능. 접근에 port-forward 가 필요해 읽기 전용 허용 목록에 넣지 않았다 |
- **Worker ↔ Inference 비교**: 같은 단계(예: 1.0C 180 s) 창에서 두 Pod 의 `container_cpu_usage_seconds_total` 증가량 ÷ 그 창에 완료된 사진 수 = "사진 한 장당 CPU 초". 평균 CPU / requests, 구간 최대 / requests 를 함께 낸다(`p2_watch.py resources` 의 `cadvisor` 항목). Phase 1 은 inference Pod, Phase 2 는 Worker Pod 로 같은 창 길이에서 계산한다. 백엔드·Relay 의 몫은 따로 보고한다(요청당 하는 일이 다르다).
- **requests 1000m 초과 판단**: 지속 부하 단계에서 Pod 평균 CPU 가 requests 의 1.2배(1200m) 넘게 이어지면 "더 많은 CPU 를 썼다"로 표시한다(이전 공정성 기준). 짧은 구간 최대가 1000m 을 넘는 것만으로는 초과로 판단하지 않는다(CPU limit 이 없고 노드가 한가하면 가능하다).
- 새 Worker Pod 는 카운터가 0 에서 시작하므로 첫 관찰값까지의 CPU(초기화·모델 로딩)를 더하고, 생성 시각부터를 분모로 쓴다 [시험].
- **하지 않은 것**: 모니터링 컴포넌트 설치·설정 변경. 사용한 것은 이미 있는 노드 cAdvisor 와 읽기 전용 API 뿐이다.

## 5. SQS · KEDA 관측 보완
| 한계 | 보완 |
|---|---|
| **CloudWatch SQS 지표는 1분마다의 스냅샷**이다. 스모크에서 워밍업 3건이 약 30 s 큐에서 기다렸는데 지표는 전 구간 0 이었다 [관찰] | DB 의 미완료 작업(`pending_publish`+`queued`+`processing`) 수와 상태별 가장 오래된 나이를 30 s 마다 읽는다. `p2_watch.py summarize` 가 **대기열 에피소드**(시작·끝·최대 크기·지속시간 최솟값/최댓값·Worker Ready 여부·진행 여부)를 만들고, 최대 지속 60 s 이하는 `short`, 그보다 길면 `long`, Worker 가 Ready 인데 60 s 이상 새 판정이 없으면 `stalled`, 아직 안 끝났으면 `open` 으로 구분한다 |
| 지속시간의 불확실성 | DB 읽기 주기만큼(기본 ±30 s). 보고서에 `resolution_s_median` 을 함께 낸다 |
| Relay 발행 시각 | 최근 120 s 접수 작업의 `published_at − received_at` 최댓값과 미발행 수를 DB 에서 같이 읽는다(`db.recent`) |
| KEDA 확장·축소 시각 | ① KEDA 로그의 결정 시각(`keda-log`) ② Pod 생성·Ready 시각 ③ ScaledObject `Active` 변화 ④ Worker 수 변화. 타임라인 이벤트는 "변화를 처음 본 표본"의 시각이라 최대 한 틱의 오차가 있다 |
| 모델 준비 | 3절 |

### KEDA 쿨다운 - 확정 사실이 아니라 **본 시험에서 검증할 가설**
스모크 관찰(각 1회): 마지막 측정 사진 판정 완료 10:46:58, KEDA 의 `minReplicaCount` 로의 축소 10:51:01(**243 s 뒤**), 직전 Worker 활동은 워밍업 판정(약 10:46:03). 설정된 `cooldownPeriod` 는 300 s.
- **H-K1**: KEDA 의 쿨다운은 "마지막 작업 완료"가 아니라 "KEDA 가 마지막으로 활동(큐 길이/처리 중 메시지 > 0)을 **폴링으로 본** 시각"부터 센다. 측정 사진 10장은 각각 1 s 안에 끝나 15 s 폴링에 잡히지 않았을 수 있다. 스모크의 243 s 는 이와 일치하지만 **한 번의 관찰이며 다른 설명(예: 폴링 시각, HPA 안정화 창)을 배제하지 못했다.**
- **H-K2**: 사진이 드물게(폴링 간격 15 s 보다 처리 시간이 짧은 채로) 오면 KEDA 가 사진이 계속 오는 중에도 Worker 를 0 으로 내려 매번 콜드 스타트(약 30 s)를 치를 수 있다.
- **H-K3**: 0→1 확장 반응(발행→KEDA 결정)은 폴링 주기(15 s)에 따라 분포한다. 관찰값: 이전 E2E 7.5 s·1 s, 스모크 약 14 s. 분포는 콜드 스타트 반복(≥ 5회)으로 본다.
- **검증 방법(본 시험, P2-C/P2-K)**: 유휴 상태에서 사진 1장을 간격 60·120·240 s 로 보내며 `Active` 전환, KEDA 로그 결정 시각, Worker 수 변화를 기록하고, 마지막 폴링에서 본 활동 시각(`Active=True` 마지막 표본)과 축소 시각의 차를 비교한다. 마지막 판정 완료와 축소 사이 시간을 같이 낸다.

## 6. 로컬 검증 결과 [시험]
관찰자 시험 31개(병렬 수집 시간, 소스별 시각 기록, 시간 초과의 부분 실패, 낡은 값 제외와 degraded 표시, 거짓 ABORT 보류와 실제 ABORT 유지, DB 읽은 시각 기준 판정, 고정 주기 틱·놓친 틱, 실제 스레드 루프의 규칙성, 호출량 상한, 읽기 전용 래퍼·실행기, cAdvisor 해석과 CPU 계산, 대기열 에피소드 분류, 간격 통계, Worker 로그 수집, KEDA 로그 해석, 단계 분석, 새 표본의 JSON 호환과 옛 소비자, **실제 스모크 타임라인과의 호환**, 비밀 정보). 전체 회귀 결과는 완료 보고서.

## 7. 아직 실제로 확인하지 못한 것 [미확인]
1. 새 수집기의 실제 호출 소요와 실제 표본 신선도(첫 실제 실행에서 `summarize` 로).
2. `kubectl get --raw …/metrics/cadvisor` 의 실제 출력 형식과 권한, 크기.
3. Worker 로그에서 모델 로딩 완료 줄의 실제 형식(정규식이 맞는지).
4. KEDA 로그의 축소 결정 줄 형식(확장 줄은 스모크에서 확인, 축소 줄은 일부만 확인).
5. `kubectl top` 의 실제 해상도 설정, Container Insights 지표 이름.
6. `kubectl exec` 의 시험 대상 교란 정도(DB 주기 30 s 의 효과).
