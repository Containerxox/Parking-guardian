"""주차면 한 칸의 차량 점유 판정 (Raspberry Pi 4 용).

목표는 "화면에 차가 보이는가"가 아니라 "지정된 장애인 주차면에 차가 서 있는가"다.
YOLO 는 옆 칸, 뒤쪽 통로의 차량까지 전부 잡으므로, 탐지 결과를 아래 규칙으로 거른다.

  1. 아랫변 중심   박스 아랫변의 가운데 점(바퀴가 닿는 자리)이 주차면 다각형(ROI) 안에 있어야 한다
  2. 겹침 비율     박스∩ROI / 박스 면적이 기준 이상이어야 한다. 경계에 걸친 차와 뒤쪽 차를 거른다
  3. 크기          박스가 화면의 일정 비율 이상이어야 한다. 멀리 있는 작은 차를 거른다
  4. 가장자리 잘림 화면 좌우 끝에 잘려 있고 폭이 좁은 박스는 옆 칸 차량으로 보고 제외한다
  5. 신뢰도        YOLO 신뢰도 하한
  6. 연속 프레임   "없음→있음"은 enter_frames 프레임 연속, "있음→없음"은 exit_frames 프레임 연속일 때만 바꾼다.
                   겹침 비율도 들어올 때(overlap_enter)와 나갈 때(overlap_exit) 기준을 다르게 둔다(이력 현상).

모든 좌표는 0~1 로 정규화한다. 저해상도 프레임(640x480)과 원본 사진(3280x2464)은 둘 다 4:3 이라 같은 값을 쓴다.
ROI 와 기준값은 코드에 고정하지 않고 환경변수(PG_ROI, PG_OCC_*)나 인자로 준다. 기본값은 sample_c 한 장에서 잡은 것이다.

모델은 COCO 사전학습 YOLO26. 기본은 small(yolo26s), 입력 480px 다. nano 도 써 봤지만 sample_c 처럼 차가
화면의 2/3 를 채우는 가까운 각도에서는 신뢰도가 0.1~0.4 로 흔들려 쓸 수 없었다(PC 측정, device/README.md 참고).
Pi 4 에서는 NCNN 으로 내보낸 폴더가 가볍다:

    from ultralytics import YOLO
    YOLO("yolo26s.pt").export(format="ncnn", imgsz=480)      # -> yolo26s_ncnn_model/
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict

import numpy as np

VEHICLE_CLASSES = (2, 3, 5, 7)  # COCO: car, motorcycle, bus, truck

# sample_c (Pi 카메라와 같은 각도) 에서 잡은 주차면. 위쪽이 벽 쪽(먼 곳), 아래쪽이 카메라 쪽.
# 순서: 왼쪽 위, 오른쪽 위, 오른쪽 아래, 왼쪽 아래. 위 변은 벽과 바닥이 만나는 선(y 0.57),
# 아래 변은 화면 끝(y 1.0)까지 둔다. 주차면 앞 선은 y 0.97 쯤이지만 차 앞 범퍼의 박스 아랫변은
# 카메라가 위에서 보기 때문에 바퀴 자리보다 앞(아래)에 찍히므로, 앞으로 당겨 댄 차가 빠지지 않게 한다.
DEFAULT_ROI = "0.10,0.57,0.90,0.57,1.00,1.00,0.00,1.00"


def parse_roi(text: str | None) -> list[tuple[float, float]]:
    """'x1,y1,x2,y2,...' (0~1, 점 3개 이상) -> [(x, y), ...]"""
    text = (text or "").strip() or DEFAULT_ROI
    values = [float(v) for v in text.replace(";", ",").split(",") if v.strip()]
    if len(values) < 6 or len(values) % 2:
        raise ValueError(f"ROI 는 x,y 쌍 3개 이상이어야 합니다: {text!r}")
    points = [(values[i], values[i + 1]) for i in range(0, len(values), 2)]
    if not all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in points):
        raise ValueError(f"ROI 좌표는 0~1 사이여야 합니다: {text!r}")
    return points


@dataclass
class OccupancyConfig:
    roi: list = field(default_factory=lambda: parse_roi(DEFAULT_ROI))
    min_conf: float = 0.40            # YOLO 신뢰도 하한
    min_area: float = 0.05            # 박스 면적 / 화면 면적 하한
    overlap_enter: float = 0.30       # 박스∩ROI / 박스 면적. 비어 있다가 "있음"으로 바꿀 때
    overlap_exit: float = 0.15        # 있다가 "없음"으로 바꿀 때 (이보다 작아야 없음)
    edge_clip_max_width: float = 0.35 # 화면 좌우 끝에 잘린 박스는 폭이 이 비율보다 좁으면 옆 칸 차량으로 본다
    enter_frames: int = 3             # 없음 -> 있음 에 필요한 연속 프레임
    exit_frames: int = 6              # 있음 -> 없음 에 필요한 연속 프레임

    @classmethod
    def from_env(cls, env=os.environ) -> "OccupancyConfig":
        g = env.get
        return cls(
            roi=parse_roi(g("PG_ROI")),
            min_conf=float(g("PG_OCC_MIN_CONF", cls.min_conf)),
            min_area=float(g("PG_OCC_MIN_AREA", cls.min_area)),
            overlap_enter=float(g("PG_OCC_OVERLAP_ENTER", cls.overlap_enter)),
            overlap_exit=float(g("PG_OCC_OVERLAP_EXIT", cls.overlap_exit)),
            edge_clip_max_width=float(g("PG_OCC_EDGE_CLIP_MAX_WIDTH", cls.edge_clip_max_width)),
            enter_frames=int(g("PG_OCC_ENTER_FRAMES", cls.enter_frames)),
            exit_frames=int(g("PG_OCC_EXIT_FRAMES", cls.exit_frames)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BoxJudgement:
    xyxy: list                 # 픽셀 좌표 [x1, y1, x2, y2]
    cls: int
    conf: float
    area: float                # 화면 대비 면적 비율
    bottom_center: tuple       # 정규화 (x, y)
    inside: bool               # 아랫변 중심이 ROI 안
    overlap: float             # 박스∩ROI / 박스 면적
    edge_clipped: bool         # 좌우 끝에 잘린 좁은 박스
    accepted: bool
    reasons: list              # 제외 사유 (비어 있으면 인정)


@dataclass
class FrameJudgement:
    occupied: bool             # 이 프레임에서 인정된 박스가 하나라도 있는가
    boxes: list                # [BoxJudgement]
    elapsed_ms: int = 0


class RoiJudge:
    """YOLO 박스 목록을 받아 프레임 하나의 점유 여부를 정한다. 모델과 무관해서 테스트가 쉽다."""

    def __init__(self, config: OccupancyConfig):
        self.config = config
        self._mask_size = None
        self._mask = None
        self._poly = None

    def _prepare(self, width: int, height: int) -> None:
        import cv2

        if self._mask_size == (width, height):
            return
        poly = np.array([(x * width, y * height) for x, y in self.config.roi], dtype=np.float32)
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [poly.astype(np.int32)], 1)
        self._mask_size, self._mask, self._poly = (width, height), mask, poly

    def judge(self, boxes, width: int, height: int, currently_occupied: bool = False) -> FrameJudgement:
        """boxes: [(x1, y1, x2, y2, conf, cls)] 픽셀 좌표. currently_occupied 에 따라 겹침 기준이 달라진다."""
        import cv2

        self._prepare(width, height)
        c = self.config
        overlap_needed = c.overlap_exit if currently_occupied else c.overlap_enter
        judged = []
        for x1, y1, x2, y2, conf, cls in boxes:
            x1, y1 = max(0, int(x1)), max(0, int(y1))
            x2, y2 = min(width, int(x2)), min(height, int(y2))
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            area = (w * h) / (width * height)
            bottom = ((x1 + x2) / 2.0, float(y2))
            inside = cv2.pointPolygonTest(self._poly, bottom, False) >= 0
            overlap = float(self._mask[y1:y2, x1:x2].sum()) / (w * h)
            touches_edge = x1 <= width * 0.01 or x2 >= width * 0.99
            edge_clipped = touches_edge and (w / width) < c.edge_clip_max_width

            reasons = []
            if conf < c.min_conf:
                reasons.append(f"conf {conf:.2f} < {c.min_conf}")
            if area < c.min_area:
                reasons.append(f"area {area:.1%} < {c.min_area:.0%}")
            if not inside:
                reasons.append("bottom-center outside ROI")
            if overlap < overlap_needed:
                reasons.append(f"overlap {overlap:.0%} < {overlap_needed:.0%}")
            if edge_clipped:
                reasons.append(f"clipped at frame edge, width {w / width:.0%}")
            judged.append(BoxJudgement(
                xyxy=[x1, y1, x2, y2], cls=int(cls), conf=float(conf), area=area,
                bottom_center=(bottom[0] / width, bottom[1] / height), inside=inside, overlap=overlap,
                edge_clipped=edge_clipped, accepted=not reasons, reasons=reasons))
        return FrameJudgement(occupied=any(b.accepted for b in judged), boxes=judged)


class OccupancyTracker:
    """프레임 판정을 연속 프레임 규칙으로 상태(있음/없음)로 바꾼다."""

    def __init__(self, config: OccupancyConfig):
        self.config = config
        self.state: bool | None = None   # None = 아직 모름
        self._candidate: bool | None = None
        self._count = 0
        self.transitions = 0

    def update(self, frame_occupied: bool) -> bool | None:
        """프레임 결과를 넣고 현재 상태를 돌려준다. 상태가 바뀐 프레임에서는 changed() 가 True."""
        self._changed = False
        if frame_occupied == self.state:
            self._candidate, self._count = None, 0
            return self.state
        if self._candidate != frame_occupied:
            self._candidate, self._count = frame_occupied, 1
        else:
            self._count += 1
        needed = self.config.enter_frames if frame_occupied else self.config.exit_frames
        if self.state is None:
            needed = self.config.enter_frames        # 부팅 직후 첫 판정은 짧게
        if self._count >= needed:
            self.state = frame_occupied
            self._candidate, self._count = None, 0
            self.transitions += 1
            self._changed = True
        return self.state

    def changed(self) -> bool:
        return getattr(self, "_changed", False)


class VehicleDetector:
    """YOLO 로 차량 박스를 찾고 RoiJudge 로 주차면 점유를 판정한다."""

    def __init__(self, model_path: str = "yolo26s_ncnn_model", imgsz: int = 480,
                 config: OccupancyConfig | None = None, classes=VEHICLE_CLASSES):
        self.model_path = model_path
        self.imgsz = imgsz
        self.config = config or OccupancyConfig()
        self.classes = list(classes)
        self.judge = RoiJudge(self.config)
        self.tracker = OccupancyTracker(self.config)
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from ultralytics import YOLO   # Pi 에서는 `pip install ultralytics` (torch 포함)

            self._model = YOLO(self.model_path, task="detect")

    def detect_boxes(self, frame_bgr) -> list:
        """[(x1, y1, x2, y2, conf, cls)]. 신뢰도는 판정 단계에서 거르므로 낮은 것까지 받는다."""
        self._ensure_loaded()
        result = self._model(frame_bgr, imgsz=self.imgsz, conf=min(0.1, self.config.min_conf),
                             classes=self.classes, verbose=False)[0]
        return [(*xyxy, conf, cls) for xyxy, conf, cls in
                zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist())]

    def process_frame(self, frame_bgr) -> FrameJudgement:
        """프레임 하나: 탐지 -> ROI 판정 -> 연속 프레임 상태 갱신. tracker.state 가 장비의 현재 판단이다."""
        started = time.perf_counter()
        boxes = self.detect_boxes(frame_bgr)
        height, width = frame_bgr.shape[:2]
        frame = self.judge.judge(boxes, width, height, currently_occupied=bool(self.tracker.state))
        self.tracker.update(frame.occupied)
        frame.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return frame
