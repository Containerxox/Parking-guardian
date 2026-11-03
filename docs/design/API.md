# Parking Guardian API 명세 (v1)

Backend와 Frontend가 공유하는 계약이다. 구현이 바뀌면 이 문서를 먼저 고친다.

## 공통 규칙

- Base URL: `http://localhost:5000/api/v1` (로컬)
- 인증: `Authorization: Bearer <access_token>`. 장비 API만 `X-Device-Key` 헤더를 쓴다.
- Refresh Token은 httpOnly 쿠키 `pg_refresh` 로 전달된다. 인증 관련 요청은 `credentials: "include"` 가 필요하다.
- 시간은 모두 UTC ISO 8601 문자열이다. 예: `2026-10-01T12:34:56Z`
- 목록 응답: `{"items": [...], "total": 12, "page": 1, "size": 20}`. Pagination이 없는 목록은 `items`, `total` 만 준다.
- 오류 응답: `{"error": {"code": "forbidden", "message": "접근 권한이 없습니다."}}`

| HTTP | code | 의미 |
|---|---|---|
| 400 | `validation_error` | 입력값 오류 |
| 401 | `unauthorized`, `invalid_credentials`, `token_expired` | 인증 실패. `token_expired` 면 `/auth/refresh` 후 재시도 |
| 403 | `forbidden` | Role 부족, 또는 다른 회사 ID를 명시한 접근 |
| 404 | `not_found` | 없음, 또는 다른 회사의 리소스 (존재 여부를 노출하지 않음) |
| 409 | `conflict`, `invalid_transition` | 중복, 또는 허용되지 않는 상태 전이 |
| 503 | `inference_unavailable` | AI 추론 실패 |

## Role과 Tenant 규칙

- Role: `SUPER_ADMIN`, `COMPANY_ADMIN`
- `COMPANY_ADMIN` 의 회사는 항상 Token에서 결정된다. 요청에 `company_id` 를 넣어도 자기 회사가 아니면 403이다.
- `SUPER_ADMIN` 은 목록 API에 `?company_id=` 로 회사를 좁힐 수 있다. 생략하면 전체다.
- 조회와 변경을 나눈다. `COMPANY_ADMIN` 은 자기 회사의 주차장, 구역, 장비, 담당 관리자를 모두 **조회**할 수 있다. 이 구조를 **변경**하는 것(주차장, 구역, 장비 등록, 키 재발급, 담당 배정)은 `SUPER_ADMIN` 만 한다. `COMPANY_ADMIN` 이 변경 API를 호출하면 403이다.
- 위반 확인과 처리 완료는 같은 회사의 `COMPANY_ADMIN` 누구나 할 수 있다. 화면에서는 `COMPANY_ADMIN` 에게만 "확인"·"처리 완료" 버튼을 보여 준다(2026-10-25). `SUPER_ADMIN` 은 조회와 "상태 복구"만 한다(API 자체는 로그인하면 호출할 수 있다).

## 객체

```jsonc
// Admin
{"id": 3, "email": "kim@a.com", "name": "김관리자", "role": "COMPANY_ADMIN", "status": "ACTIVE",
 "company_id": 1, "company_name": "A회사", "parking_lot_ids": [1, 2], "created_at": "..."}

// Company
{"id": 1, "name": "A회사", "status": "ACTIVE", "created_at": "...",
 "admin_count": 3, "parking_lot_count": 3, "open_violation_count": 2}

// ParkingLot
{"id": 1, "company_id": 1, "company_name": "A회사", "name": "강남 주차장", "address": "서울 강남구 ...",
 "status": "ACTIVE", "zone_count": 2, "machine_count": 2, "open_violation_count": 1, "admin_ids": [3, 4]}

// ParkingZone
{"id": 1, "parking_lot_id": 1, "zone_name": "A-01", "location_description": "지하 1층 입구 옆", "machine_id": 5}

// Machine
{"id": 5, "company_id": 1, "company_name": "A회사", "parking_lot_id": 1, "parking_lot_name": "강남 주차장", "serial_number": "RPI-0001",
 "name": "Camera-01", "status": "ACTIVE", "installed_at": "...", "last_heartbeat_at": "...", "online": true}

// Violation
{"id": 10, "company_id": 1, "company_name": "A회사", "parking_lot_id": 1, "parking_lot_name": "강남 주차장",
 "parking_zone_id": 1, "zone_name": "A-01", "machine_id": 5, "machine_name": "Camera-01",
 "detected_at": "...", "last_seen_at": "...", "detection_count": 37, "vehicle_present": true,
 "ended_at": null, "end_reason": null, "status": "NOTIFIED", "image_status": "UPLOADED", "has_image": true,
 "resolved_at": null, "resolved_by": null, "resolved_by_name": null, "resolution_note": null, "created_at": "..."}

// ViolationHistory
{"id": 1, "action": "DETECTED", "admin_id": null, "admin_name": null,
 "previous_status": null, "new_status": "DETECTED", "message": "AI가 불법주차 의심 차량을 탐지했습니다.", "created_at": "..."}

// Notification
{"id": 7, "violation_id": 10, "channel": "IN_APP", "status": "SENT", "created_at": "...", "sent_at": "...", "read_at": null,
 "parking_lot_name": "강남 주차장", "zone_name": "A-01", "detected_at": "...", "violation_status": "NOTIFIED"}
```

상태 값:

- Company / Admin / ParkingLot `status`: `ACTIVE`, `INACTIVE`
- Machine `status`: `ACTIVE`, `INACTIVE`
- Violation `status`: `DETECTED`, `NOTIFIED`, `IN_PROGRESS`, `RESOLVED`
- Violation `image_status`: `PENDING`, `UPLOADED`, `FAILED`, `EXPIRED`
- Notification `status`: `PENDING`, `SENT`, `FAILED`, `READ`
- History `action`: `DETECTED`, `NOTIFIED`, `ACKNOWLEDGED`, `RESOLVED`, `REOPENED`, `IMAGE_UPLOAD_FAILED`, `NOTIFY_FAILED`, `VEHICLE_LEFT`, `SIGNAL_LOST`
- Violation `end_reason`: `VEHICLE_LEFT`(차량이 나감), `STICKER_DETECTED`(같은 자리에서 스티커가 탐지됨), `SIGNAL_LOST`(사진이 끊김). 차량이 아직 있으면 `null`

Violation 상태 전이:

```text
DETECTED ──► NOTIFIED ──► IN_PROGRESS ──► RESOLVED
    └──────────────────────► IN_PROGRESS        │
    └──────────────────────────────────────► RESOLVED (NOTIFIED 에서도 가능)
RESOLVED ──► IN_PROGRESS   (reopen, SUPER_ADMIN 전용)
```

## 인증

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| POST `/auth/login` | 없음 | `{email, password}` | 200 `{access_token, expires_in, admin}` + 쿠키 설정 |
| POST `/auth/refresh` | 쿠키 | 없음 | 200 `{access_token, expires_in, admin}`. 쿠키 없거나 만료면 401 |
| POST `/auth/logout` | 쿠키 | 없음 | 200 `{ok: true}` |
| GET `/auth/me` | 로그인 | | 200 `{admin}` |

## 대시보드

| Method / Path | 권한 | 응답 |
|---|---|---|
| GET `/dashboard/summary` | 로그인 | `{companies, admins, parking_lots, machines, open_violations, resolved_violations, stale_violations, stale_violation_hours, offline_machines, failed_notifications}`. COMPANY_ADMIN은 자기 회사 기준 (`companies` 는 1) |

운영 지표의 정의:

| 필드 | 정의 |
|---|---|
| `stale_violations` | 탐지 후 `stale_violation_hours` 시간(기본 24)이 지나도록 RESOLVED가 되지 않은 위반 수 |
| `offline_machines` | 상태가 ACTIVE인데 최근 신호(업로드 또는 Heartbeat)가 없는 장비 수. 기준 시간은 장비의 `online` 판정과 같다 |
| `failed_notifications` | 아직 RESOLVED가 아닌 위반 중 알림이 제대로 전달되지 않은 건수. 전송 실패가 있거나, 담당 관리자가 없거나 발행에 실패해 `NOTIFY_FAILED` 이력이 남고 여전히 DETECTED인 경우 |

## 회사

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/companies` | SUPER_ADMIN | | `{items: Company[], total}` |
| POST `/companies` | SUPER_ADMIN | `{name}` | 201 Company |
| GET `/companies/{id}` | SUPER_ADMIN, 자기 회사의 COMPANY_ADMIN | | Company. 다른 회사면 403 |
| PATCH `/companies/{id}` | SUPER_ADMIN | `{name?, status?}` | Company |

## 관리자

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/admins` | 로그인 | `?company_id=` | `{items: Admin[], total}`. COMPANY_ADMIN은 자기 회사 관리자만 |
| POST `/admins` | SUPER_ADMIN | `{email, password, name, company_id}` | 201 Admin (role은 COMPANY_ADMIN) |
| PATCH `/admins/{id}` | SUPER_ADMIN | `{name?, status?, password?}` | Admin |
| PUT `/admins/{id}/parking-lots` | SUPER_ADMIN | `{parking_lot_ids: number[]}` | Admin. 담당 주차장 전체를 교체. 다른 회사 주차장이 섞이면 400 |

## 주차장과 구역

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/parking-lots` | 로그인 | `?company_id=` | `{items: ParkingLot[], total}` |
| POST `/parking-lots` | SUPER_ADMIN | `{name, address, company_id}` | 201 ParkingLot |
| GET `/parking-lots/{id}` | 로그인 | | ParkingLot + `zones: ParkingZone[]`, `machines: Machine[]`, `admins: Admin[]` |
| PATCH `/parking-lots/{id}` | SUPER_ADMIN | `{name?, address?, status?}` | ParkingLot |
| POST `/parking-lots/{id}/zones` | SUPER_ADMIN | `{zone_name, location_description?, machine_id?}` | 201 ParkingZone |
| PATCH `/zones/{id}` | SUPER_ADMIN | `{zone_name?, location_description?, machine_id?}` | ParkingZone |

## 장비

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/machines` | 로그인 | `?parking_lot_id=&company_id=` | `{items: Machine[], total}` |
| POST `/machines` | SUPER_ADMIN | `{parking_lot_id, serial_number, name}` | 201 `{machine: Machine, api_key: "pgk_..."}`. `api_key` 는 이때 한 번만 보여 준다 |
| PATCH `/machines/{id}` | SUPER_ADMIN | `{name?, status?}` | Machine |
| POST `/machines/{id}/rotate-key` | SUPER_ADMIN | | `{machine, api_key}` |

## 위반

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/violations` | 로그인 | `?status=open\|resolved\|DETECTED\|NOTIFIED\|IN_PROGRESS\|RESOLVED&parking_lot_id=&assigned=true&company_id=&page=1&size=20` | `{items: Violation[], total, page, size}`. `open` = RESOLVED가 아닌 것. 최신순. `assigned=true` 는 내 담당 주차장의 위반만 돌려준다(COMPANY_ADMIN 전용 편의 필터, 권한 제한이 아님) |
| GET `/violations/{id}` | 로그인 | | Violation + `history: ViolationHistory[]` (시간순) |
| POST `/violations/{id}/acknowledge` | 로그인 | | Violation (`IN_PROGRESS`). 이미 처리 중이거나 완료면 409 |
| POST `/violations/{id}/resolve` | 로그인 | `{resolution_note?}` | Violation (`RESOLVED`). 이미 완료면 409 |
| POST `/violations/{id}/reopen` | SUPER_ADMIN | `{message}` | Violation (`IN_PROGRESS`) |
| GET `/violations/{id}/image-url` | 로그인 | | `{url, expires_in}`. `url` 은 짧게 유효한 절대 주소다. `<img src>` 에 그대로 쓴다. 이미지가 없으면 404 |
| GET `/images/{token}` | 서명된 Token | | 이미지 바이트. `image-url` 이 돌려준 주소다 |

## 알림

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/notifications` | 로그인 | `?unread=true&page=1&size=20` | `{items: Notification[], total, page, size, unread_count}`. 내 앱 내 알림만 |
| POST `/notifications/{id}/read` | 본인 | | Notification |
| POST `/notifications/read-all` | 로그인 | | `{updated: 3}` |

## 감사 로그

| Method / Path | 권한 | 요청 | 응답 |
|---|---|---|---|
| GET `/audit-logs` | SUPER_ADMIN | `?company_id=&page=1&size=50` | `{items, total, page, size}`. 항목: `{id, actor_id, actor_email, company_id, action, resource_type, resource_id, before_value, after_value, created_at}` |

## 장비용 API

| Method / Path | 인증 | 요청 | 응답 |
|---|---|---|---|
| POST `/device/detections` | `X-Device-Key` | multipart: `file`(필수), `detected_at`(선택), `zone_name`(선택), `event`(선택: `arrived` / `confirm` / `refresh` / `left`) | 아래 표의 `result` 와 `inference` 요약. 새 위반이 만들어질 때만 201, 나머지는 200 |
| POST `/device/heartbeat` | `X-Device-Key` | 본문 없음(Phase 1), 또는 JSON `{session_id?, occupied?, last_seq?, acked_commands?}`(Phase 2) | `{ok: true, server_time, violation_active, commands}`. `commands` 는 보낸 `session_id` 가 그 장비의 진행 중 세션일 때만 그 세션의 confirm 명령 `[{command_id, type: "capture_confirm", session_id, due_at, expires_at}]`. 실행(confirm 사진 접수)될 때까지 매번 다시 온다. `acked_commands` 의 명령은 취소된다. 본문이 없으면 `commands: []`. 잘못된 `session_id`·`acked_commands` 는 400 |

서버는 같은 장비의 연속된 판정을 하나의 사건으로 묶는다(주차 한 번 = 위반 한 건). 장비가 사진을 보내는 방식은 두 가지이고 서버는 같은 규칙으로 처리한다.

| 장비 모드 | 사진을 보내는 때 | `event` 값 |
|---|---|---|
| interval (지금까지의 방식, `PG_MODE=interval`. 비교·성능 측정용. `scripts/send_detection.py` 도 같은 형식) | 1분마다, 차량이 없어도 | 보내지 않음 |
| event (`PG_MODE=event`, 장비 기본값. Pi가 주차면 점유를 판정) | 차량 진입 확정 때 `arrived`. 응답이 `violation_pending` 이면 60초 뒤 새 사진을 `confirm` 으로. 주차 중 10분마다 `refresh`. 출차 확정 때 빈 자리 사진을 `left` 로. 빈 자리 동안은 사진 없이 Heartbeat만 | `arrived`, `confirm`, `refresh`, `left` |

`event` 가 서버 판정을 바꾸지는 않는다. 서버는 늘 사진을 보고 판정하고(Pi는 "언제 보낼지", 서버는 "위반인지"), `event` 는 `left` 의 종료 규칙 하나에만 쓰인다. `confirm` 사진은 일반 사진과 똑같이 처리되므로 2회 연속 확정 규칙이 그대로 동작한다.

| `result` | 의미 | 서버가 한 일 |
|---|---|---|
| `no_vehicle` | 앞유리가 탐지되지 않음 | 진행 중인 사건이 있으면 미탐지 횟수 증가. 3회 연속이면 사건 종료(`episode_ended: true`). **`event=left` 이면 1회로 바로 종료**(Pi가 이미 30초 동안 빈 자리를 확인했고, 이 뒤로 사진이 더 오지 않기 때문) |
| `permitted` | 스티커가 탐지됨 | 위와 같음(3회 연속). 종료 사유는 `STICKER_DETECTED`. `event=left` 라도 바로 종료하지 않는다: 빈 자리라던 사진에 스티커가 보이면 Pi 판정이 틀린 것이므로 서버 규칙을 그대로 따른다 |
| `violation_pending` | 위반 판정이지만 아직 확정 전 | 연속 횟수만 기록. 저장과 알림 없음 |
| `violation` | 2회 연속 위반으로 확정 | Violation 생성, 사진 저장, 담당자 알림 1회. `violation_id` 포함 |
| `violation_ongoing` | 이미 기록된 차량이 계속 있음 | 기존 Violation의 `last_seen_at`, `detection_count` 만 갱신. 알림 없음 |

- 횟수와 시간은 설정값이다: `VIOLATION_CONFIRM_COUNT`(기본 2), `VEHICLE_CLEAR_COUNT`(기본 3), `EPISODE_GAP_MINUTES`(기본 10).
- 사건이 진행 중인지는 처리 상태와 무관하다. 관리자가 처리 완료해도 차량이 그대로 있으면 새 Violation을 만들지 않는다.
- 장비의 **신호**(사진 또는 Heartbeat 중 늦은 것)가 `EPISODE_GAP_MINUTES` 넘게 끊기면 진행 중인 사건을 `SIGNAL_LOST` 로 닫고 다시 센다. event 모드에서는 주차 중 사진이 10분마다만 오지만 Heartbeat가 매분 오므로 끊김으로 보지 않는다.

### Phase 2 비동기 접수 `POST /device/detection-jobs` (ADR 0019, 2026-11-02 로컬 구현, 배포 전)

`QUEUE_BACKEND` 가 `none`(기본)이면 **404 `async_ingest_disabled`** 이다. Phase 1 `POST /device/detections` 는 그대로다. 이 엔드포인트는 판정을 돌려주지 않는다. 판정은 Worker 가 나중에 하고, 위반 확정·confirm 요청은 Heartbeat 응답으로 전달한다(Worker·Heartbeat 명령은 5단계 이후).

| 요청 필드 (multipart) | 필수 | 설명 |
|---|---|---|
| `file` | 예 | 사진. 바이트 그대로 inbox 에 저장(재압축 없음) |
| `event` | 예 | `arrived` / `confirm` / `refresh` / `left` |
| `event_id` | 예 | UUID. 장비가 사진마다 만들고 재전송 때 같은 값. 중복 제거 키 `(장비, event_id)` |
| `session_id` | 예 | UUID. 진입 ~ 출차 한 번. 서버가 처음 보는 세션은 이벤트 종류와 상관없이 등록하고 그 장비의 현재 세션으로 삼는다 |
| `seq` | 예 | 세션 안 순서, 1 이상 정수 |
| `detected_at` | 아니오 | 장비 시계. 표시·감사용에만 쓴다(순서 판단에도, inbox 키에도 쓰지 않음) |
| `command_id` | 아니오 | UUID. `confirm` 사진이 어떤 명령에 응한 것인지. **보내면 유효해야 한다**: 그 장비·그 사진의 `session_id` 의 `capture_confirm` 명령이어야 하고 사진이 `confirm` 이어야 한다(명령 상태는 상관없음). 유효하지 않으면 **409 `command_conflict`** 로 거절하고 아무것도 저장하지 않는다(2026-11-02 사용자 결정. 이전에는 무시했다). 보내지 않은 사진(`command_id` 없음)은 지금까지와 같다 |

| 응답 | 의미 | 장비 동작 |
|---|---|---|
| **202** `{job_id, event_id, status, duplicate: false, server_time}` | 새로 접수. `status` 는 `queued`(큐 전송 성공) 또는 `pending_publish`(재전송기가 보냄) | 성공. 로컬 아웃박스에서 삭제 |
| **200** `{…, duplicate: true}` | 같은 `event_id` 가 이미 접수됨(같은 `job_id`) | 성공 |
| **200** `{job_id, event_id, status, duplicate: true, reason: "command_already_confirmed", command_id, existing_event_id}` | **다른** `event_id` 이지만 이 명령에는 이미 confirm 작업이 있다. 응답의 `job_id`·`status` 는 그 **기존 작업**이고, 이 요청의 사진·작업·큐 메시지·신호 갱신은 **저장되지 않았다**. `status` 는 그 사진의 현재 운명이다: `done`·`stale`·`queued`·`processing` 등은 정상 진행, `rejected` 는 최종(이 명령의 confirm 판정은 없다. 다음 refresh 가 대신한다), `dead_lettered` 는 운영자가 `flask jobs redrive` 로 다시 보낼 수 있다 | 성공(다시 보내도 달라지지 않는다). 포기할 때까지 기다리지 말고 Outbox 에서 지운다 |
| 400 | 필드 누락·형식 오류 | 재시도하지 않음 |
| 401 | 장비 키 없음·틀림 | 재시도하지 않음 |
| 404 `async_ingest_disabled` | 비동기 접수가 꺼져 있음 | — |
| 409 `event_id_conflict` | 같은 `event_id` 로 다른 `session_id`·`seq`·`event`·**`command_id`**·사진이 이미 접수됨(장비 버그). `command_id` 를 더하거나 빼거나 다른 유효한 값으로 바꾸는 것도 충돌이다 | 재시도하지 않음 |
| 409 `command_conflict` | `command_id` 가 유효한 confirm 명령이 아님: 없는 명령, 다른 장비·다른 세션의 명령, 다른 세션의 사진이 이미 쓴 명령, `confirm` 이 아닌 사진, 30일 뒤 지워진 명령. **저장된 것이 없다**(작업·사진·큐 메시지·세션 등록·신호 갱신·명령 상태 모두 그대로). 같은 요청을 다시 보내도 같은 409 이다 | 재시도하지 않음(Outbox `dead/` 에 `409 command_conflict` 로 보관) |
| 409 `session_conflict` | 다른 장비의 `session_id` | 재시도하지 않음 |
| 503 `storage_unavailable` / `database_unavailable` | 사진 저장 또는 DB 기록 실패. 아무것도 접수되지 않음 | 같은 `event_id` 로 재시도 |

접수 때 서버가 바로 하는 일(상태 경로, 설계 6.2): 처음 보는 세션 등록(이전 active 세션은 `superseded`, 그 명령은 `cancelled`), `left` 면 세션 `ended` + 그 세션 명령 `cancelled`, `confirm` + 유효한 `command_id` 면 작업에 그 `command_id` 를 저장하고 명령이 아직 진행 중이면 `fulfilled`(작업에 저장된 `command_id` 는 장비별 부분 UNIQUE 라 한 명령에는 confirm 작업이 하나뿐이다), 장비 신호 시각 갱신(갱신 전 값은 작업의 `prev_signal_at` 에 보관, 기존 값보다 늦을 때만 갱신). 장비의 현재 세션·마지막 반영 순번(순서 가드)은 바꾸지 않는다(Worker 가 판정을 반영할 때 바꾼다). inbox 키는 `inbox/m-<장비>/<event_id>` (날짜·확장자 없음). 이미 있는 객체는 덮어쓰지 않는다: 같은 바이트면 재시도로 보고 진행, 다른 바이트면 409 `event_id_conflict`.

판정 결과는 `flask judge-worker` (Worker, 2026-11-02 로컬 구현) 가 반영한다. 응답·Heartbeat 명령 전달은 7단계.
- 중복 위반 방지는 사진 방식과 무관하게 같다: 장비 행 잠금(`FOR UPDATE`)으로 같은 장비의 사진을 한 장씩 처리하고, 진행 중인 사건은 DB의 조건부 UNIQUE Index로 장비당 하나만 허용한다.
- 번호판 OCR 은 2026-10-25 에 제거됐다(ADR 0017). 위반 응답에 `plate_number`, `plate_confidence` 는 없다. 이력 `OCR_FAILED` 는 과거 데이터에만 있을 수 있다.
- 사진 저장소는 `STORAGE_BACKEND`(local / s3, ADR 0016)로 고르며 API 는 같다. `image-url` 은 여전히 Backend 주소(`/api/v1/images/<token>`)를 주고, 앞단 프록시(nginx, ALB)의 `X-Forwarded-Proto`/`Host` 를 반영한다.
- 데모 데이터의 비밀번호와 장비 키는 `SEED_ADMIN_PASSWORD`, `SEED_DEVICE_KEY_SECRET` 로 주입할 수 있다(없으면 로컬 고정값). 운영자는 `flask device-key SERIAL` 로 장비 키를 얻는다.
- 장비 하나에 진행 중인 사건은 하나뿐이다. DB의 조건부 UNIQUE Index가 보장한다.

## 상태 확인

| Method / Path | 응답 |
|---|---|
| GET `/healthz` | 프로세스 생존 여부 `{status: "ok"}` |
| GET `/readyz` | DB 연결 확인 `{status: "ok", database: "ok"}`. 실패 시 503 |
