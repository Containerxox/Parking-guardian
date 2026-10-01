import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Page from "../../components/ui/Page";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ViolationStatusBadge } from "../../components/ui/Badge";
import Pagination from "../../components/ui/Pagination";
import useLoad from "../../hooks/useLoad";
import { notificationsApi } from "../../api/client";
import { notifyNotificationsChanged } from "../../utils/notificationEvents";
import { formatDateTime } from "../../utils/format";

const PAGE_SIZE = 20;

export default function Notifications() {
  const navigate = useNavigate();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);

  const loader = useCallback(
    () => notificationsApi.list({ unread: unreadOnly ? true : undefined, page, size: PAGE_SIZE }),
    [unreadOnly, page]
  );
  const { data, loading, error, reload } = useLoad(loader);

  const items = (data && data.items) || [];
  const unreadCount = (data && data.unread_count) || 0;

  // 읽음 처리로 현재 페이지가 비면 마지막 페이지로 당긴다.
  useEffect(() => {
    if (!data || loading) return;
    const lastPage = Math.max(1, Math.ceil((data.total || 0) / (data.size || PAGE_SIZE)));
    if (page > lastPage) setPage(lastPage);
  }, [data, loading, page]);

  const afterChange = () => {
    notifyNotificationsChanged(); // AppBar 의 종 숫자 갱신
    reload();
  };

  const markRead = async (notification) => {
    setBusy(true);
    setActionError("");
    try {
      await notificationsApi.read(notification.id);
      afterChange();
    } catch (err) {
      setActionError(err.message || "읽음 처리하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const markAllRead = async () => {
    setBusy(true);
    setActionError("");
    try {
      await notificationsApi.readAll();
      afterChange();
    } catch (err) {
      setActionError(err.message || "모두 읽음 처리하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  // 알림을 누르면 읽음 처리한 뒤 위반 내역으로 이동한다.
  const openNotification = async (notification) => {
    setActionError("");
    if (!notification.read_at) {
      setBusy(true);
      try {
        await notificationsApi.read(notification.id);
        notifyNotificationsChanged();
      } catch (err) {
        setActionError(err.message || "읽음 처리하지 못했습니다.");
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    navigate("/violations", { state: { violationId: notification.violation_id } });
  };

  return (
    <Page
      title="알림"
      subtitle={`읽지 않은 알림 ${unreadCount}건`}
      actions={
        <button type="button" className="btn btn-primary" onClick={markAllRead} disabled={busy || unreadCount === 0}>
          모두 읽음
        </button>
      }
    >
      <div className="pg-toolbar">
        <label className="pg-check">
          <input
            type="checkbox"
            checked={unreadOnly}
            onChange={(e) => {
              setUnreadOnly(e.target.checked);
              setPage(1);
            }}
          />
          읽지 않은 알림만 보기
        </label>
        <div className="pg-spacer" />
        <button type="button" className="btn" onClick={reload} disabled={loading}>
          새로고침
        </button>
      </div>

      <ErrorBox message={actionError} />
      <ErrorBox message={error} onRetry={reload} />
      {loading && !data && <Loading />}

      {data && (
        <>
          <div className="pg-card">
            {items.length === 0 ? (
              <div className="pg-empty">{unreadOnly ? "읽지 않은 알림이 없습니다." : "알림이 없습니다."}</div>
            ) : (
              <ul className="noti-list">
                {items.map((n) => {
                  const unread = !n.read_at;
                  return (
                    <li key={n.id} className={`noti-item ${unread ? "unread" : ""}`}>
                      <button type="button" className="noti-main" onClick={() => openNotification(n)} disabled={busy}>
                        <div className="noti-title">
                          {n.parking_lot_name || "-"} {n.zone_name ? `· ${n.zone_name}` : ""} 불법주차 의심 차량 탐지
                        </div>
                        <div className="noti-meta">
                          <span>탐지 시각 {formatDateTime(n.detected_at)}</span>
                          <ViolationStatusBadge status={n.violation_status} />
                          <span>{unread ? "읽지 않음" : `읽음 ${formatDateTime(n.read_at)}`}</span>
                        </div>
                      </button>
                      {unread && (
                        <button type="button" className="btn btn-sm" onClick={() => markRead(n)} disabled={busy}>
                          읽음
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <Pagination
            page={page}
            size={data.size || PAGE_SIZE}
            total={data.total || 0}
            onChange={setPage}
            disabled={loading}
          />
        </>
      )}
    </Page>
  );
}
