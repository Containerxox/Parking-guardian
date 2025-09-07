import React from "react";
import "./AppBar.css";

export default function AppBar({
  brand = "Parking Guardian",
  title = "",
  onLogout, // 로그아웃 클릭 시, 실행할 함수
  sticky = true, //헤더(앱바)를 스크롤에 고정할지 말지를  T/F로 제어
  rightNode=null,
}) {
  return (
    <header className={`pg-appbar ${sticky ? "is-sticky" : ""}`}>
      <div className="pg-appbar-inner">
        {/* 좌측: 브랜드명 (Parking Guardian) */}
        <div className="pg-brand" translate="no">{brand}</div>

        {/* 중앙: 페이지 제목 */}
        <div className="pg-title" title={title}>
          {title}
        </div>

        {/* 우측: 커스텀 버튼(관리자 페이지: "사용자 관리" / 사용자 페이지: "기기 추가") + 로그아웃 버튼 */}
        <div className="pg-right">
          {rightNode}
          <button className="pg-btn-logout" onClick={onLogout}>
            로그아웃
          </button>
        </div>
      </div>
    </header>
  );
}
