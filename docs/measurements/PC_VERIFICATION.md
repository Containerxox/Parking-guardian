# PC에서 한 Raspberry Pi 기능 검증 (2026-10-24)

실제 Raspberry Pi 4가 없어서 PC에서 할 수 있는 것만 검증했다. **두 종류의 결과를 섞어 읽지 않도록** 나눠 적었다.

- A. **가상 점유 시나리오**: 점유 상태를 미리 정해 넣는다. 이벤트 전송 규약과 서버 연동을 검증한다. **AI 탐지 정확도와는 무관하다.**
- B. **실제 사진 → YOLO26s → ROI 판정**: 차량 탐지 모델이 실제로 돈다. 다만 사진이 3장뿐이고 빈 자리 사진이 없어서 "정확도"라고 부를 수 있는 수치는 아니다.

공통으로 쓴 코드: `device/pi_client.py` 의 `EventModeRunner`(상태 기계)와 `send_photo`/`send_heartbeat`, `device/vehicle_detector.py`. 서버는 실제 Backend(모델 포함)를 5001 포트에 별도 SQLite로 띄웠다. 테스트 데이터는 세션 임시 폴더에만 있다.

## A. 가상 점유 시나리오 → 이벤트 전송 → 서버 연동

### A-1. pytest 통합 테스트 (`backend/tests/test_event_mode_integration.py`, 2개)

`EventModeRunner` 를 Flask 테스트 클라이언트에 직접 붙였다. 점유는 타임라인이 주고(빈 자리 2분 → 위반 차량 15분 → 빈 자리 5분 → 정상 차량 11.7분 → 빈 자리 2분), 서버 판정은 파일 이름으로 정하는 Mock 추론이다. 가짜 시계라 1.5초에 끝난다. 확인한 것:

| 시뮬레이션 시각 | 장비가 보낸 것 | 서버 응답 |
|---|---|---|
| 0, 60, 120, … | Heartbeat 매분 (총 36회) | 200 |
| 0~120초 (빈 자리) | 사진 없음 | |
| 130초 | `arrived` (점유 3프레임째) | `violation_pending` |
| 190초 | `confirm` (60초 뒤 새 사진) | `violation` 201, 위반 생성 |
| 730초 | `refresh` (10분) | `violation_ongoing` |
| 1045초 | `left` (비점유 6프레임째) | `no_vehicle`, `episode_ended: true` |
| 1335초 | `arrived` (정상 차량) | `permitted` |
| 1935초 | `refresh` | `permitted` |
| 2050초 | `left` | `no_vehicle` (닫을 사건 없음) |

서버 쪽: 위반 1건(탐지 횟수 3, 종료 사유 `VEHICLE_LEFT`, 이력 DETECTED → NOTIFIED → VEHICLE_LEFT), 담당자 알림 1건, 장비 카운터 0으로 복귀, 진행 중 사건 없음. LED는 `left` 뒤 꺼짐.

두 번째 테스트: 첫 판정이 `violation_pending` 인데 60초 안에 차가 떠난 경우. `left` 만 가고 `confirm` 은 취소되며 위반이 생기지 않고 카운터가 0이 된다.

### A-2. 실제 서버에 대한 가상 시나리오 (`backend/scripts/measure.py --mode event`)

After 측정(`README.md`)이 이것이다. 4시간 시나리오를 실제 모델이 도는 서버에 보냈고 사진 22장, Heartbeat 240건, 위반 1건·알림 1건, 종료 사유 `VEHICLE_LEFT` 였다. 여기서도 점유는 시나리오가 준 값이다.

### A에서 알 수 있는 것 / 없는 것

- 알 수 있는 것: 진입·주차 유지·출차의 사진 전송 순서, `confirm` 타이밍과 취소, `refresh` 주기, Heartbeat 주기, 서버의 사건 생성·유지·종료, 알림 1회.
- **알 수 없는 것: Pi가 실제로 차를 보고 점유를 맞게 판정하는가.** 그것은 B의 영역이고, B도 아래 한계가 있다.

## B. 실제 사진 → YOLO26s → ROI 점유 판정

모델은 `yolo26s.pt`(ultralytics 자동 다운로드, `device/`에 둠, Git 제외)와 PC에서 NCNN으로 내보낸 `device/yolo26s_ncnn_model/`(37 MB). 입력 480px. ROI와 기준값은 기본값(`sample_c` 기준 사다리꼴, 겹침 진입 0.30 / 이탈 0.15, 면적 5%, 잘림 폭 35%, 신뢰도 0.40).

### B-1. 사진 1장씩 ROI 판정 (`device/roi_preview.py`)

| 사진 | 구도 | 탐지된 차 | 인정 | 제외와 사유 | 판정 |
|---|---|---|---|---|---|
| sample_a | 정면, 가까움 | 1 | #0 conf 0.85, 면적 62%, 겹침 59% | | 점유 |
| sample_b | 정면, 옆 칸 차량 보임 | 4 | #0 conf 0.93, 면적 32%, 겹침 59% | #1 옆 칸 차(conf 0.89): 겹침 28% < 30%, 좌측 끝 잘림 폭 24%. #2, #3 뒤쪽 차: 면적 2%, ROI 밖 또는 신뢰도 0.16 | 점유 |
| sample_c | **Pi 카메라와 같은 각도** | 1 | #0 conf 0.95, 면적 65%, 겹침 50% | | 점유 |
| sample_c (NCNN) | 같음 | 1 | #0 conf 0.94, 면적 66%, 겹침 49% | | 점유 (.pt 와 같은 결과) |

sample_b 에서 옆 칸 차량이 두 가지 규칙(겹침, 가장자리 잘림)으로 제외된 것이 ROI 규칙의 목적이다. 그림은 세션 임시 폴더에만 저장했다(`roi_preview.py --out` 으로 다시 만들 수 있다).

### B-2. 사진 프레임 → 탐지 → 상태 기계 → 서버 (`device/pc_event_test.py`)

카메라 대신 사진 타임라인을 프레임으로 넣고, 나머지(YOLO26s, ROI 판정, 연속 프레임, `EventModeRunner`, 전송)는 실제 코드가 돈다. 10배속. 타임라인: 빈 자리 60초 → 사진 200초 → 빈 자리 90초. **"빈 자리" 프레임은 실제 사진을 강하게 흐린 합성 프레임**이라 "탐지되는 것이 없는 프레임"을 흉내 낼 뿐이다.

| 실행 | 프레임 수 / 탐지 평균 | 상태 변화 (시뮬레이션 초) | 보낸 사진과 서버 응답 | 서버 결과 |
|---|---|---|---|---|
| sample_c (스티커 있음) | 60장 / 215 ms | 없음 → 비점유(56) → 점유(76) → 비점유(288) | `arrived` → `permitted`, `left` → `no_vehicle` | 위반 없음 |
| sample_b (스티커 없음) | 62장 / 196 ms | 비점유(38) → 점유(73) → 비점유(288) | `arrived` → `violation_pending`, `confirm`(약 60초 뒤, 새 사진) → `violation`, `left` → `no_vehicle` + `episode_ended` | 위반 1건, 종료 `VEHICLE_LEFT`, 이력 DETECTED → NOTIFIED → VEHICLE_LEFT, 알림 1건 |

Heartbeat는 두 실행 모두 350초 동안 6회(0, 60, …, 300). 점유 전환은 3프레임(15초) 뒤, 출차 전환은 6프레임(30초) 뒤에 일어났다. 첫 실행에서는 첫 추론이 4.9초 걸려 시뮬레이션 시계가 49초 뛰었기에 도구에 예열을 넣었다(두 번째 실행부터 적용).

### B에서 알 수 있는 것 / 없는 것

- 알 수 있는 것: YOLO26s(.pt와 NCNN 둘 다)가 세 사진의 대상 차량을 잡고 ROI 규칙이 옆 칸·뒤쪽 차를 거른다. 탐지 결과가 상태 기계와 서버까지 끊김 없이 이어진다. PC CPU 탐지 시간 약 0.2초/프레임(.pt).
- **알 수 없는 것**: 실제 빈 주차면에서 "없음"으로 판정되는가(빈 자리 사진이 없다), 경계에 걸친 차·앞으로 당겨 댄 차·야간, 기준값이 수십 장에서도 맞는가. 사진 3장은 정확도 수치가 아니다.

## Raspberry Pi 4에서만 확인할 수 있는 것 (미검증)

| 항목 | 이유 |
|---|---|
| 카메라: lores 스트림 설정, YUV420 → BGR 변환(`Hardware.capture_frame`), main 스트림 촬영과의 동시 사용 | picamera2 가 Pi 에만 있다. 코드는 있지만 한 번도 돌지 않았다 |
| NCNN 실행 속도와 메모리 (5초 간격 안에 1프레임이 끝나는가) | PC 에서 내보내고 돌려 본 것은 형식 확인일 뿐이다. Pi 4 ARM 에서의 속도는 다르다 |
| GPIO LED | Pi 전용 |
| 실제 카메라 사진에서의 서버 모델(앞유리·스티커)과 OCR 동작 | 샘플은 스마트폰 사진이다 |
| 빈 자리·옆 칸·경계·야간 프레임의 점유 판정 정확도, 기준값 재조정 | Pi 카메라 프레임을 수십 장 모아 정답을 붙여야 한다 |
| 전송 실패·재부팅 뒤의 동작 | 네트워크와 systemd 환경 |

## 다시 돌리는 방법

```powershell
# A-1
cd backend; .\venv\Scripts\python -m pytest tests\test_event_mode_integration.py -q
# B-1 (yolo26s.pt 는 처음 한 번 자동 다운로드)
backend\venv\Scripts\python device\roi_preview.py backend\uploads\sample_b.jpg --model device\yolo26s.pt --out roi_b.jpg
# B-2 (Backend 를 띄운 뒤. 컨테이너 Backend 를 쓰면 --server http://127.0.0.1:5000/api/v1, 개발 DB 에 위반이 생긴다)
backend\venv\Scripts\python device\pc_event_test.py --server http://127.0.0.1:5001/api/v1 --timeline "empty:60,backend/uploads/sample_b.jpg:200,empty:90" --speed 10
```
