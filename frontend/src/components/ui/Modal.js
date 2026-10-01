import React, { useEffect } from "react";

// 배경을 누르거나 Esc를 누르면 닫힌다.
// dismissible=false 이면 본문 안의 버튼으로만 닫을 수 있다 (실수로 닫히면 안 되는 내용용).
export default function Modal({ title, onClose, wide = false, dismissible = true, children, footer }) {
  useEffect(() => {
    if (!dismissible) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, dismissible]);

  return (
    <div className="pg-modal-overlay" onClick={dismissible ? onClose : undefined}>
      <div
        className={`pg-modal ${wide ? "is-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="pg-modal-head">
          <h2 className="pg-h2">{title}</h2>
          {dismissible && (
            <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
              닫기
            </button>
          )}
        </div>
        <div className="pg-modal-body">{children}</div>
        {footer && <div className="pg-modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
