import React, { useCallback, useState } from "react";
import Page from "../../components/ui/Page";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ActiveBadge, OnlineBadge } from "../../components/ui/Badge";
import useLoad from "../../hooks/useLoad";
import { companiesApi, machinesApi } from "../../api/client";
import { formatDateTime } from "../../utils/format";

export default function SuperMachines() {
  const [companyId, setCompanyId] = useState("");

  const companiesLoader = useCallback(() => companiesApi.list(), []);
  const companies = useLoad(companiesLoader);
  const machinesLoader = useCallback(
    () => machinesApi.list(companyId ? { company_id: companyId } : undefined),
    [companyId]
  );
  const machines = useLoad(machinesLoader);

  const companyItems = (companies.data && companies.data.items) || [];
  const companyNameById = {};
  companyItems.forEach((c) => {
    companyNameById[c.id] = c.name;
  });
  const items = (machines.data && machines.data.items) || [];
  const onlineCount = items.filter((m) => m.online).length;

  return (
    <Page
      title="장비"
      subtitle={machines.data ? `전체 ${items.length}대 중 ${onlineCount}대 온라인` : "전체 장비의 연결 상태입니다."}
    >
      <div className="pg-toolbar">
        <select
          className="pg-input"
          aria-label="회사 필터"
          value={companyId}
          onChange={(e) => setCompanyId(e.target.value)}
        >
          <option value="">전체 회사</option>
          {companyItems.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <div className="pg-spacer" />
        <button type="button" className="btn" onClick={machines.reload} disabled={machines.loading}>
          새로고침
        </button>
      </div>

      {companies.error && (
        <ErrorBox message={`회사 목록을 불러오지 못했습니다: ${companies.error}`} onRetry={companies.reload} />
      )}
      <ErrorBox message={machines.error} onRetry={machines.reload} />
      {machines.loading && !machines.data && <Loading />}

      {machines.data && (
        <div className="pg-card">
          <div className="table-wrap">
            <table className="pg-table">
              <thead>
                <tr>
                  <th>장비 이름</th>
                  <th>시리얼 번호</th>
                  <th>회사</th>
                  <th>주차장</th>
                  <th>상태</th>
                  <th>연결</th>
                  <th>마지막 신호</th>
                  <th>설치일</th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="pg-empty">
                      등록된 장비가 없습니다.
                    </td>
                  </tr>
                ) : (
                  items.map((machine) => (
                    <tr key={machine.id}>
                      <td>{machine.name}</td>
                      <td translate="no">{machine.serial_number}</td>
                      <td>{companyNameById[machine.company_id] || `회사 #${machine.company_id}`}</td>
                      <td>{machine.parking_lot_name || "-"}</td>
                      <td>
                        <ActiveBadge status={machine.status} />
                      </td>
                      <td>
                        <OnlineBadge online={machine.online} />
                      </td>
                      <td>
                        {machine.last_heartbeat_at ? formatDateTime(machine.last_heartbeat_at) : "수신 기록 없음"}
                      </td>
                      <td>{formatDateTime(machine.installed_at)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Page>
  );
}
