// 알림을 읽음 처리했을 때 AppBar의 종 모양 숫자를 바로 갱신하기 위한 이벤트.
export const NOTIFICATIONS_CHANGED = "pg:notifications-changed";

export function notifyNotificationsChanged() {
  window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED));
}
