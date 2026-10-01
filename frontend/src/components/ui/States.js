import React from "react";

export function Loading({ text = "불러오는 중..." }) {
  return <div className="pg-loading">{text}</div>;
}

export function ErrorBox({ message, onRetry }) {
  if (!message) return null;
  return (
    <div className="pg-error" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          다시 시도
        </button>
      )}
    </div>
  );
}

export function InfoBox({ message }) {
  if (!message) return null;
  return (
    <div className="pg-info" role="status">
      {message}
    </div>
  );
}
