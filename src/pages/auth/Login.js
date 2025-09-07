import React, { useState } from "react";
import "./Login.css"; 
import logo from "../../logo.png";

export default function Login() {
  const [id, setId] = useState("");
  const [pw, setPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState("");

  const onSubmit = (e) => {
    e.preventDefault(); // 페이지 새로고침 방지.
    setError("");

    if (!id.trim() || !pw.trim()) {
      setError("아이디와 비밀번호를 입력해주세요.");
      return;
    }

    // 여기에 실제 로그인 API 연동 예정 !
    // if (id === "admin") navigate("/admin"); else navigate("/user");

    console.log({ id, pw });
    alert("로그인 요청 전송(데모).\n콘솔에 입력한 id,pw 값 확인");
  };

  return (
    <div className="login-wrap">
      <div className="login-bg" />

      <div className="login-card">
        <div className="login-header">
          <img src={logo} alt="Parking Guardian 로고" className="logo-img" />
          <h1 className="title" translate="no">Parking Guadian</h1>
          <p className="subtitle">장애인 주차 위반 감지 시스템</p>
        </div>

        <form onSubmit={onSubmit} className="form">
          <label className="label">
            <span>아이디</span>
            <input
              type="text"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="ID"
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
                placeholder="Password"
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
          
          {/* &&(AND 연산자)는 앞이 true일 때만 뒤를 실행함. 즉, error값이 존재해야 errobox 렌더링함 */}
          {error && <div className="error-box">{error}</div>}

            <button type="submit" className="main-login-btn">
            로그인
            </button>
        </form>
      </div>
    </div>
  );
}

