import React from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { homePath } from "../../utils/format";

export default function Forbidden403() {
  const { user } = useAuth();
  return (
    <div className="pg-page">
      <main className="pg-container">
        <div className="pg-card pg-card-pad">
          <h1 className="page-h1">접근 권한이 없습니다 (403)</h1>
          <p className="pg-sub">현재 계정으로는 이 페이지를 볼 수 없습니다.</p>
          <p>
            {user ? (
              <Link className="pg-link" to={homePath(user.role)}>
                내 첫 화면으로 돌아가기
              </Link>
            ) : (
              <Link className="pg-link" to="/">
                로그인 페이지로 이동
              </Link>
            )}
          </p>
        </div>
      </main>
    </div>
  );
}
