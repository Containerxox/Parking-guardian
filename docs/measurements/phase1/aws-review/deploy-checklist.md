# Phase 1 EKS 배포 최종 체크리스트와 적용 순서 (2026-10-30 작성, 아직 하나도 실행하지 않았다)

> **현재 승인 범위: 코드·설정 파일 커밋까지.** Terraform apply, AWS 리소스 생성·수정(RDS 시작 포함), Git push, EKS 배포, Argo CD 동기화는 **모두 아래 게이트마다 따로 승인**을 받는다. 승인 표시가 없는 항목은 읽기 전용이거나 로컬 작업이다.
> 값에는 `<…>` 자리표시자를 썼다(계정 ID, 도메인, 버킷 이름 등은 저장소에 적지 않는다).

## 시작 상태 (2026-10-30)

| 항목 | 상태 |
|---|---|
| EKS 클러스터, platform, dns-record | 없음 (10-28 해체) |
| RDS | **정지**. 11-04 에 자동으로 다시 켜진다 |
| ECR | `backend`, `frontend` 만 있다. **`inference` 없음** |
| config 레포 `main` | 옛 구조(Prototype). 태그 `d354c49` |
| 세 레포의 5단계 변경 | 로컬 브랜치에 커밋됨, **push 안 함** (app: `develop`, infra·config: `phase1/eks-prep`) |
| 점검 스크립트의 오늘 결과 | `preflight-deploy.py` 두 단계 모두 **실패**(올바른 동작: ECR 없음, `ECR_INFERENCE` 없음, 태그 `unset`·Prototype 태그) |

## 순서 한눈에

```
G0 로컬 재확인 ─▶ G1 [승인] data apply(ECR) ─▶ G2 config.env 재생성·점검 ─▶ G3 [승인] config push
   ─▶ G4 [승인] 앱 push(CI) + 점검 ─▶ G5 [승인] RDS 시작 ─▶ G6 [승인] 재구축(cluster→platform→Argo CD→root.yaml)
   ─▶ G7 재구축 직후 검증 ─▶ G8 기능·연결 수 확인 ─▶ G9 EKS 측정 ─▶ G10 해체
```

**잘못된 전환을 막는 두 장치**
1. **전환 지점은 `argocd/root.yaml` 적용이다.** root Application 이 있어야 Argo CD 가 config 레포를 읽는다. 그전에는 config 레포를 어떻게 바꿔도 클러스터에는 아무 일도 없다.
2. `scripts/preflight-deploy.py`(config 레포): `--stage before-config-push`(G2), `--stage before-rebuild`(G4 끝과 **G6 의 root.yaml 적용 직전에 한 번 더**). 종료 코드 1 이면 다음으로 가지 않는다.

## G0. 로컬 재확인 (승인 불필요)

| 확인 | 명령 | 기대 |
|---|---|---|
| 앱 테스트 | `cd capston/backend && python -m pytest tests -q` | 219 passed (SQLite). PostgreSQL 은 `docker compose up -d db` 뒤 `TEST_DATABASE_URL` 로 한 번 더 |
| device | `python -m pytest device -q` | 17 passed |
| 매니페스트 렌더링 | `cd parking-guardian-config && python scripts/check-phase1-render.py` | 53개 항목 통과 |
| 배치 계산 | `python scripts/check-placement.py` | "설계가 기대하는 결론과 계산이 같다" |
| 점검 스크립트 테스트 | `python scripts/test_preflight_deploy.py` | 8개 통과 |
| Terraform 변경 | 아래 G1·G6 의 plan 뒤 `python scripts/check-phase1-plan.py …` | 28개 항목 통과 |
| 민감정보 | 계정 ID·도메인·ARN·키 검색 | 0건 |

## G1. [승인] infra `data` 스택 apply — ECR `inference` 생성

```
export AWS_PROFILE=parking-guardian
terraform -chdir=terraform/stacks/data plan -input=false -out=<저장소 밖>/data.tfplan
terraform -chdir=terraform/stacks/data show -json <저장소 밖>/data.tfplan > <저장소 밖>/data.plan.json
```
- **기대: `2 to add, 1 to change, 0 to destroy`.** 추가는 `aws_ecr_repository.this["inference"]`, `aws_ecr_lifecycle_policy.this["inference"]`, 변경은 `aws_iam_role_policy.github_actions_ecr` 하나.
- **중단 조건: destroy 나 replace 가 하나라도 있으면 apply 하지 않는다**(RDS·S3·Route 53 포함).
- 승인 후 `terraform -chdir=… apply <plan 파일>` (plan 과 apply 는 별도 호출).
- 확인: `aws ecr describe-repositories --repository-names parking-guardian/inference`, `terraform -chdir=… output ecr_repository_urls` 에 `inference` 키.

## G2. `config.env` 재생성과 점검 (로컬, 읽기 전용 AWS)

```
cd parking-guardian-config
AWS_PROFILE=parking-guardian python scripts/export-config-env.py --model-version v1
git diff apps/parking-guardian/config.env        # 기대: ECR_INFERENCE 한 줄 추가뿐
AWS_PROFILE=parking-guardian python scripts/preflight-deploy.py --stage before-config-push --aws
```
- **기대: `config.env` 변경은 `ECR_INFERENCE` 한 줄 추가뿐.** 다른 줄이 바뀌면 인프라 출력이 달라진 것이니 멈추고 확인한다.
- **점검 통과 조건**: `ECR_INFERENCE` 있음, 저장소 그대로 `kubectl kustomize` 통과, **클러스터 없음**, ECR `inference` 있음.

## G3. [승인] config 레포 push (클러스터가 없는 동안)

- 대상: 새 구조 + `config.env`(ECR_INFERENCE 포함). 태그는 backend·frontend `d354c49`(Prototype), inference `"unset"` 그대로 둔다 — CI 의 태그 갱신 정규식이 `inference` 항목이 있어야 동작한다.
- `phase1/eks-prep` 브랜치를 `main` 으로 반영한다(예: `git push origin phase1/eks-prep:main`). 승인 때 방식을 확정한다.
- **이 상태(Prototype 태그 + http 설정)는 일관되지 않다.** 안전한 이유는 오직 클러스터에 root Application 이 없기 때문이다. G4 가 끝나 태그가 바뀌기 전에 root.yaml 을 적용하지 않는다.

## G4. [승인] 앱 레포 push → CI → 태그 갱신

- `git push origin develop`. GitHub Actions 에서 확인:
  - `backend`(`--target api`), `frontend`, `inference`(`--target inference`) **세 잡이 모두 성공**.
  - `update config repo image tags` 잡이 config 레포 `main` 에 `deploy: backend·frontend·inference 이미지 태그 <해시>` 커밋을 올린다.
- 그 커밋을 받은 뒤: `git -C parking-guardian-config pull` 후
```
AWS_PROFILE=parking-guardian python scripts/preflight-deploy.py --stage before-rebuild --aws
```
- **점검 통과 조건**: 세 태그가 `unset` 이 아니고 같은 해시이며 `d354c49` 가 아니다. 세 이미지가 ECR 에 있다. **backend 이미지가 300 MB 미만**(api 약 84 MB. 660 MB 근처면 Prototype 이미지가 올라간 것이니 CI 의 `target` 을 확인한다).
- **중단 조건**: 어느 잡이든 실패, 태그가 어긋남, backend 크기가 크다 → 재구축으로 가지 않는다.

## G5. [승인] RDS 시작 (AWS 상태 변경)

```
aws rds start-db-instance --db-instance-identifier parking-guardian      # 약 5분
aws rds describe-db-instances --db-instance-identifier parking-guardian --query 'DBInstances[0].DBInstanceStatus'   # available
```
- 11-04 자동 재시작 전에 한다. 이번 세션이 끝나면 다시 정지한다(G10).
- `max_connections` 는 RDS 가 사설 서브넷이라 PC 에서 못 읽는다. **G8 에서 backend Pod 로 확인한다.**

## G6. [승인] 재구축 (기록된 순서)

1. `cluster` 스택: `plan -out` → `show -json` → **`python scripts/check-phase1-plan.py --data … --cluster … --addon-schema …` 통과** → 기대 `34 to add, 0 to change, 0 to destroy`(기존 31 + 신규 3) → 승인 → `apply <plan>` (약 20분, 노드 3대; 필요하면 `eks_public_access_cidrs` 의 PC IP 확인).
2. `aws eks update-kubeconfig --name parking-guardian --region ap-northeast-2`
3. `platform` 스택: `helm repo update` → plan → apply.
4. Argo CD 접속(`kubectl -n argocd port-forward svc/argocd-server 8080:80`), 초기 관리자 비밀번호 처리(미완료 항목).
5. **`root.yaml` 적용 직전에 `preflight-deploy.py --stage before-rebuild --aws` 를 다시 실행한다.** 통과해야 `kubectl apply -f argocd/root.yaml`.
6. 앱이 스스로 동기화된다: `platform`, `parking-guardian`, `monitoring`, `monitoring-config`.
7. `dns-record` 스택 apply.

## G7. 재구축 직후 검증 (클러스터 배포 승인 범위 안, 읽기 위주)

**7-1. CloudWatch 구성 요소 (P1 검증, 이유를 모르는 채 결과에 기대지 않는다)**
```
kubectl -n amazon-cloudwatch get ds,deploy,pods -o wide
kubectl -n amazon-cloudwatch get pods -o custom-columns=NAME:.metadata.name,CPU:.spec.containers[*].resources.requests.cpu,MEM:.spec.containers[*].resources.requests.memory
aws eks describe-addon --cluster-name parking-guardian --addon-name amazon-cloudwatch-observability --query 'addon.{version:addonVersion,config:configurationValues}'
```
기대: 에이전트·Fluent Bit(DaemonSet)과 매니저 1개뿐, `node-exporter`/`kube-state-metrics` 이름의 워크로드 **없음**, 버전 `v6.7.0-eksbuild.1`, CPU 요청 합 = 노드마다 300m + 100m. **다르면 멈추고 보고**(배치 계산 상수가 틀린다).

**7-2. 노드 자원과 배치 (HPA 최대 3 + 3 은 여유 30m 이라 이 확인이 핵심)**
```
kubectl describe nodes | grep -A9 "Allocated resources"      # 노드별 CPU/메모리 요청 합
kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory
kubectl -n parking-guardian get pods -o wide                  # V1: inference·api 가 어느 노드에
```
기대: allocatable 약 1930m / 7.2 GiB, 노드당 기본 예약 약 650m(`check-placement.py` 상수와 비교). **노드에 묶이지 않는 Pod(frontend 외 ALB 컨트롤러·ESO·Argo CD·모니터링)의 CPU 요청 합이 80m 이하인지** 계산한다. 80m 를 넘으면 api 3 + inference 3 이 들어가지 않으므로 HPA 최대를 다시 정한다(사용자 결정).

**7-3. Pod Identity 분리가 실제로 적용되는가 (사용자 요청)**
네 서비스어카운트마다 임시 Pod 로 확인한다(Pod 는 끝나면 삭제). 허용과 거부를 **구분할 수 있는** 호출만 쓴다. S3 는 `ListBucket` 이 없으면 없는 키도 403 이라 `get-object` 로는 권한 유무를 가릴 수 없다.
```
NS=parking-guardian ; IMG=public.ecr.aws/aws-cli/aws-cli:2.38.6
run() { kubectl -n $NS run iam-$1 --rm -i --restart=Never --image=$IMG \
        --overrides="{\"spec\":{\"serviceAccountName\":\"$1\",\"automountServiceAccountToken\":false}}" --command -- "${@:2}"; }

run inference aws sts get-caller-identity                               # Arn 에 assumed-role/parking-guardian-inference
run backend   aws sts get-caller-identity                               # Arn 에 assumed-role/parking-guardian-backend
run inference aws s3 ls s3://<models bucket>/v1/                        # 기대: 목록이 나온다 (허용)
run backend   aws s3 ls s3://<models bucket>/v1/                        # 기대: AccessDenied (backend 에서 모델 권한 제거됨)
run inference aws secretsmanager list-secrets                           # 기대: AccessDenied
run backend   aws secretsmanager list-secrets                           # 기대: AccessDenied (비밀은 ESO 가 K8s Secret 으로 전달)
run inference aws rds describe-db-instances                             # 기대: AccessDenied
run inference aws s3api put-object --bucket <photos bucket> --key violations/_inference-denied-check   # 기대: AccessDenied
```
- 마지막 호출이 **성공하면 이는 권한 분리 실패라는 발견**이다: 만들어진 빈 객체는 관리자 프로필로 지우고 보고한다.
- backend 가 사진을 **쓸 수 있는지**는 쓰기 호출로 확인하지 않고(삭제 권한이 없는 역할로 객체를 남기지 않는다) G8 의 위반 생성(사진 저장) 성공으로 확인한다. 정적 확인: `aws iam simulate-principal-policy --policy-source-arn <backend role arn> --action-names s3:PutObject --resource-arns <photos bucket arn>/violations/x`.
- inference Pod 가 모델을 받은 것은 initContainer 로그 `fetch-models end` 로도 확인된다.

**7-4. 미해결 위험의 증거 기록 (해결하지 않는다)**
```
kubectl -n $NS run net-inference --rm -i --restart=Never --image=public.ecr.aws/amazonlinux/amazonlinux:2023 \
  --overrides='{"spec":{"serviceAccountName":"inference","automountServiceAccountToken":false}}' --command -- bash -c '
  T=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60");
  echo "IMDS token bytes: ${#T}";                                                   # >0 이면 도달 가능(홉 제한 2)
  [ -n "$T" ] && curl -s -m 3 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/iam/security-credentials/ ; echo;
  (echo > /dev/tcp/<rds host>/5432) 2>/dev/null && echo "RDS 5432 reachable" || echo "RDS 5432 not reachable"'
```
- 기대(= 위험이 실제로 존재): IMDS 토큰이 나오고 노드 역할 **이름**이 보이며(자격 증명 값은 출력하지 않는다), RDS 5432 에 TCP 가 닿는다. 결과는 `F7` 의 근거로 기록만 하고 이번 계획에서 고치지 않는다.

## G8. 기능과 연결 수 확인

| 확인 | 방법 | 기대 |
|---|---|---|
| API / AI 상태 | `curl https://<host>/api/v1/readyz`, `kubectl -n parking-guardian get pods` | 200 (`inference: remote`), inference Ready |
| 탐지 | `send_detection.py sample_b ×2` (키: `flask device-key`) | 200 → 201, 위반 1건, 알림 |
| 정상 차량 | `sample_c` | permitted, 위반 없음 |
| 사진 저장(backend 쓰기 권한) | 위 위반의 사진 URL 조회 | 사진이 열린다 |
| **RDS 연결 수 (사용자 요청)** | `kubectl -n parking-guardian exec deploy/backend -c backend -- python -c "from app import create_app; from app.extensions import db; from sqlalchemy import text; app=create_app(); app.app_context().push(); print(db.session.execute(text('SHOW max_connections')).scalar(), db.session.execute(text('SHOW superuser_reserved_connections')).scalar())"` | `max_connections` 값을 7절 시나리오(112/87/60)와 비교 |
| 마이그레이션 | `kubectl -n parking-guardian logs job/backend-migrate` | `migrate end` |

## G9. EKS 검증과 측정 (설계 6-3 ~ 6-5)

V1(배치), **6-3b 장애 격리**(`kubectl scale deploy/inference --replicas=0`, Pod 삭제, drain, 지연 주입, 동시 요청), E0 ~ E3, T1 ~ T3, S0 ~ S5. 관찰 항목(사전 등록 README 5.3): **AI 대기 중 DB 연결 점유**(`pg_stat_activity` 의 `idle in transaction`), 최대 상태의 롤링 지연, 탐지 503 이 나오기까지의 시간(로컬 Docker 는 8.3~8.6초, Service 는 다를 것으로 예상), CloudWatch 구성 요소. 모든 시작값(요청, Probe, `AI_CLIENT_*`, `TORCH_NUM_THREADS`, HPA 최대)은 여기서 정한다.

## G10. 해체 (기록된 순서 + Phase 1 에서 달라지는 점)

dns-record destroy → Argo 앱 `root` → `parking-guardian` (ALB 0 확인) → `monitoring-config`, `monitoring` → **`kubectl -n monitoring delete pvc --all`** → `platform` 앱 → platform destroy → cluster destroy(백그라운드, 중단 금지) → **RDS stop**. ECR 이미지는 남긴다(저장 비용 월 1달러 미만 추정). inference 는 PVC 가 없어 추가 정리가 필요 없다.

## 중단 조건 모음

| 상황 | 하는 일 |
|---|---|
| plan 에 destroy/replace | apply 하지 않는다 |
| `preflight-deploy.py` 실패 | 다음 게이트로 가지 않는다 |
| CI 잡 실패, 태그 불일치, backend 이미지가 Prototype 크기 | 재구축하지 않는다 |
| CloudWatch 에 exporter 가 있음 | 멈추고 보고 (배치 상수 오류) |
| 노드 요청 합이 상수와 다름 / 묶이지 않는 Pod 가 80m 초과 | HPA 최대를 사용자와 다시 정한다 |
| inference 가 사진 버킷·Secrets·RDS API 에 성공 | 권한 분리 실패로 보고 |
| RDS 11-04 가 가까움 | 재구축을 서두르거나 다시 정지 |

## 승인이 필요한 게이트

| 게이트 | 무엇을 | 상태 |
|---|---|---|
| G1 | `data` apply | 대기 |
| G3 | config 레포 push | 대기 |
| G4 | 앱 레포 push (CI 실행) | 대기 |
| G5 | RDS 시작 | 대기 |
| G6 | `cluster`/`platform` apply, Argo CD 동기화 | 대기 |
| G7 ~ G9 | 임시 Pod 생성, 부하 측정 | G6 승인 범위 안에서 진행, 부하 측정은 별도 확인 |
| G10 | 해체 | 별도 확인 |

현재 승인된 것: **코드·설정 파일 커밋까지**.
