import React, { useState } from "react";
import "./UserRegistration.css";

export default function UserRegistration() {
  const [form, setForm] = useState({
    userId: "",
    password: "",
    building: "",
    address:""
  });

  const handleChange = (e) => {
    setForm({
      ...form,
      [e.target.name]: e.target.value,
    });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    alert(
      `ID: ${form.userId}\nPW: ${form.password}\n건물명: ${form.building}\n주소: ${form.address}\n(백엔드 연동은 추후 진행 예정!)`
    );
  };

  return (
    <div className="login-root">
      <div className="login-box">
        <h2 className="login-title">고객 회원가입</h2>
        <form onSubmit={handleSubmit}>
          <input
            type="text"
            name="userId"
            placeholder="아이디"
            value={form.userId}
            onChange={handleChange}
            className="login-input"
          />
          <input
            type="password"
            name="password"
            placeholder="비밀번호"
            value={form.password}
            onChange={handleChange}
            className="login-input"
          />
          <input
            type="text"
            name="building"
            placeholder="건물명"
            value={form.building}
            onChange={handleChange}
            className="login-input"
          />
          <input
            type="text"
            name="address"
            placeholder="주소"
            value={form.address}
            onChange={handleChange}
            className="login-input"
          />
          <button type="submit" className="login-btn">
            회원가입
          </button>
        </form>
      </div>
    </div>
  );
}

