import React, { useCallback, useState } from "react";
import Page from "../../components/ui/Page";
import { ErrorBox, Loading } from "../../components/ui/States";
import Pagination from "../../components/ui/Pagination";
import useLoad from "../../hooks/useLoad";
import { auditLogsApi, companiesApi } from "../../api/client";
import { formatDateTime } from "../../utils/format";

const PAGE_SIZE = 50;

// before_value / after_value 는 객체일 수도, 문자열일 수도 있다.
function ValueCell({ value }) {
  if (value === null || value === undefined || value === "") return "-";
  let text;
  if (typeof value === "string") {
    try {
      text = JSON.stringify(JSON.parse(value), null, 2);
    } catch (e) {
      text = value;
    }
  } else {
    text = JSON.stringify(value, null, 2);
  }
  return (
    <details>
      <summary>보기</summary>
      <pre translate="no">{text}</pre>
    </details>
  );
}

export default function AuditLogs() {
  const [companyId, setCompanyId] = useState("");
  const [page, setPage] = useState(1);

  const companiesLoader = useCallback(() => companiesApi.list(), []);
  const companies = useLoad(companiesLoader);
  const logsLoader = useCallback(
    () => auditLogsApi.list({ company_id: companyId, page, size: PAGE_SIZE }),
    [companyId, page]
  );
  const logs = useLoad(logsLoader);

  const companyItems = (companies.data && companies.data.items) || [];
  const companyNameById = {};
  companyItems.forEach((c) => {
    companyNameById[c.id] = c.name;
  });
  const items = (logs.data && logs.data.items) || [];

  const companyLabel = (id) => {
    if (id === null || id === undefined) return "-";
    return companyNameById[id] || `회사 #${id}`;
  };

  return (
    <Page title="감사 로그" subtitle="누가 언제 무엇을 바꿨는지 기록한 내역입니다.">
      <div className="pg-toolbar">
        <select
          className="pg-input"
          aria-label="회사 필터"
          value={companyId}
          onChange={(e) => {
            setCompanyId(e.target.value);
            setPage(1);
          }}
        >
          <option value="">전체 회사</option>
          {companyItems.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <div className="pg-spacer" />
        <button type="button" className="btn" onClick={logs.reload} disabled={logs.loading}>
          새로고침
        </button>
      </div>

      {companies.error && (
        <ErrorBox message={`회사 목록을 불러오지 못했습니다: ${companies.error}`} onRetry={companies.reload} />
      )}
      <ErrorBox message={logs.error} onRetry={logs.reload} />
      {logs.loading && !logs.data && <Loading />}

      {logs.data && (
        <>
          <div className="pg-card">
            <div className="table-wrap">
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>시각</th>
                    <th>수행자</th>
                    <th>회사</th>
                    <th>동작</th>
                    <th>대상</th>
                    <th>변경 전</th>
                    <th>변경 후</th>
                  </tr>
                </thead>
                <tbody>
                  {items.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="pg-empty">
                        감사 로그가 없습니다.
                      </td>
                    </tr>
                  ) : (
                    items.map((log) => (
                      <tr key={log.id}>
                        <td>{formatDateTime(log.created_at)}</td>
                        <td translate="no">{log.actor_email || (log.actor_id ? `#${log.actor_id}` : "시스템")}</td>
                        <td>{companyLabel(log.company_id)}</td>
                        <td translate="no">{log.action}</td>
                        <td translate="no">
                          {log.resource_type}
                          {log.resource_id !== null && log.resource_id !== undefined ? ` #${log.resource_id}` : ""}
                        </td>
                        <td>
                          <ValueCell value={log.before_value} />
                        </td>
                        <td>
                          <ValueCell value={log.after_value} />
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
          <Pagination
            page={page}
            size={logs.data.size || PAGE_SIZE}
            total={logs.data.total || 0}
            onChange={setPage}
            disabled={logs.loading}
          />
        </>
      )}
    </Page>
  );
}
