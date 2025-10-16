import React, { useState, useMemo, useEffect } from "react";
import "./AdminDashboard.css";
import AppBar from "../../components/ui/AppBar";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";


// =========================================================================
// 클라우드 (배포용)
// const API_BASE = "https://capston-bajen.run.goorm.site";
// =========================================================================

// 로컬 (개발용)
const API_BASE = "http://localhost:5000";


export default function AdminDashboard() {
  const navigate = useNavigate();
  const { logout } = useAuth() || {};

  // 로그아웃 처리
  const handleLogout = async () =>{
    try{
      if(logout){
        await logout(); // 서버의 /logout 호출 & 전역 user=null
      }
    }finally{
      navigate("/", {replace:true});
    }
  };

  // 서버에서 받아올 실제 데이터
  // row 형태: {idx, user_id, building_id, address, device_count}
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  // 검색 관련
  const [searchField, setSearchField] = useState("building_id"); // 검색 옵션
  const [query, setQuery] = useState(""); //검색어
  const [appliedQuery, setAppliedQuery] = useState({ field: "", value: "" }); // 검색 옵션 + 검색어

  // API 호출하여 데이터 로드 
  useEffect(() => {
    (async () => {
      try{
        const res = await fetch(`${API_BASE}/admin/users-devices`,{
          credentials:"include", //세션쿠키 포함
        });
        
        const data = await res.json();
        if(!data.ok){
          alert(data.error);
          navigate("/", {replace:true});
          return;
        }
        setRows(data.rows || []);
      }catch(e){
        console.error(e);
        alert("서버 통신 오류");
      }finally{
        setLoading(false);
      }
    })();
  },[navigate]);

  // 검색 적용
  const handleSearch = () => {
    setAppliedQuery({ field: searchField, value: query.trim().toLowerCase() });
  };

  // 검색 초기화
  const handleReset = () => {
    setQuery("");
    setAppliedQuery({ field: "", value: "" });
  };

  // 화면에 표현할 목록 (검색 적용하여)
  const viewRows = useMemo(() => {
    if (!appliedQuery.value) return rows;
    const val = appliedQuery.value;
    return rows.filter((r) => {
      if (appliedQuery.field === "user_id") {
        return String(r.user_id ?? "").includes(val);
      }
      if (appliedQuery.field === "building_id") {
        return String(r.building_id ?? "").includes(val);
      }
      if (appliedQuery.field === "address") {
        return String(r.address ?? "").toLowerCase().includes(val);
      }
      if (appliedQuery.field === "device_count") {
        return String(r.device_count ?? 0).includes(val);
      }
      return true;
    });
  }, [rows, appliedQuery]);


  const handleDelete = (building_id) => {
    if (!window.confirm(`건물ID ${building_id}를 삭제할까요?`)) return;
    setRows((prev) => prev.filter((r) => r.building_id !== building_id));
  };



    if (loading) {
    return (
      <div className="admin-root">
        <AppBar title="관리자 페이지 - 건물 목록" onLogout={handleLogout} />
        <main className="admin-container">로딩중…</main>
      </div>
    );
  }

  return (
    <div className="admin-root">
      {/* 상단 AppBar */}
     <AppBar
     title="관리자 페이지 - 건물 목록"
     rightNode={<button className="pg-btn" onClick={() => navigate("/user-registration")}>회원 관리</button>}
     onLogout={handleLogout}
     />

      {/* 본문 */}
      <main className="admin-container">
        {/* 검색 영역 */}
        <div className="admin-search">
          <select
            value={searchField}
            onChange={(e) => setSearchField(e.target.value)}
            className="admin-select"
          >
            <option value="user_id">사용자 ID</option>
            <option value="buildingId">건물 ID</option>
            <option value="address">주소</option>
            <option value="deviceCount">설치 기기 수</option>
          </select>

          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="검색어 입력"
            className="admin-input"
          />

          <button onClick={handleSearch} className="btn btn-primary">
            검색
          </button>
          <button onClick={handleReset} className="btn btn-ghost">
            초기화
          </button>
        </div>

        {/* 표 */}
        <div className="admin-card">
          <div className="table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>idx</th>
                  <th>사용자ID</th>
                  <th>건물 ID</th>
                  <th>주소</th>
                  <th>설치 기기 수</th>
                  <th>관리</th>
                </tr>
              </thead>
              <tbody>
                {viewRows.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="empty">
                      검색 결과가 없습니다.
                    </td>
                  </tr>
                ) : (
                  viewRows.map((r) => (
                    <tr key={`${r.idx}-${r.user_id}`}> 
                      <td className="center">{r.idx}</td>
                      <td className="center" translate="no">{r.user_id}</td>
                      <td className="center" translate="no">{r.building_id ?? "-"}</td>
                      <td className="center" translate="no">{r.address ?? "-"}</td>
                      <td className="center">{(r.device_count ?? 0)}</td>
                      <td className="center">
                        <button
                          onClick={() => handleDelete(r.buildingId)}
                          className="btn btn-danger"
                        >
                          삭제
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  );
}
