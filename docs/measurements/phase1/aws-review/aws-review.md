# 단계 5: AWS 인프라 변경 검토 — 분석과 plan 까지 (2026-10-30, 결정 반영 갱신)

> **이 단계에서 하지 않은 것**: Terraform apply, AWS 리소스 생성·수정, EKS 배포, Kubernetes 매니페스트 적용, Git push. 아래 변경은 모두 **로컬 작업 트리의 미커밋 파일**이다.
> **한 것**: AWS 읽기 전용 조회(애드온 버전·스키마, RDS 설정), `terraform plan`(`-lock=false`, plan 파일은 저장소 밖), `kubectl kustomize` 오프라인 렌더링, 로컬 계산.
> 로컬(이 PC)에서 확인한 것과 EKS에서 추가로 확인해야 하는 것은 10절에 따로 정리했다. 계정 ID·ARN은 `<acct>`로 치환했다.

## 0. 사용자 결정과 이 문서의 갱신 (2026-10-30, 5단계 보고 뒤)

| 결정 | 내용 | 반영 |
|---|---|---|
| CloudWatch 버전 | **P1(v6.7.0 고정) 선택.** nodeExporter·kubeStateMetrics 는 명시적 비활성화를 유지한다. v6.7.0 에서 두 구성 요소가 실행되지 않았던 정확한 이유는 미확인이므로 **재구축 직후 실제 설치 여부와 CPU 요청을 검증**한다 | 이미 구현(`addons.tf`). 검증은 `deploy-checklist.md` G7 |
| API HPA 최대 | **3 으로 설정.** api·inference 모두 최대 3 을 시작 기준으로 삼고, 노드 자원과 Pod 배치를 확인해 확정한다. **실험 환경 설정이며 운영의 최종 확장 한도가 아니다** | `backend-hpa.yaml`, 렌더링 체커, 5절 배치 계산(최대 상태와 롤링 시나리오 추가) |
| 커밋 | app, infra, config 세 레포를 민감정보 검사 후 커밋. **push 는 하지 않는다** | 완료 (infra·config 는 `main` 이 아니라 브랜치에 커밋) |

| 추가 확인 | 반영 위치 |
|---|---|
| 1. 재구축 리소스와 Phase 1 신규 리소스 구분 | 1.1절 |
| 2. AI 대기 중 DB 연결 점유를 부하 테스트 관찰 항목에 | 7절, 사전 등록 README 5.3, 설계 5.3 |
| 3. IAM 분리가 실제 Pod Identity 로 적용되는지 EKS 배포 후 검증 | `deploy-checklist.md` G7, 설계 6-3 |
| 4. IMDS 노드 자격 증명, AI Pod 의 RDS 네트워크 접근은 미해결 위험으로 유지 | 3절, 12절, 체크리스트 G7(증거만 기록) |
| 5. RDS `max_connections` 는 DB 가동 후 확인 | 7절, 체크리스트 G5 |
| 6. ECR → CI → config → EKS 순서 재확인, 중간 전환 방지 | 11절, `deploy-checklist.md`, `scripts/preflight-deploy.py` |

## 1. 변경 예정 리소스 한눈에

| 레포 | 변경 | 규모 |
|---|---|---|
| infra `data` 스택 | ECR 리포지토리 `parking-guardian/inference` + lifecycle 정책 새로 생성. GitHub Actions ECR 푸시 정책은 **대상 목록이 자동으로 늘어 제자리 변경 1건** | plan: **2 to add, 1 to change, 0 to destroy** (기존 27개 no-op) |
| infra `cluster` 스택 | inference 역할, 인라인 정책, Pod Identity 연결 3개 추가. backend 역할의 정책에서 모델 권한 제거. CloudWatch add-on 버전 고정 + 중복 구성 요소 끔 | plan: **34 to add** (변경 전 기준 31 + 3, 이 스택은 10-28에 해체되어 처음부터 만든다). 기존 리소스 변경·삭제 없음 |
| config 레포 | 신규 6개(ServiceAccount, Deployment, Service, HPA, PDB, PodMonitor), 수정 5개(backend Deployment·HPA·PDB, kustomization, config.env.example)와 스크립트 | 렌더링 21개 오브젝트 |
| 앱 레포 | `build-push.yml`: backend는 `api` 타깃, inference 추가, 태그 갱신 3줄 | actionlint 새 경고 없음 |

plan에서 새로 생기는 리소스 주소(변경 전 기준 plan과 비교; 1.1절에서 재구축분과 구분한다):

```
cluster: baseline 31 creates -> 34 creates; added: ['aws_eks_pod_identity_association.inference', 'aws_iam_role.inference', 'aws_iam_role_policy.inference_models'] | removed: []
data:  {'aws_ecr_lifecycle_policy.this["inference"]': ['create'], 'aws_ecr_repository.this["inference"]': ['create'], 'aws_iam_role_policy.github_actions_ecr': ['update']} | no-op: 27
```

### 1.1 재구축 리소스와 Phase 1 신규 리소스 (plan 의 숫자를 나눠 읽는다)

`cluster` 스택은 10-28 에 해체되어 plan 이 **처음부터 만드는** 형태다. 그래서 "34 to add" 는 Phase 1 이 34개를 새로 더한다는 뜻이 아니다.

| 구분 | 개수 | 내용 |
|---|---|---|
| 기존 클러스터 재구축 리소스 | **31** | 10-28 에 destroy 한 31개와 같은 구성 (EKS, 노드 그룹, NAT, EIP, 라우트, 애드온 7, IAM 역할 5, 로그 그룹 4, 시작 템플릿, backend Pod Identity 연결 등). 변경 전 코드의 기준 plan 이 31 |
| 　└ 그중 **내용이 Phase 1 로 바뀌는 것** | 2 | backend 인라인 정책(`backend_s3`: 모델 권한 제거), CloudWatch 애드온(버전 고정 + 두 구성 요소 끔) |
| **Phase 1 신규** | **3** | `aws_iam_role.inference`, `aws_iam_role_policy.inference_models`, `aws_eks_pod_identity_association.inference` |
| 합계 | 34 | |

`data` 스택은 이미 있는 인프라 위의 증분이다: 신규 2(ECR `inference`, lifecycle 정책) + 제자리 변경 1(GitHub Actions 푸시 정책의 대상 목록) + 변경 없음 27. 파괴·교체는 없다.
## 2. ECR: AI 추론 서비스 전용 저장소 (요청 1)

- 변경은 `data/storage_ecr.tf`의 저장소 집합에 `"inference"` 한 단어를 더한 것뿐이다. `for_each` 구조라 **이미지 불변 태그(IMMUTABLE), 푸시 시 취약점 스캔, AES256 암호화, lifecycle 정책(태그 있는 이미지 최근 10개, untagged 1일)이 backend·frontend와 똑같이** 따라온다.
- **CI 푸시 권한은 별도 수정이 없다.** `cicd.tf`의 정책이 `[for r in aws_ecr_repository.this : r.arn]`를 대상으로 하므로 새 저장소가 자동 포함된다(plan의 "제자리 변경 1건"이 이것이다). 설계의 `[확인 필요]` 항목이 해소됐다. 출력 `ecr_repository_urls`도 같은 맵이라 `inference` 키가 자동으로 생긴다.
- backend 저장소에는 이제 **API 이미지(`api` 타깃)** 가 올라간다. 같은 저장소에 Prototype 이미지(옛 태그)와 api 이미지(새 태그)가 섞이지만 태그가 커밋 해시라 구분된다.

## 3. 권한 분리: API와 AI (요청 2)

| | backend(API) | inference(AI) |
|---|---|---|
| Pod Identity 역할 | `parking-guardian-backend` | `parking-guardian-inference` (**신규**) |
| S3 사진 버킷 `violations/*` | Put/Get | **없음** |
| S3 모델 버킷 `v*/` | ~~List/Get~~ **제거** | List/Get (initContainer의 `aws s3 sync`가 쓴다) |
| Secrets Manager, RDS, KMS, IAM | 역할에는 없음 (DB 비밀번호는 External Secrets가 만든 **K8s Secret**으로 전달) | 없음 |
| K8s Secret(`backend-secrets`) | 마운트·참조 | **참조하지 않음** |
| 환경 설정 | `backend-config` 전체 | 전용 `inference-config`(키 4개) + `backend-config`에서 **키 3개만**(모델 버킷, 버전, 리전) |
| ServiceAccount 토큰 | 마운트 안 함 | 마운트 안 함 |

검증(`scripts/check-phase1-plan.py`, `check-phase1-render.py`, 아래 원문): inference 역할의 동작은 `s3:ListBucket`, `s3:GetObject` 둘뿐이고 대상은 모델 버킷뿐이다. AWS 관리형 정책 attachment가 없다. 렌더링된 inference Pod에는 Secret 참조가 0개, DB·비밀·사진 버킷 환경변수가 0개다. `backend-secrets`를 쓰는 워크로드는 backend와 마이그레이션 Job뿐이다.

### 남는 한계 — 미해결 위험으로 유지 (사용자 지시 2026-10-30. EKS 에서 도달 가능성을 증거로 기록만 하고 이번 계획에서 해결하지 않는다)

1. **노드 IAM 역할 경로**: 노드 시작 템플릿의 IMDS 홉 제한이 **2**다(`nodes.tf`, EKS 기본값. CloudWatch 에이전트 등이 IMDS를 쓰기 때문일 수 있다). 그래서 어떤 Pod든 IMDS를 통해 **노드 역할**의 자격 증명을 얻을 수 있다. 노드 역할은 ECR 읽기, CNI(ENI 관리), SSM Core, 워커 노드 정책을 가진다. Secrets Manager·S3·RDS 권한은 없다. 즉 inference Pod가 침해되면 노드 역할 범위(ECR 이미지 읽기, ENI 조작 등)에는 닿는다. 이것은 Prototype의 backend Pod에도 이미 있던 속성이다.
   - 홉 제한을 1로 낮추면 막히지만, CloudWatch 에이전트·ALB 컨트롤러·EBS CSI가 IMDS에 의존하는지 확인하기 전에는 **바꾸지 않는다**(깨질 수 있다). 대안은 NetworkPolicy로 inference에서 `169.254.169.254` 이그레스를 막는 것인데, VPC CNI의 네트워크 정책 기능을 켜야 한다(현재 `vpc-cni` 애드온은 설정 없이 기본값). 둘 다 **미검증이고 이번 계획에 넣지 않았다** → 설계 11장 F7.
2. **네트워크로는 RDS에 닿는다**: RDS 보안 그룹은 노드 SG에서 5432를 연다. inference Pod도 같은 노드 SG를 쓰므로 TCP는 닿을 수 있다. 다만 DB 자격 증명(Secret)을 받지 못해 인증을 통과할 수 없다. 포트 자체를 막으려면 NetworkPolicy가 필요하다(위와 같은 전제, F7).
3. **Pod Identity 연결은 ServiceAccount 이름 계약이다**: infra `var.inference_service_account = "inference"`와 config `serviceaccount-inference.yaml`이 같아야 한다. 어긋나면 initContainer가 `Unable to locate credentials`로 실패한다(렌더링 검사가 ServiceAccount 이름은 확인하지만 Terraform 변수와의 일치는 사람이 본다).

## 4. Kubernetes 매니페스트 (요청 3)

| 파일 | 내용 (설계 3.5.1, 3.7, 9.1) |
|---|---|
| `inference-deployment.yaml` | 이미지는 ECR `inference`. 요청 1000m / 1.5Gi, 메모리 limit 2Gi, CPU limit 없음. 롤링 surge 1 / unavailable 0. **preferred** 분산(가중치 100) + AZ spread(ScheduleAnyway). 종료 유예 45초. initContainer `fetch-models`. startupProbe `/readyz` 5초×36(180초), readinessProbe `/readyz`, livenessProbe `/healthz` 10초×3. 포트 8000/9102. 권한 상승 금지, capability 전부 제거. sync-wave 2 |
| `inference-service.yaml` | ClusterIP 8000. ALB 어노테이션 없음, Ingress에 없음 |
| `inference-hpa.yaml` | 2 ~ 3, CPU 70%(시작값), 축소 안정화 300초 |
| `inference-pdb.yaml` | minAvailable 1 |
| `inference-podmonitor.yaml` | `metrics` 포트 스크레이프 (`pg_inference_*`) |
| `serviceaccount-inference.yaml` | `inference`, 토큰 자동 마운트 끔 |
| `backend-deployment.yaml` (수정) | 모델 initContainer·볼륨 **제거**. 요청 200m / 256Mi, 메모리 limit 512Mi. required → **preferred** 분산. 종료 유예 130 → 45초 |
| `backend-hpa.yaml` / `backend-pdb.yaml` (수정) | HPA 2 ~ **3** (3노드 실험 시작 기준, 최종 한도 아님), 축소 안정화 120초. PDB minAvailable 50% → 1 |
| `kustomization.yaml` (수정) | 리소스 6개 추가, 이미지 `parking-guardian/inference`(`newTag: "unset"`), `ECR_INFERENCE` replacements, `inference-config` 생성기, `backend-config`에 AI 연결 설정(`INFERENCE_BACKEND=http`, `INFERENCE_URL`, `GUNICORN_THREADS=8`, `AI_CLIENT_DEADLINE=10`, `AI_CLIENT_MAX_CONCURRENCY=4`) |
| `config.env.example`, `scripts/export-config-env.py` | `ECR_INFERENCE` 키 추가 |

요청 CPU/메모리, HPA 숫자, `AI_CLIENT_MAX_CONCURRENCY=4`, `TORCH_NUM_THREADS=1`은 모두 **추정·시작값이고 확정이 아니다**(EKS의 E0/E1/S0/T1~T3에서 정한다). 대시보드 JSON(`build_dashboards.py`) 재구성은 이번에 하지 않았다(PodMonitor로 지표는 수집된다).

렌더링된 오브젝트 21개:

```
ConfigMap: backend-config, frontend-nginx, inference-config
Deployment: backend, frontend, inference
ExternalSecret: backend-secrets
HorizontalPodAutoscaler: backend, inference
Ingress: parking-guardian
Job: backend-migrate
Namespace: parking-guardian
PodDisruptionBudget: backend, inference
PodMonitor: backend, inference
Service: backend, frontend, inference
ServiceAccount: backend, inference
total 21
```

## 5. 3노드 배치와 CPU·메모리 요청 (요청 4)

`scripts/check-placement.py`가 **렌더링된 요청값**과 노드 상수(출처는 스크립트 머리말: 2026-10-28 `kubectl describe nodes`에서 역산한 allocatable 1930m / 7.2 GiB, 노드당 기본 예약 650m / 640Mi)로 계산했다. 클러스터가 내려가 있어 노드를 다시 잴 수 없으므로 **재구축 직후 `kubectl describe nodes`로 상수를 다시 확인해야 한다.**

- 모니터링(Prometheus, Grafana 등)과 Argo CD는 values에 **메모리 요청만** 있고 CPU 요청이 없어 CPU 계산에서 사실상 0이다. ALB 컨트롤러·ESO 차트의 기본 요청은 확인하지 못했다(0으로 가정) **[미확인]**.
- "best-case"는 요청을 만족하는 배치가 존재하는가이고, "scheduler 모델"은 기본 스케줄러를 흉내 낸 순차 배치다. 후자는 모델이며 실제 배치는 `kubectl get pods -o wide`(설계 V1)로 봐야 한다.

```
요청 (렌더링된 매니페스트): api 200m / 256Mi | inference 1000m / 1536Mi | frontend 50m / 64Mi × 1 | 마이그레이션 Job 100m / 256Mi (배포 때만)
노드 3대 상수: allocatable 1930m / 7373Mi, 기본 예약 650m / 640Mi → 앱이 쓸 수 있는 양 노드당 1280m / 6733Mi

=== 현행 설계: CloudWatch v6.7.0 고정 + 중복 구성 요소 끔
    노드당 남는 양 1280m / 6733Mi, 노드에 묶이지 않는 Pod 합 50m / 1760Mi
| 시나리오 | best-case | 남는 CPU(최소 노드) | scheduler 모델 (순서: api → inference → 늘어난 쪽) |
|---|---|---|---|
| 평상시 (api 2 + inference 2) | 가능 (N1: api×2 ; N2: inference×1 ; N3: inference×1) | 280m | N1: api×1, inference×1 ; N2: api×1 ; N3: inference×1 |
| S4 프로필 1 (api 2 + inference 3) | 가능 (N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: inference×1) | 80m | N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: inference×1 |
| S4 프로필 2 (api 4 + inference 2) | 가능 (N1: api×4 ; N2: inference×1 ; N3: inference×1) | 280m | N1: api×2 ; N2: api×1, inference×1 ; N3: api×1, inference×1 |
| HPA 최대 동시 (api 3 + inference 3) | 가능 (N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: api×1, inference×1) | 30m | N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: api×1, inference×1 |
| [참고] 이전 HPA 최대 (api 4 + inference 3) | **불가능** | - | **inference 가 Pending** |
| 최대 상태에서 api 롤링 (api 3+surge 1 + inference 3) | **불가능** | - | **inference 가 Pending** |
| 최대 상태에서 inference 롤링 (api 3 + inference 3+surge 1) | **불가능** | - | **inference 가 Pending** |
| inference 롤링 (api 2 + inference 2+surge 1) | 가능 (N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: inference×1) | 80m | N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: inference×1 |
| api 롤링 (api 2+surge 1 + inference 2) | 가능 (N1: api×3 ; N2: inference×1 ; N3: inference×1) | 280m | N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: api×1 |
| 둘 다 롤링 (api 3 + inference 3) | 가능 (N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: api×1, inference×1) | 30m | N1: api×1, inference×1 ; N2: api×1, inference×1 ; N3: api×1, inference×1 |
| 평상시 + 마이그레이션 Job (배포 때) | 가능 (N1: api×2, migrate×1 ; N2: inference×1 ; N3: inference×1) | 280m | N1: api×1, inference×1 ; N2: api×1, migrate×1 ; N3: inference×1 |

=== CloudWatch 중복 구성 요소 켜짐 (Terraform 이 끄는 것의 반대)
    노드당 남는 양 1024m / 6605Mi, 노드에 묶이지 않는 Pod 합 306m / 1888Mi
| 시나리오 | best-case | 남는 CPU(최소 노드) | scheduler 모델 (순서: api → inference → 늘어난 쪽) |
|---|---|---|---|
| 평상시 (api 2 + inference 2) | 가능 (N1: api×2 ; N2: inference×1 ; N3: inference×1) | 24m | **inference 가 Pending** |
| S4 프로필 1 (api 2 + inference 3) | **불가능** | - | **inference 가 Pending** |
| S4 프로필 2 (api 4 + inference 2) | **불가능** | - | **inference 가 Pending** |
| HPA 최대 동시 (api 3 + inference 3) | **불가능** | - | **inference 가 Pending** |
| [참고] 이전 HPA 최대 (api 4 + inference 3) | **불가능** | - | **inference 가 Pending** |
| 최대 상태에서 api 롤링 (api 3+surge 1 + inference 3) | **불가능** | - | **inference 가 Pending** |
| 최대 상태에서 inference 롤링 (api 3 + inference 3+surge 1) | **불가능** | - | **inference 가 Pending** |
| inference 롤링 (api 2 + inference 2+surge 1) | **불가능** | - | **inference 가 Pending** |
| api 롤링 (api 2+surge 1 + inference 2) | 가능 (N1: api×3 ; N2: inference×1 ; N3: inference×1) | 24m | **inference 가 Pending** |
| 둘 다 롤링 (api 3 + inference 3) | **불가능** | - | **inference 가 Pending** |
| 평상시 + 마이그레이션 Job (배포 때) | 가능 (N1: api×2, migrate×1 ; N2: inference×1 ; N3: inference×1) | 24m | **inference 가 Pending** |

=== 민감도: api 3 + inference 3 (노드마다 inference 1 + api 1 → 노드마다 80m 남는다)
    노드에 묶이지 않는 Pod 의 CPU 요청 합이 80m 를 넘으면 들어가지 않는다. 지금 가정은 50m (frontend 뿐) → 남는 여유 30m
| 노드에 묶이지 않는 Pod 의 CPU 합 | api 3 + inference 3 |
|---|---|
| 50m | 들어간다 |
| 80m | 들어간다 |
| 81m | **들어가지 않는다** |
| 130m | **들어가지 않는다** |
| 250m | **들어가지 않는다** |
    (ALB 컨트롤러·ESO 의 차트 기본 요청과 Argo CD·모니터링의 CPU 요청은 확인하지 못했다. 재구축 직후 `kubectl describe nodes` 의 노드별 요청 합으로 확인한다)

설계가 기대하는 결론과 계산이 같다
```

**결론**
1. **평상시(api 2 + inference 2)는 3노드에 들어간다.** inference와 api가 같은 노드에 공존해도 1200m / 1280m이라 N1·N2에 각각 80m가 남고 N3에 여유가 크다. 메모리는 노드당 약 6.7 GiB가 남아 문제없다.
2. S4 프로필 1(inference 3 + api 2), 프로필 2(**api 3** + inference 2), inference 롤링(surge), api 롤링, 마이그레이션 Job 동시 실행은 모두 들어간다.
3. **둘 다 최대(api 3 + inference 3, 사용자 결정 2026-10-30)도 들어가지만 여유가 30m 뿐이다.** 노드마다 inference 1 + api 1(1200m / 1280m)이라 노드마다 80m가 남고, **노드에 묶이지 않는 Pod(frontend 50m + 확인 못 한 ALB 컨트롤러·ESO·Argo CD·모니터링의 CPU 요청)의 합이 80m를 넘으면 들어가지 않는다**(민감도 표). 이전 설계 서술(N3 여유 약 130m, "inference 3일 때 api 3번째부터 들어갈 노드 없음")은 그 합을 약 150m로 잡은 가정이었다. 지금 계산은 values에서 CPU 요청이 있는 것은 frontend뿐이라는 근거로 50m로 잡았다. **어느 쪽이 맞는지는 재구축 직후 노드별 요청 합으로 확인한다**(체크리스트 G7). 이전 HPA 최대 api 4 + inference 3은 어느 가정에서도 들어가지 않는다.
4. **최대 상태에서 롤링 업데이트는 surge Pod가 들어갈 자리가 없다**(api 3+1 + inference 3, 또는 api 3 + inference 3+1 모두 불가능). `maxUnavailable: 0`이라 **서비스는 끊기지 않고 롤링이 지연**되며, 부하가 줄어 HPA가 내려가면(api 축소 안정화 120초, inference 300초) 이어진다. 평상시(2+2)의 롤링은 문제없다. 최대 상태의 롤링 지연은 부하 테스트의 관찰 항목이다. 지연이 문제가 되면 배포 전에 HPA 하한을 쓰거나 롤링 전략을 바꾸는 선택지가 있으나 **지금은 적용하지 않는다.**
5. N1·N2의 여유가 80m뿐이라 **CPU 요청이 있는 임시 Pod를 더하면(예: k6 Job 100m) 그 노드에는 들어가지 않는다.** N3에 들어간다.
6. **CloudWatch 중복 구성 요소가 켜지면(아래 6절) 배치가 무너진다**: 노드당 남는 CPU가 1024m로 줄어 inference(1000m)와 api(200m)가 같은 노드에 들어가지 못한다. 평상시도 api 둘을 한 노드에 몰아야만 들어가고(여유 24m, 노드 단위 고가용성 포기), **inference 롤링(surge)과 inference 확장(S4 프로필 1)은 불가능**하다. api 롤링은 api를 한 노드에 모을 때만 가능하다.

## 6. CloudWatch Observability add-on (요청 5)

**읽기 전용 조회로 알게 된 사실 (2026-10-30)**

| 항목 | 값 |
|---|---|
| EKS 1.35용 기본 버전 | **v7.0.0-eksbuild.1** (v6.7.0, 6.6.0 … 도 선택 가능) |
| 10-27/28 클러스터가 쓴 버전 | v6.7.0 (그때의 기본 버전, `addons.tf` 주석) |
| 두 버전 설정 스키마 차이 | **Knative / KServe / vLLM 스크랩 옵션이 v7.0.0에 추가된 것뿐** |
| `nodeExporter`, `kubeStateMetrics` | **두 버전 모두** 스키마에 있고 `enabled` 기본값 `true`, 요청 256m / 128Mi |

**정정**: 설계 3.12는 "v7이 설치되면 nodeExporter·kubeStateMetrics가 추가로 뜰 수 있다"고 적었지만, **스키마만으로는 그 근거가 없다**(두 버전이 같다). 반대로 10-27/28의 v6.7.0 클러스터에서는 두 구성 요소가 실제로 보이지 않았다(요청 합 700m). 스키마의 "기본값"과 실제 배포 동작이 다를 수 있고 그 이유는 확인하지 못했다. 그래서 **버전이 무엇이든 명시적으로 끈다.**

**Terraform 변경안 (구현함, 미적용)**: `addon_version = "v6.7.0-eksbuild.1"`로 고정하고, `configuration_values`에 `nodeExporter.enabled = false`, `kubeStateMetrics.enabled = false`를 더했다(기존 `applicationSignals` 끔 설정은 유지). 키는 고정 버전의 공개 스키마와 맞는지 확인했다. 이유:
- v6.7.0은 **요청 합을 실제로 잰 버전**이라 3노드 배치 계산(5절)의 상수와 같은 조건이다.
- 명시적 끄기는 버전이 바뀌어도 결과가 같게 한다. kube-prometheus-stack이 이미 node-exporter(노드마다)와 kube-state-metrics를 돌리므로 중복이다.

| 선택지 | 내용 | 비고 |
|---|---|---|
| **P1 (구현)** | v6.7.0 고정 + 두 구성 요소 명시적 끔 | 측정된 요청 합과 같은 조건. 올릴 때는 이 줄과 스키마를 같이 확인 |
| P2 | v7.0.0 고정 + 두 구성 요소 명시적 끔 | 한 줄만 바꾸면 된다. 최신이지만 요청 합을 이 프로젝트에서 잰 적이 없다 |
| **사용자 결정 (2026-10-30)** | **P1 선택.** 중복 구성 요소의 명시적 비활성화 유지 | v6.7.0 에서 두 구성 요소가 실행되지 않았던 이유는 미확인이다 |

**재구축 직후 확인**: `kubectl -n amazon-cloudwatch get ds,deploy`에 에이전트·Fluent Bit·매니저만 있어야 하고(노드 요청 합 700m / 노드 2대 기준), 문제가 있으면 5절의 상수를 고친다.

**P1 확정에 따른 재구축 직후 검증 (체크리스트 G7)**: 이유를 모르는 채 "안 뜬다"에 기대지 않고 실제를 본다.
```
kubectl -n amazon-cloudwatch get ds,deploy,pods -o wide
kubectl -n amazon-cloudwatch get pods -o custom-columns=NAME:.metadata.name,CPU:.spec.containers[*].resources.requests.cpu,MEM:.spec.containers[*].resources.requests.memory
aws eks describe-addon --cluster-name parking-guardian --addon-name amazon-cloudwatch-observability --query 'addon.{version:addonVersion,config:configurationValues}'
```
기대: 에이전트(DaemonSet)·Fluent Bit(DaemonSet)·매니저 1개만 있고 `node-exporter`·`kube-state-metrics` 이름의 워크로드가 **없다**. 애드온 버전 `v6.7.0-eksbuild.1`. CPU 요청 합 = 노드마다 300m + 클러스터에 100m (3노드 1,000m). 다르면 **멈추고 보고한다**(5절 상수가 틀린 것이다).
## 7. RDS 연결 수 (요청 6)

**읽기 전용 조회**: `parking-guardian`은 `db.t4g.micro`(메모리 1024 MiB), PostgreSQL 16.13, **현재 정지(stopped)** 상태다. 정지 중이라 접속해서 `SHOW max_connections`를 볼 수 없다(켜는 것은 AWS 변경이라 하지 않았다). 파라미터 그룹의 값은 `LEAST({DBInstanceClassMemory/9531392}, 5000)`(기본 공식), `superuser_reserved_connections = 3`이다.

- **상한**: 인스턴스 메모리 1024 MiB로 계산하면 1073741824 / 9531392 = **112.6 → 최대 112**. `DBInstanceClassMemory`는 OS·RDS 예약을 뺀 값이라 **실제 값은 112보다 작다. 얼마인지는 이번에 확인하지 못했다 [미확인]**. 그래서 아래는 시나리오(112 / 87 / 60)로 본다. 87은 같은 1 GiB급 인스턴스에서 흔히 알려진 값이지만 **이 인스턴스에서 확인한 값이 아니다**.
- **앱 쪽**: SQLAlchemy 풀 기본값(풀 5 + 오버플로 10)이고 앱에서 바꾸지 않았다(`pool_pre_ping`만 설정). gunicorn worker 1개 × 스레드 8이라 Pod당 동시 연결은 **최대 8**(스레드마다 요청 하나, 15 미만). 코드 확인: `require_device`가 DB를 조회한 뒤 커밋 없이 AI 호출로 넘어가므로 **AI 응답을 기다리는 동안에도 요청이 DB 연결을 잡고 있다**(`pg_stat_activity`로 측정하지는 않았다). 다만 API의 동시 호출 상한(4)이 넘는 요청은 AI에 가지 않고 바로 실패하므로 AI를 기다리며 연결을 잡는 요청은 Pod당 4개 이하다.

| 항목 | Prototype | Phase 1 |
|---|---|---|
| Pod 최대 | 3 | 4 (롤링 surge 포함 5) |
| Pod당 동시 연결(실질 최대 = 스레드) | 4 | 8 |
| 합계 실질 최대 (+ 마이그레이션 Job 1) | 3×4 = 12 | 5×8 + 1 = **41** |
| 설정상 천장 (풀 5 + 오버플로 10 = 15 / Pod) | 45 | 5×15 + 1 = **76** |

| `max_connections` 가정 | 쓸 수 있는 연결 (− 예약 3 − RDS 내부 추정 3) | 실질 최대 41 | 설정상 천장 76 |
|---|---|---|---|
| 112 (상한) | 106 | 여유 | 여유 |
| 87 | 81 | 여유 | 간신히 (5 남음) |
| 60 | 54 | 여유 | **초과** |

결론: 실질 사용(41)은 가정한 어느 값에서도 들어간다. 설정상 천장(76)은 `max_connections`가 80 미만이면 넘을 수 있지만, 스레드가 8이라 실제로 천장까지 쓰는 일은 일어나기 어렵다. **EKS에서 RDS를 켠 직후 `SHOW max_connections;`로 실제 값을 확인하고, S1 부하 중 `SELECT count(*) FROM pg_stat_activity;`로 실측**한다(설계 6-2/6-4). Phase 2에서 Worker와 재전송기가 연결을 더하므로 그때 다시 계산한다. 개선 후보(미구현, 설계 F8): AI 호출 전에 DB 세션을 반환하면 AI 대기 중 연결 점유가 사라진다(`device.py` 수정이라 판정·이벤트 규칙을 건드리지 않는 선에서 별도 승인 필요).

**확인 시점과 관찰 항목 (사용자 지시 2026-10-30)**
- **`max_connections` 는 실제 DB 가동 뒤에 확인한다.** RDS 를 켠 직후(체크리스트 G5) `SHOW max_connections;` 와 `SHOW superuser_reserved_connections;`. 이번 문서의 112 / 87 / 60 은 가정이다.
- **AI 응답 대기 중 API 의 DB 연결 점유는 Phase 1 부하 테스트(S1, S4)의 관찰 항목**이다(사전 등록 README 5.3, 설계 5.3). `pg_stat_activity` 의 `idle in transaction` 수를 AI 호출 시간·`pg_ai_client_in_flight` 와 함께 기록한다. 코드로만 확인했고 측정하지 않았다.
## 8. Terraform plan과 렌더링 결과 (요청 7)

### Terraform plan 검증 (`scripts/check-phase1-plan.py`, 28개 항목 통과)

- 변경 전 코드의 기준 plan: data **No changes**, cluster **31 to add**.
- 변경 후: data **2 to add, 1 to change**, cluster **34 to add**. 같은 체커가 **변경 전 plan에는 10건 실패**하므로 변경 누락을 잡아낸다(테스트를 먼저 쓰고 빨간불을 확인한 뒤 구현).
- plan 파일(`*.tfplan`)과 JSON에는 계정 ID가 있어 커밋하지 않았다.

```
[data] ECR
  ok    새로 생기는 것은 inference 리포지토리와 lifecycle 정책뿐이다 (실제: ['aws_ecr_lifecycle_policy.this["inference"]', 'aws_ecr_repository.this["inference"]'])
  ok    기존 리소스의 변경은 GitHub Actions ECR 푸시 정책 하나뿐이다 (실제: ['aws_iam_role_policy.github_actions_ecr'])
  ok    삭제·교체가 없다 (실제: {})
  ok    리포지토리 이름 parking-guardian/inference (실제: parking-guardian/inference)
  ok    태그 불변(IMMUTABLE): 같은 태그로 덮어쓸 수 없다
  ok    푸시 때 취약점 스캔
  ok    기존 backend 리포지토리는 그대로다
  ok    기존 frontend 리포지토리는 그대로다

[cluster] IAM / Pod Identity / add-on
  ok    cluster 스택은 처음부터 만드는 plan 이라 전부 create 다 (실제 다른 동작: {})
  ok    생성 수 = 기준 31 + 새 3 (실제: 34)
  ok    inference 역할·정책·연결이 있다 (없는 것: [])
  ok    inference 정책의 동작은 ['s3:GetObject', 's3:ListBucket'] 뿐이다 (실제: ['s3:GetObject', 's3:ListBucket'])
  ok    inference 정책에 Deny 가 아닌 Allow 만 있다(권한 추가가 아님을 확인용)
  ok    inference 정책의 대상은 모델 버킷뿐이다 (실제: ['arn:aws:s3:::parking-guardian-models-<acct>', 'arn:aws:s3:::parking-guardian-models-<acct>/v*/*'])
  ok    inference 정책에 사진 버킷이 없다
  ok    Secrets Manager / RDS / KMS / IAM 관련 동작이 없다
  ok    Pod Identity 연결 = parking-guardian/inference (실제: parking-guardian/inference)
  ok    inference 역할에 AWS 관리형 정책 attachment 가 없다
  ok    신뢰 정책 주체는 pods.eks.amazonaws.com (backend 와 같은 방식)
  ok    backend 정책의 동작은 사진 Put/Get 뿐이다 (실제: ['s3:GetObject', 's3:PutObject'])
  ok    backend 정책에서 모델 버킷이 빠졌다 (실제 대상: ['arn:aws:s3:::parking-guardian-photos-<acct>/violations/*'])
  ok    backend 정책의 대상은 사진 버킷뿐이다
  ok    CloudWatch add-on 버전이 고정되어 있다: v6.7.0-eksbuild.1 (실제: v6.7.0-eksbuild.1)
  ok    nodeExporter 를 명시적으로 끈다 (kube-prometheus-stack 이 이미 노드 지표를 낸다)
  ok    kubeStateMetrics 를 명시적으로 끈다 (monitoring 스택에 이미 있다)
  ok    기존 설정(applicationSignals 끔)이 유지된다
  ok    기존 설정(monitorAllServices 끔)이 유지된다
  ok    configuration_values 가 고정 버전의 공개 스키마와 맞는다 (문제: [])

모두 통과
```

### 매니페스트 렌더링 검증 (`scripts/check-phase1-render.py`, 53개 항목 통과)

```
렌더링된 오브젝트 21개: ConfigMap, Deployment, ExternalSecret, HorizontalPodAutoscaler, Ingress, Job, Namespace, PodDisruptionBudget, PodMonitor, Service, ServiceAccount

[inference] 있어야 하는 것
  ok    Deployment inference 가 있다
  ok    Service inference 가 있다
  ok    HorizontalPodAutoscaler inference 가 있다
  ok    PodDisruptionBudget inference 가 있다
  ok    PodMonitor inference 가 있다
  ok    ServiceAccount inference 가 있다

[inference] 권한과 노출 범위
  ok    ServiceAccount inference (backend 와 다르다)
  ok    ServiceAccount 토큰을 마운트하지 않는다 (Kubernetes API 호출 없음)
  ok    Secret 을 참조하지 않는다 (실제: [])
  ok    Secret 볼륨이 없다
  ok    볼륨은 모델용 emptyDir 하나뿐이다 (실제: ['models'])
  ok    envFrom 은 전용 ConfigMap inference-config 하나뿐이다 — backend-config(DB 호스트, 사진 버킷, 비밀 이름)를 통째로 받지 않는다 (실제: ['inference-config-c7hc6mtm6t'])
  ok    backend-config 에서 가져오는 키는 모델 다운로드에 필요한 셋뿐이다 (실제: ['AWS_REGION', 'MODELS_BUCKET', 'MODEL_VERSION'])
  ok    DB·비밀·사진 버킷 환경변수가 없다 (실제 env: ['MODELS_BUCKET', 'MODEL_VERSION', 'AWS_REGION'])
  ok    inference-config 에 DB·비밀 키가 없다 (키: ['INFERENCE_BACKEND', 'INFERENCE_MAX_WAITING', 'METRICS_PORT', 'TORCH_NUM_THREADS'])
  ok    권한 상승 금지, capability 전부 제거
  ok    Service 는 ClusterIP 8000 (클러스터 안에서만)
  ok    ALB 가 이 Service 를 보지 않는다
  ok    Ingress 가 inference 를 노출하지 않는다 (백엔드: ['backend', 'frontend'])

[inference] 자원·Probe·배치 (설계 3.5.1, 3.7)
  ok    요청 1000m / 1.5Gi (추정값, E0 뒤 조정) — 실제 {'cpu': '1000m', 'memory': '1.5Gi'}
  ok    메모리 limit 2Gi, CPU limit 없음 — 실제 {'memory': '2Gi'}
  ok    startupProbe /readyz 5초 × 36회 = 180초 (실제 5초 × 36)
  ok    readinessProbe /readyz (모델 ready 일 때만 Service 에 들어간다)
  ok    livenessProbe /healthz 10초 × 3회 (모델 로딩 포기 시 503 → 재시작)
  ok    포트 http 8000, metrics 9102
  ok    종료 유예 ≥ 45초 (gunicorn graceful_timeout 30초 + 여유) — 실제 45
  ok    분산은 preferred(가중치 100)이고 required 가 아니다 (3노드 공존, 설계 3.7.1)
  ok    AZ 분산은 ScheduleAnyway
  ok    롤링 maxSurge 1 / maxUnavailable 0 (실제 {'maxSurge': 1, 'maxUnavailable': 0})
  ok    replicas 를 적지 않는다 (HPA 가 정한다)
  ok    initContainer fetch-models 가 모델 버킷에서 받는다
  ok    Argo CD sync-wave 2

[inference] HPA / PDB / PodMonitor
  ok    HPA 2 ~ 3 (실제 2 ~ 3)
  ok    HPA 지표는 CPU (시작값. 대기 수 기준 KEDA 는 Phase 2)
  ok    PDB minAvailable 1
  ok    PodMonitor 가 inference 의 metrics 포트를 긁는다

[backend] Phase 1 변경
  ok    모델 다운로드 initContainer 가 없다
  ok    모델 볼륨·마운트가 없다
  ok    요청 200m / 256Mi, 메모리 limit 512Mi (추정값, S0 뒤 조정) — 실제 {'limits': {'memory': '512Mi'}, 'requests': {'cpu': '200m', 'memory': '256Mi'}}
  ok    api 끼리 분산도 preferred 다 (required 가 아니다)
  ok    종료 유예 ≥ 45초 — 실제 45
  ok    ServiceAccount backend (사진 권한만)
  ok    backend-config 의 AI 연결 설정 (없거나 다른 것: [])
  ok    AI_CLIENT_MAX_CONCURRENCY 가 API 스레드 수보다 작다 (모든 스레드가 AI 를 기다리지 않게. 값 자체는 T2 로 정한다)
  ok    HPA 2 ~ 3 — 3노드 실험 환경의 시작 기준이고 운영의 최종 한도가 아니다 (실제 2 ~ 3)
  ok    축소 안정화 120초
  ok    PDB minAvailable 1

[이미지·비밀] 전체
  ok    backend 와 마이그레이션 Job 은 같은 이미지(api 타깃이 올라간 backend 리포지토리)
  ok    inference 이미지는 inference 리포지토리 (inference:unset)
  ok    frontend 이미지 그대로
  ok    레지스트리가 비어 있는 이미지가 없다 (replacements 가 모두 채웠다)
  ok    backend-secrets 를 쓰는 워크로드는 backend 와 마이그레이션 Job 뿐이다 (실제: ['backend', 'backend-migrate'])
  ok    모든 리소스가 parking-guardian Namespace 에 있다

모두 통과
```

### 렌더링에서 확인된 적용 순서 제약

저장소의 **실제 `config.env`에는 아직 `ECR_INFERENCE`가 없다**(ECR이 만들어지기 전). 그대로 `kubectl kustomize`를 실행하면 `fieldPath data.ECR_INFERENCE is missing for replacement source ConfigMap`으로 **실패한다.** 검사 도구는 임시 복사본에만 가짜 값을 넣어 렌더링했고 저장소의 `config.env`는 건드리지 않았다. 즉 config 레포 변경은 **ECR을 만든 뒤 `export-config-env.py`로 `config.env`를 다시 만든 다음에** push해야 한다.

## 9. 예상 비용 (모두 추정)

| 항목 | 변화 | 근거·한계 |
|---|---|---|
| 노드(EC2 3대), EKS 컨트롤 플레인, NAT, ALB, RDS | **변화 없음** | 추가 노드 없음(사용자 결정 v4). 시간당 약 0.52달러(10-28 기준)가 그대로다. 단, 이 숫자를 이번에 다시 확인하지 않았다 |
| ECR 저장 | **월 1달러 미만, 순변화는 0에 가깝다** [추정] | inference 최대 10개 × 압축 약 625 MB ≈ 6.3 GB × 0.10달러 = 0.63달러/월 상한. backend 저장소는 api 이미지(압축 약 84 MB)로 바뀌어 옛 Prototype 이미지(압축 약 660 MB)가 lifecycle로 밀려나며 줄어든다. 단가는 `storage_ecr.tf` 주석의 값을 썼다 |
| 데이터 전송 | 사실상 변화 없음 [추정] | ECR 레이어는 S3 Gateway Endpoint를 타고 ECR API 호출만 NAT를 지난다(`storage_ecr.tf` 주석). 모델 다운로드도 S3 Endpoint. `public.ecr.aws`의 aws-cli 이미지는 NAT를 지나지만 Prototype과 같다 |
| CloudWatch Logs / 지표 | 미측정 | inference의 gunicorn 로그가 늘고 보관은 7일. 사용량은 재구축 뒤 봐야 한다 |
| 세션 비용 | 수 달러 (기존 추정 유지) | 재구축·해체 약 40분이 과금 시간에 포함. RDS는 11-04 자동 재시작 |

**이번 변경은 시간당 운영비를 늘리지 않는다. 추가되는 것은 월 1달러 미만의 저장 비용 추정과 측정 세션 비용뿐이다.** 이미지 크기 감소(api 88%)와 비용 감소를 같은 것으로 읽지 않는다(`local/docker-split.md`).

## 10. 로컬에서 확인한 것과 EKS에서 추가로 확인해야 하는 것

| 주제 | 로컬(Docker Compose, 이 PC)에서 확인함 | EKS에서 추가로 확인해야 함 |
|---|---|---|
| 판정 | API ↔ inference 분리 후 6장 판정이 Prototype과 같다(차이 0/6). 위반·정상 전체 흐름이 Prototype과 같다 | EKS의 두 Pod 사이 HTTP, 실제 ALB를 거친 요청 |
| 장애 격리 | AI 컨테이너 중단·재시작·지연·동시 폭주에도 관리자 API 정상(77건·53건 전부 200), 탐지만 503 | `kubectl scale --replicas=0`, Pod 삭제, drain, 지연 주입. **503까지 걸리는 시간**(로컬은 Docker DNS 때문에 8.3~8.6초; K8s Service는 다를 것으로 예상, 미확인) |
| 이미지 | api 343 MB / inference 2.74 GB / prototype 2.89 GB (압축 전). 의존성 분리 확인 | **Pull 시간**, 스케일 아웃 시작 시간, ECR 저장 |
| 모델 로딩·Probe | Linux gunicorn에서 준비까지 약 12~30초(실행마다 변동), 로딩 실패 시 `/healthz` 503 | Probe 값(180초 startup 등)을 **EKS 로딩 시간**으로 재조정, 재시작 경로, CrashLoopBackOff |
| 메모리·CPU | 최대 RSS 1363 MiB(스레드 1), 스레드 1/2/4 시간(1회 측정, 실행마다 최대 2~3배 변동) | **Request/Limit 확정, torch 스레드 수 결정(E0/E1)**, Pod 안 torch 기본 스레드 수 |
| 배치 | 렌더링된 요청값으로 계산(5절) | **실제 노드 allocatable·기본 예약 재측정**, `kubectl get pods -o wide`(V1), 몰림(V3) |
| RDS | 코드로 계산(7절) | `SHOW max_connections`, S1 중 `pg_stat_activity` |
| IAM / Pod Identity | plan으로 정책 내용 확인 | 실제 자격 증명 발급(`aws sts get-caller-identity` in Pod), inference에서 사진 버킷·Secrets 접근이 **거부되는지**, 노드 역할 IMDS 경로 |
| CloudWatch | 스키마 비교 | 재구축 뒤 `amazon-cloudwatch` 구성 요소 목록과 요청 합 |
| CI | 정적 테스트 8개 + actionlint | 실제 GitHub Actions 실행, ECR 푸시, 태그 갱신 커밋 |

## 11. 적용 순서 (승인 후, 아직 하나도 실행하지 않았다)

**최종 체크리스트와 단계별 사전 점검·기대 결과·중단 조건은 `deploy-checklist.md`.** 순서 요약(게이트마다 사용자 승인):

1. 로컬 재확인 → 2. **[승인] `data` 스택 apply**(ECR) → 3. `config.env` 재생성 + `preflight-deploy.py --stage before-config-push --aws` → 4. **[승인] config 레포 push**(클러스터 없음 확인 뒤) → 5. **[승인] 앱 레포 push**(CI) + `preflight-deploy.py --stage before-rebuild --aws` → 6. **[승인] RDS 시작**, `SHOW max_connections` → 7. **[승인] 재구축**(기록된 순서: cluster → platform → Argo CD → `root.yaml`) → 8. 재구축 직후 검증(CloudWatch, 노드 요청 합, Pod Identity) → 9. EKS 검증과 측정.

**전환 지점은 `argocd/root.yaml` 적용이다.** Argo CD 는 root Application 이 있어야 config 레포를 읽기 시작한다. 그전에는 config 레포를 어떻게 바꿔도 클러스터에 아무 일도 일어나지 않는다. 따라서 "config push 는 클러스터가 없는 동안"이 가장 안전하고, 점검 스크립트(`scripts/preflight-deploy.py`)가 순서 위반을 막는다:
- `before-config-push`: `config.env` 에 `ECR_INFERENCE` 가 있는지, 저장소 그대로 렌더링되는지, (`--aws`) 클러스터가 없는지, ECR `inference` 가 있는지.
- `before-rebuild`: 태그 셋이 `unset` 이 아니고 같은 커밋 해시이며 Prototype 태그(`d354c49`)가 아닌지, (`--aws`) 세 이미지가 ECR 에 있고 **backend 이미지가 api 크기(약 84 MB)인지**(Prototype 약 660 MB 가 아닌지).
오늘의 저장소 상태에서 두 단계는 **둘 다 실패한다**(올바른 동작: `ECR_INFERENCE` 없음, ECR 없음, 태그 `unset`·Prototype 태그).

**이 순서를 어기면 생기는 일**
- ECR 보다 먼저 CI 를 push: inference 잡이 실패하고 `update-config` 가 `needs: build` 때문에 실행되지 않는다.
- `config.env` 에 `ECR_INFERENCE` 없이 config 를 push: `kubectl kustomize` 가 실패하고 Argo CD 동기화가 깨진다(직접 확인).
- `root.yaml` 적용 시점에 태그가 `unset`/Prototype 태그: inference 는 `ImagePullBackOff`, backend 는 Prototype 이미지(HttpInference 없음)에 `INFERENCE_BACKEND=http` 를 받아 모델 없이 뜬다.
- 클러스터와 root 가 이미 있는 상태에서 config 를 push: Argo CD 가 새 구조를 옛 태그로 즉시 동기화한다.
- RDS 는 **2026-11-04 에 자동으로 다시 켜진다.** 그 전에 재구축을 시작하지 않으면 다시 정지해야 한다.

## 12. 주요 위험

| 위험 | 영향 | 상태 |
|---|---|---|
| 적용 순서 위반 (11절) | 동기화 실패, 이미지 불일치 | 순서를 문서화함. 사용자 승인 단계마다 확인 |
| HPA 최대 3 + 3 의 여유가 30m 뿐 | 노드에 묶이지 않는 Pod 의 CPU 요청 합이 80m 를 넘으면 api 또는 inference 하나가 Pending | 민감도 표(5절). 재구축 직후 노드별 요청 합으로 확인(G7) |
| 최대 상태에서 롤링 지연 | surge Pod 자리가 없어 롤링이 HPA 축소까지 기다림(서비스는 유지) | 관찰 항목(README 5.3) |
| 노드 상수가 10-28 값 | 배치 계산이 틀릴 수 있다 | 재구축 직후 재측정 |
| CloudWatch 구성 요소 | 켜지면 배치가 무너진다 | 명시적으로 끔. 재구축 뒤 확인 |
| IMDS 홉 2, RDS 포트 도달 | Pod가 노드 역할·DB 포트에 닿는다 | 해결하지 않음. F7 |
| AI 대기 중 DB 연결 점유 | AI가 느리면 연결이 묶인다 | 동시 호출 상한이 완화. F8(미구현) |
| 모든 임계값이 시작값 | 요청, Probe, 상한이 EKS에서 틀릴 수 있다 | E0/E1/S0/T1~T3로 결정 |
| 로컬 측정 변동 (같은 설정 2~3배) | 시간 값을 근거로 쓸 수 없다 | 3회 반복 규칙 |
| 간헐적 테스트 실패 | 원인 미확정 | 재발 시 공유 상태 재분석 |
| 실제 Pi 미검증 | 장비 쪽 동작은 PC 시뮬레이터로만 확인 | 장비 입수 후 |

## 13. 지금까지 확인된 기능 유지 결과 (요약)

- Prototype 판정 규칙·장비 이벤트 규칙을 담은 코드(`device/`, `services/`, `api/device.py`, `models/`)는 `prototype-v1` 태그와 **완전히 동일**하다.
- 테스트: backend 219개(SQLite; 이 단계의 CI 테스트 8개 포함), 직전 단계에서 PostgreSQL 211개와 device 17개 통과.
- PC 시뮬레이터: 위반 차량 arrived → confirm → violation → refresh → left와 정상 차량 arrived → refresh → left가 Prototype과 같다(`local/step4-simulator.md`).
- **첫 시뮬레이터 실행의 차이**: Phase 1 실행 1에서 sample_c의 **하트비트 17 vs 18, 프레임 185 vs 210**이 Prototype과 달랐다(이벤트·응답·상태 변화는 같았다). 같은 시나리오를 새 DB에서 다시 실행했을 때는 **완전히 일치했다**(18/18, 210/210). 실행 1에서 장비 PC의 평균 탐지 시간이 376 ms(Prototype 133 ms)였고 이것이 원인일 것으로 보지만 **직접 측정으로 확인하지 못했다. 원인은 미확정이다.**
- 코드 변경 중 Prototype 경로에 닿는 것은 `YoloInference.warm_up`의 로딩 실패 시 최대 3회 재시도뿐이다(성공 경로 동일).

## 14. 하지 않은 것 / 승인 대기

- Terraform apply 전부, AWS 리소스 변경 전부 (RDS 시작 포함)
- 클러스터 재구축, EKS 배포, Argo CD 동기화, 매니페스트 실제 적용
- Git 커밋·push (infra, config, 앱 레포의 5단계 변경은 모두 미커밋)
- IMDS 홉 제한 변경, NetworkPolicy, 대시보드 JSON 재구성, `device.py` 수정(F8)
