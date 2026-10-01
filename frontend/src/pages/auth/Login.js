import React, { useState } from "react";
import "./Login.css";
import logo from "../../logo.png";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { homePath } from "../../utils/format";

export default function Login() {
  const navigate = useNavigate();
  const { user, ready, login } = useAuth();

  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // 이미 로그인한 상태라면 Role에 맞는 첫 화면으로 보낸다.
  if (ready && user) return <Navigate to={homePath(user.role)} replace />;

  const onSubmit = async (e) => {
    e.preventDefault(); // 페이지 새로고침 방지
    setError("");

    if (!email.trim() || !pw) {
      setError("이메일과 비밀번호를 입력해주세요.");
      return;
    }

    setSubmitting(true);
    try {
      const admin = await login(email.trim(), pw);
      navigate(homePath(admin.role), { replace: true });
    } catch (err) {
      setError(err.message || "로그인에 실패하였습니다.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="login-bg" />

      <div className="login-card">
        <div className="login-header">
          <img src={logo} alt="Parking Guardian 로고" className="logo-img" />
          <h1 className="title" translate="no">Parking Guardian</h1>
          <p className="subtitle">장애인 주차 위반 감지 시스템</p>
        </div>

        <form onSubmit={onSubmit} className="form" noValidate>
          <label className="label">
            <span>이메일</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="이메일"
              autoComplete="username"
              className="input"
            />
          </label>

          <label className="label">
            <span>비밀번호</span>
            <div className="input-group">
              <input
                type={showPw ? "text" : "password"}
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                placeholder="비밀번호"
                autoComplete="current-password"
                className="input input-has-button"
              />
              <button
                type="button"
                onClick={() => setShowPw(!showPw)}
                className="hidden-btn show-btn"
              >
                {showPw ? "숨김" : "보기"}
              </button>
            </div>
          </label>

          {error && <div className="error-box" role="alert">{error}</div>}
          {!ready && <div className="login-hint">이전 로그인 상태를 확인하는 중...</div>}

          <button type="submit" className="main-login-btn" disabled={submitting}>
            {submitting ? "로그인 중..." : "로그인"}
          </button>
        </form>
      </div>
    </div>
  );
}
