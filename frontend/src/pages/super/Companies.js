import React, { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import Page from "../../components/ui/Page";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ActiveBadge } from "../../components/ui/Badge";
import useLoad from "../../hooks/useLoad";
import { companiesApi, dashboardApi } from "../../api/client";
import { formatDateTime } from "../../utils/format";

const SUMMARY_FIELDS = [
  { key: "companies", label: "회사" },
  { key: "admins", label: "관리자" },
  { key: "parking_lots", label: "주차장" },
  { key: "machines", label: "장비" },
];

// 운영자가 확인해야 하는 이상 징후. 0보다 크면 강조해서 보여 준다.
const ATTENTION_FIELDS = [
  {
    key: "stale_violations",
    label: "오래 방치된 위반",
    hint: (data) => `탐지 후 ${data.stale_violation_hours ?? 24}시간 넘게 처리되지 않은 건`,
  },
  {
    key: "offline_machines",
    label: "신호가 끊긴 장비",
    hint: () => "사용 중인데 최근 신호가 없는 장비",
  },
  {
    key: "failed_notifications",
    label: "알림 전송 실패",
    hint: () => "알림이 전달되지 않은 미처리 위반 (전송 실패 또는 담당자 없음)",
  },
];

export default function Companies() {
  const summaryLoader = useCallback(() => dashboardApi.summary(), []);
  const summary = useLoad(summaryLoader);
  const companiesLoader = useCallback(() => companiesApi.list(), []);
  const companies = useLoad(companiesLoader);

  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [busyId, setBusyId] = useState(null);

  const items = (companies.data && companies.data.items) || [];

  const reloadAll = () => {
    companies.reload();
    summary.reload();
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!name.trim()) {
      setCreateError("회사 이름을 입력해 주세요.");
      return;
    }
    setCreating(true);
    setCreateError("");
    try {
      await companiesApi.create(name.trim());
      setName("");
      reloadAll();
    } catch (err) {
      setCreateError(err.message || "회사를 등록하지 못했습니다.");
    } finally {
      setCreating(false);
    }
  };

  const toggleStatus = async (company) => {
    const deactivate = company.status === "ACTIVE";
    if (deactivate && !window.confirm(`${company.name} 회사를 비활성화할까요?`)) return;
    setBusyId(company.id);
    setStatusError("");
    try {
      await companiesApi.update(company.id, { status: deactivate ? "INACTIVE" : "ACTIVE" });
      reloadAll();
    } catch (err) {
      setStatusError(`${company.name}: ${err.message || "상태를 변경하지 못했습니다."}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Page title="회사 관리" subtitle="전체 현황과 회사 목록입니다.">
      <ErrorBox message={summary.error && `요약 정보를 불러오지 못했습니다: ${summary.error}`} onRetry={summary.reload} />
      {summary.loading && !summary.data && <Loading text="요약 정보를 불러오는 중..." />}
      {summary.data && (
        <div className="pg-stats">
          {SUMMARY_FIELDS.map((field) => (
            <div key={field.key} className="pg-card pg-stat">
              <div className="pg-stat-label">{field.label}</div>
              <div className="pg-stat-value">{summary.data[field.key] ?? "-"}</div>
            </div>
          ))}
        </div>
      )}

      {summary.data && (
        <section className="pg-section">
          <div className="pg-section-head">
            <h2 className="pg-h2">확인이 필요한 항목</h2>
          </div>
          <div className="pg-stats">
            {ATTENTION_FIELDS.map((field) => {
              const value = summary.data[field.key];
              const needsAttention = typeof value === "number" && value > 0;
              return (
                <div key={field.key} className="pg-card pg-stat">
                  <div className="pg-stat-label">{field.label}</div>
                  <div className="pg-stat-value" style={needsAttention ? { color: "#b91c1c" } : undefined}>
                    {value ?? "-"}
                  </div>
                  <div className="pg-small pg-muted">{field.hint(summary.data)}</div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <form className="pg-card pg-card-pad" onSubmit={handleCreate}>
        <h3 className="pg-h3">회사 등록</h3>
        <div className="pg-form-row">
          <label className="pg-field">
            <span>회사 이름</span>
            <input
              className="pg-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="예: A회사"
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={creating}>
            {creating ? "등록 중..." : "등록"}
          </button>
        </div>
        {createError && (
          <div className="pg-error" role="alert">
            {createError}
          </div>
        )}
      </form>

      <section className="pg-section">
        <div className="pg-section-head">
          <h2 className="pg-h2">회사 목록</h2>
        </div>
        <ErrorBox message={statusError} />
        <ErrorBox message={companies.error} onRetry={companies.reload} />
        {companies.loading && !companies.data && <Loading />}
        {companies.data && (
          <div className="pg-card">
            <div className="table-wrap">
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>회사</th>
                    <th>상태</th>
                    <th>관리자 수</th>
                    <th>주차장 수</th>
                    <th>미처리 위반</th>
                    <th>등록일</th>
                    <th>관리</th>
                  </tr>
                </thead>
                <tbody>
                  {items.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="pg-empty">
                        등록된 회사가 없습니다.
                      </td>
                    </tr>
                  ) : (
                    items.map((company) => {
                      const active = company.status === "ACTIVE";
                      return (
                        <tr key={company.id}>
                          <td className="left">
                            <Link className="pg-link" to={`/super/companies/${company.id}`}>
                              {company.name}
                            </Link>
                          </td>
                          <td>
                            <ActiveBadge status={company.status} />
                          </td>
                          <td>{company.admin_count ?? 0}</td>
                          <td>{company.parking_lot_count ?? 0}</td>
                          <td>{company.open_violation_count ?? 0}</td>
                          <td>{formatDateTime(company.created_at)}</td>
                          <td>
                            <button
                              type="button"
                              className={`btn btn-sm ${active ? "btn-danger" : ""}`}
                              disabled={busyId === company.id}
                              onClick={() => toggleStatus(company)}
                            >
                              {active ? "비활성화" : "활성화"}
                            </button>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </Page>
  );
}
