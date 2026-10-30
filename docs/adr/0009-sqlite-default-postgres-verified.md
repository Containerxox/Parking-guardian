# 0009. 로컬 기본은 SQLite, 검증은 PostgreSQL에서도 한다

- 상태: 결정됨 (2026-10-02)

## 문제

운영 DB는 PostgreSQL(RDS)로 정했다. 그런데 개발 초기에 이 PC에는 Docker 데몬이 꺼져 있었고 PostgreSQL도 설치되어 있지 않았다. 앱과 DB를 빨리 바꿔 로컬에서 돌려 보는 것이 우선이었다.

## 결정

- 로컬 기본 DB는 SQLite 파일로 둔다. 설치 없이 바로 실행된다.
- `DATABASE_URL` 환경변수 하나로 PostgreSQL로 바꾼다.
- 모델과 제약은 SQLAlchemy로 작성해 양쪽에서 동작하게 한다.
- **DB 제약, 잠금, 마이그레이션을 건드리면 PostgreSQL에서도 테스트를 돌린다.** `TEST_DATABASE_URL` 을 주면 같은 테스트가 PostgreSQL에서 돈다.

## 실제로 있었던 일

PostgreSQL에서 처음 테스트를 돌리자 SQLite에서 모두 통과하던 테스트가 실패했다.

```text
FOR UPDATE cannot be applied to the nullable side of an outer join
```

장비 업로드 처리에서 같은 장비의 요청이 겹치지 않게 장비 row를 잠그는데, 장비 모델이 주차장과 회사를 outer join으로 함께 읽고 있었다. PostgreSQL은 이 조합을 거부한다. SQLite는 `FOR UPDATE` 를 아예 무시하기 때문에 드러나지 않았다.

잠금 대상을 장비 Table로 명시해 고쳤다. 그대로 배포했다면 **모든 장비의 사진 업로드가 실패**했을 것이다.

그 밖에 알게 된 것:

- PostgreSQL은 `pg_` 로 시작하는 사용자 이름을 금지한다. 처음 정한 이름 때문에 컨테이너가 뜨지 않았다.
- 이 PC에서 `localhost` 로 접속하면 연결 한 번에 1분 넘게 걸렸다. IPv6 주소를 먼저 시도하는데 컨테이너 포트는 IPv4에만 열려 있었다. `127.0.0.1` 로 쓰면 0.04초다.

## 확인한 것

| 항목 | 결과 |
|---|---|
| 테스트 31개, SQLite | 통과 |
| 테스트 31개, PostgreSQL 16 | 통과 |
| 마이그레이션 2개를 PostgreSQL에 적용 | 정상. 모델과 실제 스키마 사이에 차이 없음 |
| 복합 FK, 조건부 UNIQUE Index 생성 | PostgreSQL에서 확인 |
| 같은 장비로 동시에 4건 업로드 | 위반 1건만 생성 |

## 결과

- 얻은 것: 로컬 실행이 가볍다. 운영 DB에서만 터지는 문제를 배포 전에 잡았다.
- 감수한 것: 두 DB의 차이를 계속 의식해야 한다. SQLite에서 통과했다고 안심할 수 없다.
- 이후: CI에서 PostgreSQL 컨테이너를 띄워 테스트를 돌리면 사람이 기억하지 않아도 된다. Docker Compose로 전체를 띄우는 단계부터는 PostgreSQL이 기본이 된다.

## 근거

- 수정: `backend/app/services/detections.py` 의 `with_for_update(of=Machine)`
- 환경: `docker-compose.yml`, `docker/postgres/init/`
- 테스트 설정: `backend/tests/conftest.py`

## 한 줄 설명

로컬은 가벼운 SQLite로 두되 같은 테스트를 PostgreSQL에서도 돌리게 했고, 그 과정에서 SQLite가 무시하던 행 잠금 구문 때문에 장비 업로드가 전부 실패할 뻔한 버그를 배포 전에 찾았습니다.
