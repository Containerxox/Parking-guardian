import React, { useState, useMemo } from "react";
import "./AdminDashboard.css";
import AppBar from "../../components/ui/AppBar";
import { useNavigate } from "react-router-dom";

export default function AdminDashboard() {
  // 하드코딩 더미 데이터
  const [rows, setRows] = useState([
    { buildingId: 102, address: "서울시 송파구 올림픽로 45", deviceCount: 2 },
    { buildingId: 201, address: "경기도 성남시 판교역로 10", deviceCount: 7 },
    { buildingId: 301, address: "부산광역시 해운대구 A로 77", deviceCount: 3 },
  ]);

  // 검색 관련
  const [searchField, setSearchField] = useState("buildingId"); // 검색 옵션
  const [query, setQuery] = useState(""); //검색어
  const [appliedQuery, setAppliedQuery] = useState({ field: "", value: "" }); // 검색 옵션 + 검색어

  // 검색 & 초기화
  const handleSearch = () => {
    setAppliedQuery({ field: searchField, value: query.trim().toLowerCase() });
  };
  const handleReset = () => {
    setQuery("");
    setAppliedQuery({ field: "", value: "" });
  };

  // idx  + 검색 필터 적용
  const viewRows = useMemo(() => {
    const base = !appliedQuery.value
      ? rows // 검색어 X 경우 -> 전체 row 그대로 사용
      : rows.filter((r) => { // 검색어 존재 경우 -> 필터링
          if (appliedQuery.field === "buildingId") {
            return String(r.buildingId).includes(appliedQuery.value);
          }
          if (appliedQuery.field === "address") {
            return r.address.toLowerCase().includes(appliedQuery.value);
          }
          if (appliedQuery.field === "deviceCount") {
            return String(r.deviceCount).includes(appliedQuery.value);
          }
          return true;
        });
    return base.map((r, i) => ({ idx: i + 1, ...r }));
  }, [rows, appliedQuery]);

  const handleDelete = (buildingId) => {
    if (!window.confirm(`건물ID ${buildingId}를 삭제할까요?`)) return;
    setRows((prev) => prev.filter((r) => r.buildingId !== buildingId));
  };

  const navigate = useNavigate();
  return (
    <div className="admin-root">
      {/* 상단 AppBar */}
     <AppBar
     title="관리자 페이지 - 건물 목록"
     rightNode={<button className="pg-btn" onClick={() => navigate("/user-registration")}>회원 관리</button>}
     onLogout={() => alert("데모(로그아웃)")}
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
                    <tr key={r.buildingId}>
                      <td className="center">{r.idx}</td>
                      <td className="center">{r.buildingId}</td>
                      <td className="center">{r.address}</td>
                      <td className="center">{r.deviceCount}</td>
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
