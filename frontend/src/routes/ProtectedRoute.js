import React from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../state/AuthContext";

// roles: 접근을 허용할 Role 배열 (SUPER_ADMIN, COMPANY_ADMIN). 생략하면 로그인만 확인한다.
export default function ProtectedRoute({ children, roles }) {
  const { user, ready } = useAuth();

  // 세션 복구(/auth/refresh)가 끝나기 전
  if (!ready) return <div className="pg-fullscreen-msg">로딩 중...</div>;

  // 로그인하지 않은 상태 -> 로그인 페이지
  if (!user) return <Navigate to="/" replace />;

  // Role이 맞지 않으면 403 페이지
  if (roles && !roles.includes(user.role)) {
    return <Navigate to="/403" replace />;
  }

  return children;
}
