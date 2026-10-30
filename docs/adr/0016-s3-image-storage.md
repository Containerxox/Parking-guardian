# 0016. 위반 사진은 S3에 저장하고, 브라우저에는 지금처럼 Backend가 내려준다

- 상태: **결정됨** (2026-10-25). Adapter 구현과 테스트 완료(AWS 없이). 실제 S3 에서의 동작은 EKS 배포(Phase 1) 때 확인한다.
- 배경: AWS 구축 Phase 1(현재 구조 그대로 EKS 배포)의 사전 작업. 설계 문서는 `parking-guardian-infra/docs/phase1/architecture.md` 4.6절(비공개).
- 유지하는 결정: 0006(사진을 다시 압축하지 않는다), 0007(저장은 Adapter 뒤에 둔다), 0013(모델 파일은 이미지 밖).

## 문제

위반 사진은 `LocalImageStorage` 가 컨테이너의 디스크(`/data/storage`, Compose volume)에 저장한다. EKS 로 가면 Pod 는 언제든 다른 노드, 다른 AZ 에 다시 뜬다. 디스크를 따라다니게 하려면 EBS 볼륨(PVC)을 붙여야 하는데 그러면 Pod 가 그 볼륨의 AZ 에 묶이고, EBS CSI 드라이버와 스냅샷 백업이 생긴다. "Terraform 으로 지우고 다시 만든다"는 Phase 1 원칙과 가장 안 맞는 부분이다.

사진을 어디에 두고, 브라우저가 어떤 경로로 받게 할지 정해야 했다.

## 검토한 대안

| 질문 | 대안 | 버린 이유 |
|---|---|---|
| 어디에 저장 | EBS PVC | Pod 가 AZ 에 고정. CSI 드라이버와 백업을 내가 운영. 코드 변경이 없다는 것만 장점 |
| | EFS | 다중 AZ 공유는 되지만 GB 당 비용이 S3 의 10배 이상이고 CSI 가 또 필요. 사진은 객체 저장소가 정석 |
| 브라우저가 받는 길 | Presigned GET URL 로 S3 에서 직접 | Backend 부하가 줄지만 S3 CORS 설정, 로컬(LocalStack)에서 "접속 주소 ≠ 공개 주소" 문제가 따라온다. 관리자가 사진을 보는 빈도가 낮아 지금은 이득이 작다 |
| 로컬 개발 | 로컬도 S3(LocalStack) 로 통일 | `python wsgi.py` 가 Docker 를 요구하게 된다. 테스트도 LocalStack 없이 돌아야 한다 |

## 결정

| 질문 | 결정 |
|---|---|
| 저장소 | `STORAGE_BACKEND=s3` 면 S3 버킷(`S3_BUCKET`, 선택 `S3_PREFIX`). 기본은 `local`(지금 그대로) |
| Adapter | `backend/app/adapters/storage.py` 의 `S3ImageStorage`. `save`(PutObject, 바이트 그대로, ContentType 은 매직 바이트로), `read`(GetObject), `exists`(HeadObject). 없는 키는 `FileNotFoundError` 로 로컬 Adapter 와 같게 |
| 브라우저 경로 | 변경 없음. `/api/v1/violations/{id}/image-url` → 서명 토큰 → `/api/v1/images/<token>` 에서 Backend 가 S3 를 읽어 내려준다. Presigned URL 은 Phase 2 이후 비교 항목 |
| 자격 증명 | 코드·설정에 키 없음. boto3 기본 체인(EKS Pod Identity, 로컬은 `aws sso login`) |
| 권한 | 버킷의 `violations/*` 에 Put/Get/Head 만. 삭제 권한 없음(앱이 삭제하지 않음) |
| 보존 기간 | S3 Lifecycle 규칙 없음. `IMAGE_RETENTION_DAYS` 는 여전히 만료일 계산만 한다. 삭제·EXPIRED 는 뒤에 함께 결정 |
| 기존 사진 | Compose volume 의 데모 사진은 옮기지 않는다 |
| 테스트 | 가짜 S3 클라이언트를 주입해 AWS 없이 돈다. `moto` 는 의존성이 하나 늘어 쓰지 않았다 |

## 이유

- **Pod 를 상태 없이 만든다.** 사진이 Pod 밖(S3)에 있으면 Pod 는 어디서 다시 떠도 된다. 노드 장애 복구 시간을 측정하는 Phase 1 의 목적과도 맞는다.
- **변경이 작다.** 저장 Key 는 처음부터 S3 규칙이었고(`violations/company-{id}/parking-lot-{id}/YYYY/MM/DD/violation-{id}.jpg`), 호출부는 `adapters.storage()` 의 메서드 셋만 쓴다. Adapter 한 클래스와 `build_storage()` 분기, 설정값 4개가 전부다. 호출부(`api/device.py`, `api/violations.py`)는 바뀌지 않았다.
- **Backend 경유 읽기를 유지하면** Frontend 변경이 0 이고, 서명 토큰과 Tenant 확인이 지금처럼 Backend 에 남는다.
- **Phase 3(SageMaker)** 은 사진이 S3 에 있어야 비동기 추론(S3 입력)을 쓸 수 있다. 실시간 엔드포인트의 본문 상한(6 MB)보다 큰 원본 사진을 재압축 없이 보낼 길은 그것뿐이다.

## 결과

- 얻은 것: EKS 에서 PVC·EBS CSI 가 필요 없다. 로컬과 테스트는 그대로 디스크를 쓴다. 설정 하나로 전환.
- 감수한 것: 사진을 볼 때마다 Backend 가 S3 에서 읽어 전달한다(관리자가 보는 빈도가 낮아 무시할 수준). boto3 가 `requirements.txt` 에 추가됐다(이미지 +수십 MB). `local` 백엔드는 boto3 를 import 하지 않는다.
- 나중에 바꿀 것: Presigned URL(성능 비교 항목), Lifecycle 과 `EXPIRED` 처리, 사진 주소에 `X-Forwarded-Proto` 를 반영하는 ProxyFix 는 이번에 함께 넣었다(ADR 0013 에 예고된 항목).

## 근거

- 코드: `backend/app/adapters/storage.py`(`S3ImageStorage`, `build_storage`), `backend/app/config.py`(`STORAGE_BACKEND`, `S3_BUCKET`, `S3_PREFIX`, `AWS_REGION`), `backend/requirements.txt`(boto3).
- 테스트: `backend/tests/test_storage_s3.py` 4개. 저장→읽기 바이트 동일, 없는 키 → `FileNotFoundError`, 설정 분기, 업로드부터 `/images/<token>` 응답까지 S3 Adapter 경유. SQLite·PostgreSQL 모두 통과(2026-10-25, 전체 46개).
- 확인하지 못한 것: 실제 S3 와 Pod Identity 에서의 동작, botocore 가 HEAD 404 를 어떤 코드로 돌려주는지(`404`, `NotFound`, `NoSuchKey` 세 가지를 모두 "없음"으로 처리해 두었다), S3 객체와 전송 바이트의 SHA-256 비교(EKS 배포 때 스크립트로).

## 한 줄 설명

사진 저장을 S3 Adapter 로 바꿔 Pod 를 상태 없이 만들었고, 브라우저가 받는 경로는 그대로 두어 Frontend 와 권한 검사를 건드리지 않았습니다.
