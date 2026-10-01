// 표시용 변환 모음. 상태 값은 docs/design/API.md 기준.

export const VIOLATION_STATUS_LABEL = {
  DETECTED: "탐지됨",
  NOTIFIED: "알림 전송됨",
  IN_PROGRESS: "현장 대응 중",
  RESOLVED: "처리 완료",
};

export const VIOLATION_STATUS_TONE = {
  DETECTED: "red",
  NOTIFIED: "amber",
  IN_PROGRESS: "blue",
  RESOLVED: "green",
};

export const HISTORY_ACTION_LABEL = {
  DETECTED: "탐지",
  NOTIFIED: "알림 전송",
  ACKNOWLEDGED: "확인 (현장 대응 시작)",
  RESOLVED: "처리 완료",
  REOPENED: "상태 복구",
  IMAGE_UPLOAD_FAILED: "이미지 저장 실패",
  NOTIFY_FAILED: "알림 전송 실패",
  VEHICLE_LEFT: "차량 이탈",
  SIGNAL_LOST: "신호 끊김",
};

export const ACTIVE_STATUS_LABEL = {
  ACTIVE: "활성",
  INACTIVE: "비활성",
};

export const ROLE_LABEL = {
  SUPER_ADMIN: "전체 관리자",
  COMPANY_ADMIN: "회사 관리자",
};

// UTC ISO 8601 문자열을 브라우저 현지 시각으로 보여 준다.
export function formatDateTime(iso) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// 위반 차량이 아직 그 자리에 있는지 한 줄로 설명한다.
// 장비가 1분마다 올리는 사진은 같은 위반으로 묶이고, 차량이 나가면 ended_at 이 채워진다.
export function vehiclePresenceText(violation) {
  if (!violation) return "-";
  if (violation.vehicle_present) {
    return `주차 중 (마지막 확인 ${formatDateTime(violation.last_seen_at || violation.detected_at)})`;
  }
  const when = formatDateTime(violation.ended_at);
  if (violation.end_reason === "STICKER_DETECTED") return `스티커 탐지로 종료 (${when})`;
  if (violation.end_reason === "SIGNAL_LOST") return `신호 끊김 (${when})`;
  return `차량 이탈 (${when})`;
}

// Role별 첫 화면
export function homePath(role) {
  return role === "SUPER_ADMIN" ? "/super/companies" : "/violations";
}
