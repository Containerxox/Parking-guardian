import React, { useState, useEffect } from "react";
import "./UserDashboard.css"; 
import AppBar from "../../components/ui/AppBar";



export default function UserDashboard() {
  const [selectedZone, setSelectedZone] = useState("전체");
  const [violations, setViolations] = useState([]);
  const [selectedImage, setSelectedImage] = useState(null);

  // 로그인도니 사용자 ID(데모용) ( 나중에 flask fetch해서 DB 연결해야 함. )
  //const currentUserId = localStorage.getItem("userId") || "";

  const handelAddDevice = () => {
    // 사용자 ID 입력
    const inputId = window.prompt("ID를 입력하세요.");
    if(inputId === null) return;
    if(!inputId.trim()){
    alert("ID를 입력하세요."); return;
    }

    // 추가할 라즈베리파이의 시리얼 넘버 입력
    const serial = window.prompt("추가할 기기의 시리얼 넘버를 입력하세요.");
    if(serial ===null) return;
    if(!serial.trim()){
      alert("시리얼 넘버를 입력하세요.");
      return;
    }

    // 확인용=> 입력한 ID와 시리얼 넘버 alert
    // 서버 연동은 나중에 할 예정
    alert(`입력한 ID: ${inputId}\n추가할 시리얼 넘버: ${serial}\n(백엔드 연동은 추후 진행 예정!)`);
  }
  
  
  useEffect(() => {
    fetch('http://localhost:5000/violations')
      .then((res) => res.json())
      .then((data) => {
        const initialized = data.map((v) => ({ ...v, isReported: false }));
        setViolations(initialized);
      })
      .catch((err) => console.error("데이터 불러오기 실패:", err));
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
       rightNode={<button className="pg-btn" onClick={handelAddDevice}>기기 추가</button>}
       onLogout={() => alert("데모(로그아웃)")}
      />


      {/* 본문 */}
      <main className="user-container">
        <h1 className="page-h1">조선대학교 - 장애인 주차 위반 내역</h1>


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
              <div key={v.serial_number} className="violation-card">
                <p>📍 <strong>{v.zone}</strong></p>
                {/* <p>🚗 차량번호: {v.carNumber}</p> */}
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
                      src={`http://localhost:5000/uploads/${v.image}`}
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
            src={`http://localhost:5000/uploads/${selectedImage}`}
            alt="차량 이미지"
            className="img-modal-content"
          />
        </div>
      )}
    </div>
  );
}

