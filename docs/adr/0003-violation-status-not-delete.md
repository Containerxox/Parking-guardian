# 0003. 처리 완료는 삭제가 아니라 상태 변경과 이력이다

- 상태: 결정됨 (2026-10-01)

## 문제

기존 서비스에서 관리자가 "신고 완료"를 누르면 위반 row가 DB에서 삭제됐다. 누가 언제 처리했는지 남지 않았고, 실수로 누르면 되돌릴 수 없었다. 사진 파일은 지워지지 않고 디스크에 남았다.

## 결정

위반은 삭제하지 않는다. 상태로 관리하고 모든 변화를 이력에 남긴다.

```text
DETECTED ──► NOTIFIED ──► IN_PROGRESS ──► RESOLVED
    └──────────────────────► IN_PROGRESS
    └──────────────────────────────────────► RESOLVED   (NOTIFIED 에서도 가능)
RESOLVED ──► IN_PROGRESS   (복구. 서비스 운영자만)
```

- `violations` 는 현재 상태를, `violation_history` 는 시간순 처리 과정을 담는다. 이력은 추가만 한다.
- 알림이 늦거나 실패해도 관리자가 바로 처리할 수 있게 `DETECTED` 에서 곧장 다음 상태로 갈 수 있다.
- 처리 완료를 되돌리는 것은 서비스 운영자만 할 수 있고 사유와 함께 이력에 남는다.
- 업무 Table은 모두 `ON DELETE RESTRICT` 로 두고 비활성화는 `status` 로 한다. 기존의 연쇄 삭제를 없앴다.

## 동시 처리

두 관리자가 같은 위반에 동시에 "처리 완료"를 누를 수 있다. 상태 변경은 조건부 UPDATE로 한다.

```sql
UPDATE violations SET status = :new WHERE id = :id AND status = :읽은_상태
```

바뀐 row가 1건이 아니면 다른 사람이 먼저 바꾼 것이므로 409를 돌려준다. 상태 변경과 이력 기록은 한 트랜잭션에서 한다. 모든 전이는 `violation_service` 한 곳을 거친다.

## 결과

- 얻은 것: 누가 언제 무엇을 했는지 남는다. 잘못된 처리를 되돌릴 수 있다. 통계를 낼 수 있다.
- 감수한 것: 데이터가 계속 쌓인다. 사진은 개인정보이므로 사건 기록과 보관 기간을 분리해 따로 삭제한다(이후 S3 Lifecycle).

## 근거

- 상태 전이: `backend/app/services/violations.py`
- 테스트: `backend/tests/test_violation_flow.py` 의 처리 흐름 테스트. 처리 완료 후 row가 남는지, 두 번째 처리 완료가 409인지, 이력 순서가 맞는지 검증한다
- 실제 확인: 처리 완료 후 "DETECTED, NOTIFIED, ACKNOWLEDGED, RESOLVED" 네 줄의 이력이 시간순으로 조회됐다

## 한 줄 설명

처리 완료를 삭제에서 상태 변경으로 바꾸고, 조건부 UPDATE로 동시 처리를 막으면서 모든 변화를 이력 Table에 남겼습니다.
