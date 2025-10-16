import React, { useState, useEffect } from "react";
import "./UserDashboard.css"; 
import AppBar from "../../components/ui/AppBar";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";


// =========================================================================
// 클라우드 (배포용)
// const API_BASE = "https://capston-bajen.run.goorm.site";
// =========================================================================

// 로컬 (개발용)
const API_BASE = "http://localhost:5000";

// session에서 현재 로그인한 사용자ID 얻기
async function getSessionUserId() {
  const res = await fetch(`${API_BASE}/session`, {
    method: "GET",
    credentials: "include", //세션 쿠키 포함
  });
  const data = await res.json();
  if (!data.ok || !data.user) return null;
  return data.user.user_id;
}

export default function UserDashboard() {
  const navigate = useNavigate();
  const {logout} = useAuth() || {};

  const [selectedZone, setSelectedZone] = useState("전체");
  const [violations, setViolations] = useState([]);
  const [selectedImage, setSelectedImage] = useState(null);
  const [buildingName, setBuildingName] = useState("");


  const handleAddDevice = async() => {
       try{
        // 1) 사용자 ID 입력받기
        const inputId = window.prompt("ID를 입력하세요.");
        if (inputId == null) return;
        const trimmedId = (inputId || "").trim();
        if(!trimmedId){
          alert("ID를 입력하세요."); return;
      }

      // 2) session에 저장된 현재 로그인된 사용자ID와 입력한 사용자ID 일치 여부 확인 
      const sessionUserId = await getSessionUserId();
      if(trimmedId !== sessionUserId){
        alert(`입력한 ID가 올바르지 않습니다.`); return;
      }

      // 3) 장치의 시리얼 넘버 입력
      const serial = window.prompt("추가할 장치의 시리얼 번호를 입력하세요.")
      if(serial == null) return;
      const serialTrimmed = (serial || "").trim();
      if(!serialTrimmed){
        alert("장치의 시리얼 번호를 입력하세요."); return;
      }

      // 4) /machine-register API 호출하여 장치 추가 등록하기
      const res = await fetch(`${API_BASE}/machine-register`,{
        method:"POST",
        credentials: "include", // 세션 쿠키 포함
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          username:sessionUserId,
          machine_id: serialTrimmed,
        }),
      });

      const data = await res.json();
      if(data.ok) {
        alert("장치 등록이 완료되었습니다.");
      }else{
        alert(data.error);
      }
    }catch(e){
      console.error(e);
      alert("요청 처리 중 요류가 발생했습니다.");
    }
  };

  // 로그아웃 처리
  const handleLogout = async () => {
    try{
      if(logout){
        await logout();  // 서버의 /logout 호출 & 전역 user=null
      }
    }finally{
      navigate("/", {replace:true});
    }
  };

  
  
  useEffect(() => {
  fetch(`${API_BASE}/violations`, { credentials: "include" })
    .then((res) =>
      res.json().then((data) => ({ ok: res.ok, data }))
    )
    .then(({ ok, data }) => {
      // HTTP 오류(500/404/등) → 서버가 준 error 우선 표시
      if (!ok) {
        alert((data && data.error) || "요청 실패");
        return;
      }
      // 서버 표준 에러 형태 { ok:false, error } 대응 (혹시 있을 경우)
      if (data && data.ok === false) {
        alert(data.error || "오류가 발생했습니다.");
        return;
      }
      // 정상: 배열 기대
      const rows = Array.isArray(data) ? data : [];
      const initialized = rows.map((v) => ({ ...v, isReported: false }));
      setViolations(initialized);

      if (rows.length > 0) setBuildingName(rows[0].building_name || ""); 
    })
    .catch((err) => {
      console.error("데이터 불러오기 실패:", err);
      alert("서버 통신 오류");
    });
}, []);


  const toggleReported = (serial_number) => {
    setViolations((prev) =>
      prev.map((v) => (v.serial_number === serial_number ? { ...v, isReported: !v.isReported } : v))
    );
  };

  const filtered =
    selectedZone === "전체"
      ? violations
      : violations.filter((v) => v.zone === selectedZone);

  return (
    <div className="user-root">
      {/* 상단 AppBar */}
      <AppBar
       title="장애인 주차 구역 위반 감지 시스템"
       rightNode={<button className="pg-btn" onClick={handleAddDevice}>기기 추가</button>}
       onLogout={handleLogout}
      />


      {/* 본문 */}
      <main className="user-container">
        <h1 className="page-h1">{(buildingName)} - 장애인 주차 위반 내역</h1>


        {/* 구역 필터 */}
        <div className="zone-filters">
          {["전체", "A구역", "B구역", "C구역"].map((zone) => (
            <button
              key={zone}
              onClick={() => setSelectedZone(zone)}
              className={`zone-btn ${selectedZone === zone ? "active" : ""}`}
            >
              {zone}
            </button>
          ))}
        </div>

        {/* 위반내역 */}
        <div className="violations-grid">
          {filtered.length === 0 ? (
            <div className="empty-grid">해당 구역에 위반 기록이 없습니다.</div>
          ) : (
            filtered.map((v) => (
              <div key={`${v.serial_number}-${v.time}`} className="violation-card">
                <p>📍 <strong>{v.zone}</strong></p>
                <p>⏰ 시간: {v.time}</p>

                <label className="reported-check" translate="no">
                  <input
                    type="checkbox"
                    checked={v.isReported}
                    onChange={() => toggleReported(v.serial_number)}
                  />
                  신고 완료
                </label>

                {v.image && (
                  <div className="thumb-wrap">
                    <img
                      src={`${API_BASE}/uploads/${v.image}`}
                      alt="차량 이미지"
                      onClick={() => setSelectedImage(v.image)}
                      className="thumb-img"
                    />
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </main>

      {/* 모달 */}
      {selectedImage && (
        <div className="img-modal" onClick={() => setSelectedImage(null)}>
          <img
            src={`${API_BASE}/uploads/${selectedImage}`}
            alt="차량 이미지"
            className="img-modal-content"
          />
        </div>
      )}
    </div>
  );
}

