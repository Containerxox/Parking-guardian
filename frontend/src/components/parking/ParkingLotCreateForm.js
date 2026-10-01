import React, { useState } from "react";
import { parkingLotsApi } from "../../api/client";

// 주차장 등록 폼. companyId 는 SUPER_ADMIN 이 특정 회사에 등록할 때만 넘긴다.
export default function ParkingLotCreateForm({ companyId, onCreated }) {
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!name.trim() || !address.trim()) {
      setError("주차장 이름과 주소를 입력해 주세요.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const body = { name: name.trim(), address: address.trim() };
      if (companyId !== undefined && companyId !== null) body.company_id = companyId;
      const lot = await parkingLotsApi.create(body);
      setName("");
      setAddress("");
      onCreated(lot);
    } catch (err) {
      setError(err.message || "주차장을 등록하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="pg-card pg-card-pad" onSubmit={handleSubmit}>
      <h3 className="pg-h3">주차장 등록</h3>
      <div className="pg-form-row">
        <label className="pg-field">
          <span>주차장 이름</span>
          <input
            className="pg-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="예: 강남 주차장"
          />
        </label>
        <label className="pg-field">
          <span>주소</span>
          <input
            className="pg-input"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="예: 서울 강남구 ..."
          />
        </label>
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? "등록 중..." : "등록"}
        </button>
      </div>
      {error && (
        <div className="pg-error" role="alert">
          {error}
        </div>
      )}
    </form>
  );
}
