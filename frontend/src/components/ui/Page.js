import React from "react";
import AppBar from "./AppBar";

// 로그인 후 모든 화면의 공통 틀: 상단 AppBar + 본문 컨테이너
export default function Page({ title, subtitle, actions, children }) {
  return (
    <div className="pg-page">
      <AppBar />
      <main className="pg-container">
        <div className="pg-page-head">
          <div>
            <h1 className="page-h1">{title}</h1>
            {subtitle && <p className="pg-sub">{subtitle}</p>}
          </div>
          {actions && <div className="pg-page-actions">{actions}</div>}
        </div>
        {children}
      </main>
    </div>
  );
}
