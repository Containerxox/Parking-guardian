"""ROI 와 점유 판정을 사진 위에 그려서 확인하는 도구.

설치할 때 카메라가 보는 사진으로 ROI 를 잡고, 어느 차가 인정되고 제외되는지 눈으로 확인한다.

    # PC 에서 (backend 가상환경에 ultralytics 가 있다. yolo26s.pt 는 처음 한 번 자동 다운로드)
    backend\\venv\\Scripts\\python device\\roi_preview.py backend\\uploads\\sample_c.jpg --out roi_sample_c.jpg

    # 다른 ROI 로 시험
    backend\\venv\\Scripts\\python device\\roi_preview.py photo.jpg --roi 0.12,0.58,0.88,0.58,1,0.93,0,0.93

    # Pi 에서 카메라 프레임으로 (NCNN 모델)
    python3 roi_preview.py --camera --model yolo26s_ncnn_model --out /tmp/roi.jpg

출력: 초록 다각형 = ROI, 초록 박스 = 점유로 인정된 차량, 빨간 박스 = 제외된 차량(사유 표시),
점 = 판정에 쓴 아랫변 중심. 터미널에 차량별 수치 표를 찍고 --json 을 주면 JSON 으로도 저장한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vehicle_detector import OccupancyConfig, VehicleDetector, parse_roi  # noqa: E402

GREEN, RED, YELLOW, WHITE, BLACK = (60, 200, 60), (40, 40, 230), (0, 210, 255), (255, 255, 255), (0, 0, 0)


def draw(frame_bgr, judgement, config: OccupancyConfig, state_text: str):
    import cv2
    import numpy as np

    out = frame_bgr.copy()
    height, width = out.shape[:2]
    thick = max(2, width // 400)
    scale = max(0.6, width / 1600)

    # ROI: 반투명 채움 + 테두리 + 꼭짓점 좌표
    poly = np.array([(int(x * width), int(y * height)) for x, y in config.roi], dtype=np.int32)
    layer = out.copy()
    cv2.fillPoly(layer, [poly], GREEN)
    out = cv2.addWeighted(layer, 0.18, out, 0.82, 0)
    cv2.polylines(out, [poly], True, GREEN, thick)
    for (px, py), (nx, ny) in zip(poly, config.roi):
        cv2.circle(out, (int(px), int(py)), thick * 3, GREEN, -1)
        cv2.putText(out, f"({nx:.2f},{ny:.2f})", (int(px) + 6, int(py) - 6), cv2.FONT_HERSHEY_SIMPLEX,
                    scale * 0.8, GREEN, thick)

    def label(text, x, y, color):
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        y = max(th + 6, y)
        cv2.rectangle(out, (x, y - th - 6), (x + tw + 8, y + 4), color, -1)
        cv2.putText(out, text, (x + 4, y - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, WHITE, thick)

    for i, b in enumerate(judgement.boxes):
        x1, y1, x2, y2 = b.xyxy
        color = GREEN if b.accepted else RED
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thick)
        bx, by = int(b.bottom_center[0] * width), int(b.bottom_center[1] * height)
        cv2.circle(out, (bx, by), thick * 4, YELLOW, -1)
        cv2.circle(out, (bx, by), thick * 4, BLACK, max(1, thick // 2))
        head = f"#{i} {'IN' if b.accepted else 'OUT'} conf {b.conf:.2f} area {b.area:.0%} overlap {b.overlap:.0%}"
        label(head, x1, y1 - 4, color)
        for k, reason in enumerate(b.reasons):
            label(reason, x1, y1 - 4 + int((k + 1) * 26 * scale * 1.6), color)

    banner = f"OCCUPIED: {'YES' if judgement.occupied else 'NO'}   state: {state_text}   boxes: {len(judgement.boxes)}"
    label(banner, 10, int(34 * scale * 1.6), GREEN if judgement.occupied else RED)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", nargs="?", help="사진 파일 (또는 --camera)")
    parser.add_argument("--camera", action="store_true", help="Raspberry Pi 카메라에서 프레임 한 장을 찍어 쓴다")
    parser.add_argument("--model", default=os.environ.get("PG_DETECTOR_MODEL", "yolo26s.pt"),
                        help="YOLO 모델 (.pt 또는 NCNN 폴더). 기본 yolo26s.pt (자동 다운로드)")
    parser.add_argument("--imgsz", type=int, default=int(os.environ.get("PG_DETECTOR_IMGSZ", "480")))
    parser.add_argument("--roi", default=os.environ.get("PG_ROI"), help="x1,y1,x2,y2,... (0~1). 기본값은 sample_c 기준")
    parser.add_argument("--min-conf", type=float)
    parser.add_argument("--min-area", type=float)
    parser.add_argument("--overlap-enter", type=float)
    parser.add_argument("--overlap-exit", type=float)
    parser.add_argument("--edge-clip-max-width", type=float)
    parser.add_argument("--occupied", action="store_true", help="현재 상태를 '있음'으로 두고 판정 (나갈 때 기준 적용)")
    parser.add_argument("--out", help="결과 이미지 저장 경로")
    parser.add_argument("--json", help="판정 결과 JSON 저장 경로")
    parser.add_argument("--max-width", type=int, default=1600, help="저장 이미지의 최대 가로 픽셀")
    args = parser.parse_args()

    import cv2

    config = OccupancyConfig.from_env()
    config.roi = parse_roi(args.roi)
    for name in ("min_conf", "min_area", "overlap_enter", "overlap_exit", "edge_clip_max_width"):
        value = getattr(args, name)
        if value is not None:
            setattr(config, name, value)

    if args.camera:
        from picamera2 import Picamera2

        camera = Picamera2()
        camera.configure(camera.create_still_configuration(main={"size": (1640, 1232)}))
        camera.start()
        frame = cv2.cvtColor(camera.capture_array(), cv2.COLOR_RGB2BGR)
        camera.stop()
        source = "camera"
    else:
        if not args.image:
            parser.error("사진 파일을 주거나 --camera 를 쓰세요.")
        frame = cv2.imread(args.image)
        if frame is None:
            parser.error(f"사진을 읽을 수 없습니다: {args.image}")
        source = args.image

    detector = VehicleDetector(args.model, imgsz=args.imgsz, config=config)
    boxes = detector.detect_boxes(frame)
    height, width = frame.shape[:2]
    judgement = detector.judge.judge(boxes, width, height, currently_occupied=args.occupied)

    print(f"{source}  {width}x{height}  model={args.model}  ROI={config.roi}")
    print(f"thresholds: conf>={config.min_conf} area>={config.min_area:.0%} "
          f"overlap>={config.overlap_exit if args.occupied else config.overlap_enter:.0%} "
          f"edge_clip_max_width={config.edge_clip_max_width:.0%}")
    print(f"{'#':>2} {'verdict':7} {'cls':>3} {'conf':>5} {'area':>6} {'bottom(x,y)':>14} {'inside':>6} "
          f"{'overlap':>7}  reasons")
    for i, b in enumerate(judgement.boxes):
        print(f"{i:>2} {'IN' if b.accepted else 'OUT':7} {b.cls:>3} {b.conf:5.2f} {b.area:6.1%} "
              f"{b.bottom_center[0]:6.2f},{b.bottom_center[1]:5.2f} {str(b.inside):>6} {b.overlap:7.0%}  "
              f"{'; '.join(b.reasons) or '-'}")
    print(f"=> OCCUPIED: {'YES' if judgement.occupied else 'NO'}")

    if args.out:
        image = draw(frame, judgement, config, "occupied" if args.occupied else "empty")
        if image.shape[1] > args.max_width:
            s = args.max_width / image.shape[1]
            image = cv2.resize(image, (args.max_width, int(image.shape[0] * s)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(args.out, image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print("saved", args.out)
    if args.json:
        payload = {"source": source, "size": [width, height], "config": config.to_dict(),
                   "occupied": judgement.occupied,
                   "boxes": [{**b.__dict__, "bottom_center": list(b.bottom_center)} for b in judgement.boxes]}
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
