import React, { useState } from "react";
import "./UserRegistration.css";
import { useNavigate } from "react-router-dom";

// =========================================================================
// 클라우드 (배포용)
// const API_BASE = "https://capston-bajen.run.goorm.site";
// =========================================================================

// 로컬 (개발용)
const API_BASE = "http://localhost:5000";


export default function UserRegistration() {
  const navigate = useNavigate();
  const [id,setId] = useState("");
  const [pw,setPw] = useState("");
  const [building,setBuilding]=useState("");
  const [address,setAddress] = useState("");


  const handleSubmit = async (e) => {
    e.preventDefault();

    if(!id.trim() || !pw.trim() || !building.trim() || !address.trim()){
      alert("아이디, 비밀번호, 건물명, 건물 주소를 입력해주세요.");
      return;
    }

    // 회원가입 API 연동
    try{
      const res = await fetch(`${API_BASE}/user-register`, {
        method:"POST",
        headers: {"Content-Type": "application/json"},
        body:JSON.stringify({user_id:id,password:pw, building_name: building, building_address:address}),
      });

      const data = await res.json();

      if(data.ok){
        navigate("/admin-dashboard" );
        alert(data.message);
      }else{
        alert(data.error);
      }

    }
    catch (err){
      console.error("서버 오류:",err);
    }
  };

  return (
    <div className="register-root">
      <div className="register-box">
        <h2 className="register-title">고객 회원가입</h2>
        <form onSubmit={handleSubmit}>
          <input
            type="text"
            name="userId"
            placeholder="아이디"
            value={id}
            onChange={(e) => setId(e.target.value)}
            className="register-input"
          />
          <input
            type="password"
            name="password"
            placeholder="비밀번호"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            className="register-input"
          />
          <input
            type="text"
            name="building"
            placeholder="건물명"
            value={building}
            onChange={(e) => setBuilding(e.target.value)}
            className="register-input"
          />
          <input
            type="text"
            name="address"
            placeholder="주소"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            className="register-input"
          />
          <button type="submit" className="register-btn">
            회원가입
          </button>
        </form>
      </div>
    </div>
  );
}

