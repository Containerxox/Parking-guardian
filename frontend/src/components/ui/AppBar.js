import React, { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import "./AppBar.css";
import { useAuth } from "../../state/AuthContext";
import { notificationsApi } from "../../api/client";
import { NOTIFICATIONS_CHANGED } from "../../utils/notificationEvents";
import { ROLE_LABEL, homePath } from "../../utils/format";

const POLL_MS = 15000;

const NAV_BY_ROLE = {
  SUPER_ADMIN: [
    { to: "/super/companies", label: "회사 관리" },
    { to: "/super/violations", label: "위반 내역" },
    { to: "/super/machines", label: "장비" },
    { to: "/super/audit-logs", label: "감사 로그" },
  ],
  COMPANY_ADMIN: [
    { to: "/violations", label: "위반 내역" },
    { to: "/parking-lots", label: "주차장" },
  ],
};

// 읽지 않은 알림 수를 15초마다 확인한다.
function useUnreadCount(enabled) {
  const [count, setCount] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const tick = () => {
      notificationsApi.list({ unread: true, size: 1 }).then(
        (data) => {
          if (!alive) return;
          setCount((data && data.unread_count) || 0);
          setFailed(false);
        },
        () => {
          if (alive) setFailed(true);
        }
      );
    };
    tick();
    const timer = setInterval(tick, POLL_MS);
    window.addEventListener(NOTIFICATIONS_CHANGED, tick);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener(NOTIFICATIONS_CHANGED, tick);
    };
  }, [enabled]);

  return { count, failed };
}

function BellIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

export default function AppBar({ brand = "Parking Guardian", sticky = true }) {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const role = user ? user.role : null;
  const isCompanyAdmin = role === "COMPANY_ADMIN";
  const { count, failed } = useUnreadCount(isCompanyAdmin);
  const links = NAV_BY_ROLE[role] || [];

  const handleLogout = async () => {
    await logout();
    navigate("/", { replace: true });
  };

  let bellTitle = "읽지 않은 알림 없음";
  if (failed) bellTitle = "알림 수를 확인하지 못했습니다.";
  else if (count > 0) bellTitle = `읽지 않은 알림 ${count}건`;

  return (
    <header className={`pg-appbar ${sticky ? "is-sticky" : ""}`}>
      <div className="pg-appbar-inner">
        {/* 좌측: 브랜드명 */}
        <NavLink to={homePath(role)} className="pg-brand" translate="no">
          {brand}
        </NavLink>

        {/* 중앙: Role에 따른 메뉴 */}
        <nav className="pg-nav" aria-label="주 메뉴">
          {links.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) => `pg-nav-link ${isActive ? "active" : ""}`}
            >
              {link.label}
            </NavLink>
          ))}
        </nav>

        {/* 우측: 알림, 로그인한 관리자, 로그아웃 */}
        <div className="pg-right">
          {isCompanyAdmin && (
            <button
              type="button"
              className={`pg-bell ${failed ? "is-failed" : ""}`}
              onClick={() => navigate("/notifications")}
              title={bellTitle}
              aria-label={bellTitle}
            >
              <BellIcon />
              {failed && <span className="pg-bell-count is-failed">!</span>}
              {!failed && count > 0 && (
                <span className="pg-bell-count">{count > 99 ? "99+" : count}</span>
              )}
            </button>
          )}
          {user && (
            <div className="pg-user" title={user.email}>
              <span className="pg-user-name">{user.name}</span>
              <span className="pg-user-company">
                {user.company_name || ROLE_LABEL[user.role] || ""}
              </span>
            </div>
          )}
          <button type="button" className="pg-btn-logout" onClick={handleLogout}>
            로그아웃
          </button>
        </div>
      </div>
    </header>
  );
}
