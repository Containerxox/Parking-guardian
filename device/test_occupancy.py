"""주차면 점유 판정 규칙의 단위 테스트. 모델 없이 돈다.

    cd device
    python -m pytest test_occupancy.py -q      (backend 가상환경의 pytest, opencv, numpy 사용)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vehicle_detector import DEFAULT_ROI, OccupancyConfig, OccupancyTracker, RoiJudge, parse_roi  # noqa: E402

W, H = 640, 853   # Pi 저해상도 프레임과 같은 4:3 ... 아니라 sample 사진 비율(3:4). 판정은 정규화 좌표라 비율과 무관하다


def box(x1, y1, x2, y2, conf=0.9, cls=2):
    """정규화 좌표 -> 픽셀 박스"""
    return (x1 * W, y1 * H, x2 * W, y2 * H, conf, cls)


@pytest.fixture
def judge():
    return RoiJudge(OccupancyConfig())


def test_default_roi_is_a_quadrilateral():
    assert len(parse_roi(DEFAULT_ROI)) == 4


def test_roi_must_be_normalized_pairs():
    with pytest.raises(ValueError):
        parse_roi("0.1,0.2,0.3")
    with pytest.raises(ValueError):
        parse_roi("0.1,0.2,1.5,0.2,0.5,0.9")


def test_car_in_the_stall_is_accepted(judge):
    # sample_c 의 차량: 화면 2/3, 아랫변 중심 (0.50, 0.93)
    frame = judge.judge([box(0.02, 0.23, 0.97, 0.93, conf=0.9)], W, H)
    assert frame.occupied
    assert frame.boxes[0].accepted and frame.boxes[0].inside and frame.boxes[0].overlap > 0.4


def test_neighbor_car_clipped_at_the_edge_is_rejected(judge):
    # sample_b 의 왼쪽 옆 칸 차량: 화면 왼쪽 끝에 잘려 폭 24%, 아랫변 중심 (0.12, 0.68)
    frame = judge.judge([box(0.0, 0.42, 0.24, 0.68, conf=0.88)], W, H)
    assert not frame.occupied
    reasons = " ".join(frame.boxes[0].reasons)
    assert "clipped" in reasons or "overlap" in reasons


def test_small_car_in_the_background_is_rejected(judge):
    # sample_b 의 뒤쪽 통로 차량: 면적 2.8%
    frame = judge.judge([box(0.74, 0.44, 0.93, 0.59, conf=0.7)], W, H)
    assert not frame.occupied
    assert any("area" in r for r in frame.boxes[0].reasons)


def test_bottom_center_outside_the_stall_is_rejected(judge):
    # 큰 차가 왼쪽 칸에: 아랫변 중심 x = 0.10 은 ROI 왼쪽 선 밖 (y 0.80 에서 경계 x ≈ 0.04 ... 안쪽) -> 더 왼쪽으로
    frame = judge.judge([box(-0.6, 0.3, 0.3, 0.80, conf=0.9)], W, H)   # 잘려서 x1=0, 아랫변 중심 x=0.15
    judged = frame.boxes[0]
    assert judged.xyxy[0] == 0
    # 잘린 폭 30% < 35% 이므로 옆 칸 차량으로 제외된다
    assert not judged.accepted and judged.edge_clipped


def test_low_confidence_is_rejected(judge):
    frame = judge.judge([box(0.02, 0.23, 0.97, 0.93, conf=0.2)], W, H)
    assert not frame.occupied and any("conf" in r for r in frame.boxes[0].reasons)


def test_overlap_threshold_is_lower_while_occupied():
    config = OccupancyConfig(overlap_enter=0.30, overlap_exit=0.15)
    judge = RoiJudge(config)
    # 경계에 걸친 차: 박스의 1/4 쯤만 ROI 와 겹친다 (폭 55% 라 가장자리 잘림 규칙에는 걸리지 않는다)
    straddling = box(0.0, 0.15, 0.55, 0.75, conf=0.9)
    empty_state = judge.judge([straddling], W, H, currently_occupied=False)
    occupied_state = judge.judge([straddling], W, H, currently_occupied=True)
    overlap = empty_state.boxes[0].overlap
    assert 0.15 <= overlap < 0.30, overlap          # 테스트 전제: 두 기준 사이의 값
    assert not empty_state.occupied                  # 비어 있을 때는 아직 "있음"이 아니고
    assert occupied_state.occupied                   # 있을 때는 아직 "없음"이 아니다 (이력 현상)


def test_passing_car_does_not_flip_the_state():
    tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))
    for _ in range(3):
        tracker.update(False)
    assert tracker.state is False
    tracker.update(True)          # 지나가는 차: 1~2 프레임
    tracker.update(True)
    assert tracker.state is False
    tracker.update(False)
    assert tracker.state is False and tracker.transitions == 1


def test_three_frames_enter_six_frames_exit():
    tracker = OccupancyTracker(OccupancyConfig(enter_frames=3, exit_frames=6))
    assert [tracker.update(True) for _ in range(3)] == [None, None, True]
    assert tracker.changed()
    results = [tracker.update(False) for _ in range(6)]
    assert results[:5] == [True] * 5 and results[5] is False
    # 잠깐 가려진 것(없음 4프레임 뒤 다시 있음)은 상태를 바꾸지 않는다
    for _ in range(4):
        tracker.update(True)
    assert tracker.state is True
    for _ in range(4):
        tracker.update(False)
    tracker.update(True)
    assert tracker.state is True and tracker.transitions == 3
