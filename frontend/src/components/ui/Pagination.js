import React from "react";

export default function Pagination({ page, size, total, onChange, disabled = false }) {
  const totalPages = Math.max(1, Math.ceil((total || 0) / (size || 1)));
  return (
    <div className="pg-pagination">
      <button
        type="button"
        className="btn btn-sm"
        disabled={disabled || page <= 1}
        onClick={() => onChange(page - 1)}
      >
        이전
      </button>
      <span>
        {page} / {totalPages} 페이지 (총 {total || 0}건)
      </span>
      <button
        type="button"
        className="btn btn-sm"
        disabled={disabled || page >= totalPages}
        onClick={() => onChange(page + 1)}
      >
        다음
      </button>
    </div>
  );
}
