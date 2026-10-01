import React, { useState } from "react";
import Modal from "../ui/Modal";

// 짧은 글을 입력받아 onSubmit(text) 을 실행하는 작은 Modal.
// onSubmit 이 실패(throw)하면 오류 문구를 Modal 안에 보여 준다.
export default function NoteModal({
  title,
  description,
  label,
  placeholder = "",
  required = false,
  submitLabel,
  onSubmit,
  onClose,
}) {
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    const value = text.trim();
    if (required && !value) {
      setError(`${label}을(를) 입력해 주세요.`);
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSubmit(value);
    } catch (err) {
      setError(err.message || "요청을 처리하지 못했습니다.");
      setSaving(false);
    }
  };

  return (
    <Modal title={title} onClose={onClose}>
      <form onSubmit={handleSubmit}>
        {description && <p className="pg-sub" style={{ marginBottom: 12 }}>{description}</p>}
        <label className="pg-field">
          <span>
            {label} {required ? "(필수)" : "(선택)"}
          </span>
          <textarea
            className="pg-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={placeholder}
            autoFocus
          />
        </label>
        {error && (
          <div className="pg-error" role="alert">
            {error}
          </div>
        )}
        <div className="pg-form-row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            취소
          </button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "처리 중..." : submitLabel}
          </button>
        </div>
      </form>
    </Modal>
  );
}
