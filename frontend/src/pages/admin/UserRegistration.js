import React, { useState } from "react";
import "./UserRegistration.css";
import { useNavigate } from "react-router-dom";

export default function UserRegistration() {
  const navigate = useNavigate();
  const [id,setId] = useState("");
  const [pw,setPw] = useState("");
  const [building,setBuilding]=useState("");
  const [address,setAddress] = useState("");


  const handleSubmit = async (e) => {
    e.preventDefault();
    // alert(
    //   `ID: ${form.userId}\nPW: ${form.password}\n건물명: ${form.building}\n주소: ${form.address}\n(백엔드 연동은 추후 진행 예정!)`
    // );
    if(!id.trim() || !pw.trim() || !building.trim() || !address.trim()){
      alert("아이디, 비밀번호, 건물명, 건물 주소를 입력해주세요.");
      return;
    }

    // 회원가입 API 연동
    try{
      const res = await fetch('http://localhost:5000/user-register', {
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

