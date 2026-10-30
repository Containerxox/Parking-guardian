# OCR 제거 전후 비교 (2026-10-25)

결정은 `docs/adr/0017-remove-plate-ocr.md`. 이 문서는 측정 원본이다.

## 조건

| 항목 | 값 |
|---|---|
| PC | 이 Windows PC, Docker Desktop. `docker compose up -d`(db, backend, frontend) |
| 제거 전 이미지 | `parking-guardian-backend:latest` `fb76bb22bd9d`, 2026-10-24 빌드(easyocr 포함) |
| 제거 후 이미지 | `273aee4d7538`, 2026-10-25 빌드. **OCR 제거만 반영**(boto3 는 그 뒤에 추가되어 이 비교에 없음) |
| 사진 | `backend/uploads/sample_e.jpg`(2.9 MB, 서버 판정 permitted 라 DB 에 남지 않음), 회귀 확인은 `sample_b.jpg`(위반) |
| 장비 | 시드 장비 `RPI-A-GANGNAM-01`(측정), `RPI-A-PANGYO-01`(회귀) |
| 메모리 | `docker stats --no-stream` 의 컨테이너 MemUsage. 유휴는 healthy 뒤 15초 |
| 시간 | `scripts/send_detection.py` 호출 전후 `perf_counter`. **Python 인터프리터 시작과 요청 전송이 포함**되어 서버 추론 시간보다 약 1초 길다 |
| 반복 | 각 1회. 편차를 재지 않았다 |

## 결과

| 항목 | 제거 전 | 제거 후 | 차이 |
|---|---|---|---|
| 이미지 크기(`docker image ls`) | 3.16 GB | 2.84 GB | −320 MB (−10%) |
| `pip install -r requirements.txt` 레이어 | 308 MB | 77 MB | −231 MB |
| torch + YOLOv5 의존성 레이어 | 1.77 GB | 1.77 GB | 같음(캐시 재사용) |
| apt(libgl1, libglib2.0-0) 레이어 | 216 MB | 216 MB | 같음 |
| 설치 패키지 수(`pip list`) | 76 | 66 | −10 |
| `easyocr` 설치 여부 | 1.7.2 | 없음 | |
| 유휴 메모리 | 81.38 MiB | 81.4 MiB | 같음 |
| 첫 사진 뒤 메모리 | 891.8 MiB | 889.9 MiB | 같음 |
| 둘째 사진 뒤 메모리 | 891.8 MiB | 931.6 MiB | +40 MiB(요청 처리 중 일시 증가로 봄) |
| 첫 사진 왕복(모델 로딩 포함) | 18.2초 | 12.7초 | 편차로 봄 |
| 둘째 사진 왕복 | 2.4초 | 1.7초 | 편차로 봄 |

제거된 패키지(venv 메타데이터로 "easyocr 만 요구"하는 것을 계산): `easyocr`, `scikit-image`, `imageio`, `tifffile`, `lazy-loader`, `shapely`, `pyclipper`, `python-bidi`, `ninja`, `opencv-python-headless`. `scipy`(YOLOv5 요구)와 `networkx`(torch 요구)는 남는다.

## 해석

- 이미지 감소는 전부 `requirements.txt` 레이어에서 나왔다. torch 1.77 GB 는 그대로이므로 Backend 이미지의 큰 감소는 추론 서비스를 분리하는 Phase 2 의 몫이다.
- 메모리가 같은 것은 예상대로다. OCR 어댑터는 `OCR_ENABLED=false` 면 `easyocr` 을 import 하지 않았다(지연 로딩). 켰을 때의 +428 MB 는 `docs/measurements/README.md` "OCR 비용"에 기록되어 있다.
- 첫 사진 시간 차이는 OCR 과 무관하다. 로딩되는 모델(앞유리·스티커)은 바뀌지 않았다. 1회 측정의 편차다.

## 회귀 확인 (제거 후 이미지)

`sample_b` 2장 → 1장째 `violation_pending`, 2장째 `violation`, 위반 8 생성. DB: `status=NOTIFIED`, `detection_count=2`, `image_status=UPLOADED`, `plate_number` NULL. 알림: 담당 관리자 2명(kim, lee) × IN_APP·EMAIL = 4건 `SENT`. 이력: `DETECTED`, `NOTIFIED`. 테스트: SQLite 46개, PostgreSQL 46개 통과(이벤트 모드 통합 테스트 2개를 PostgreSQL 에서 처음 실행).

## 보관한 모델

`C:\parking_guardian\archive\ocr-models\` (lp_det.pt, lp_models/, README.txt). 7개 파일 37.6 MB, 복사 뒤 SHA-256 비교 후 원본 삭제.
