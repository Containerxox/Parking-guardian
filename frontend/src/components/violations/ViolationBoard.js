import React, { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import "./ViolationBoard.css";
import useLoad from "../../hooks/useLoad";
import { companiesApi, parkingLotsApi, violationsApi } from "../../api/client";
import { useAuth } from "../../state/AuthContext";
import { ErrorBox, InfoBox, Loading } from "../ui/States";
import { ViolationStatusBadge } from "../ui/Badge";
import Pagination from "../ui/Pagination";
import ViolationImage from "./ViolationImage";
import ViolationDetailModal from "./ViolationDetailModal";
import NoteModal from "./NoteModal";
import { formatDateTime, vehiclePresenceText } from "../../utils/format";

const PAGE_SIZE = 12;
const TABS = [
  { key: "open", label: "미처리" },
  { key: "resolved", label: "처리 이력" },
];

const noCompanies = () => Promise.resolve(null);

// 회사 관리자의 주차장 필터 기본값: 내 담당 주차장 전체.
const LOT_MINE = "mine";

// 위반 목록 화면의 본문. COMPANY_ADMIN 과 SUPER_ADMIN 이 함께 쓴다.
// scope="super" 이면 회사 필터와 "상태 복구" 버튼이 추가된다.
// scope="company" 이면 주차장 필터에 내 담당 주차장만 나오고, 목록도 담당 주차장의 위반만 보여 준다.
export default function ViolationBoard({ scope = "company" }) {
  const isSuper = scope === "super";
  const location = useLocation();
  const { user } = useAuth();

  const [tab, setTab] = useState("open");
  const [companyId, setCompanyId] = useState("");
  const [lotId, setLotId] = useState(isSuper ? "" : LOT_MINE);
  const [page, setPage] = useState(1);
  const [notice, setNotice] = useState(null); // {type: "error" | "info", text}
  const [busyId, setBusyId] = useState(null);
  const [noteTarget, setNoteTarget] = useState(null); // {mode: "resolve" | "reopen", violation}
  // 알림 화면에서 넘어오면 해당 위반의 상세를 바로 연다.
  const [detailId, setDetailId] = useState(
    location.state && location.state.violationId ? location.state.violationId : null
  );

  const companiesLoader = useCallback(() => companiesApi.list(), []);
  const companies = useLoad(isSuper ? companiesLoader : noCompanies);

  const lotsLoader = useCallback(
    () => parkingLotsApi.list(isSuper && companyId ? { company_id: companyId } : undefined),
    [isSuper, companyId]
  );
  const lots = useLoad(lotsLoader);

  const listLoader = useCallback(
    () =>
      violationsApi.list({
        status: tab,
        parking_lot_id: lotId === LOT_MINE ? undefined : lotId,
        assigned: !isSuper && lotId === LOT_MINE ? true : undefined,
        company_id: isSuper ? companyId : undefined,
        page,
        size: PAGE_SIZE,
      }),
    [tab, lotId, isSuper, companyId, page]
  );
  const list = useLoad(listLoader);
  const reloadList = list.reload;

  // 회사 관리자에게는 자신이 담당으로 배정된 주차장만 선택지로 보여 준다.
  const allLots = (lots.data && lots.data.items) || [];
  const lotOptions = isSuper ? allLots : allLots.filter((lot) => (lot.admin_ids || []).includes(user.id));

  const items = (list.data && list.data.items) || [];
  const total = (list.data && list.data.total) || 0;
  const size = (list.data && list.data.size) || PAGE_SIZE;

  // 처리 후 현재 페이지가 비면 마지막 페이지로 당긴다.
  useEffect(() => {
    if (!list.data || list.loading) return;
    const lastPage = Math.max(1, Math.ceil(total / size));
    if (page > lastPage) setPage(lastPage);
  }, [list.data, list.loading, total, size, page]);

  const changeTab = (key) => {
    setTab(key);
    setPage(1);
    setNotice(null);
  };

  const handleActionError = (err) => {
    setNotice({ type: "error", text: err.message || "요청을 처리하지 못했습니다." });
    // 다른 관리자가 먼저 처리한 경우(409) 등은 목록을 최신 상태로 다시 불러온다.
    if (err.status === 409 || err.status === 404) reloadList();
  };

  const acknowledge = async (violation) => {
    setBusyId(violation.id);
    setNotice(null);
    try {
      await violationsApi.acknowledge(violation.id);
      setNotice({ type: "info", text: "현장 대응을 시작했습니다." });
      reloadList();
    } catch (err) {
      handleActionError(err);
    } finally {
      setBusyId(null);
    }
  };

  const submitNote = async (text) => {
    const { mode, violation } = noteTarget;
    try {
      if (mode === "resolve") {
        await violationsApi.resolve(violation.id, text);
        setNotice({ type: "info", text: "처리 완료로 기록했습니다. 처리 이력 탭에서 확인할 수 있습니다." });
      } else {
        await violationsApi.reopen(violation.id, text);
        setNotice({ type: "info", text: "현장 대응 중 상태로 복구했습니다. 미처리 탭에서 확인할 수 있습니다." });
      }
      setNoteTarget(null);
      reloadList();
    } catch (err) {
      if (err.status === 409 || err.status === 404) {
        setNoteTarget(null);
        handleActionError(err);
        return;
      }
      throw err; // NoteModal 안에 오류 문구를 보여 준다.
    }
  };

  const handleDetailAction = (kind, violation) => {
    setDetailId(null);
    if (kind === "acknowledge") acknowledge(violation);
    else setNoteTarget({ mode: kind, violation });
  };

  const stop = (e) => e.stopPropagation();

  return (
    <>
      <div className="pg-toolbar">
        <div className="pg-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`pg-tab ${tab === t.key ? "active" : ""}`}
              onClick={() => changeTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="pg-spacer" />
        {isSuper && (
          <select
            className="pg-input"
            aria-label="회사 필터"
            value={companyId}
            onChange={(e) => {
              setCompanyId(e.target.value);
              setLotId("");
              setPage(1);
            }}
          >
            <option value="">전체 회사</option>
            {((companies.data && companies.data.items) || []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        )}
        <select
          className="pg-input"
          aria-label="주차장 필터"
          value={lotId}
          onChange={(e) => {
            setLotId(e.target.value);
            setPage(1);
          }}
        >
          {isSuper ? <option value="">전체 주차장</option> : <option value={LOT_MINE}>내 담당 주차장 전체</option>}
          {lotOptions.map((lot) => (
            <option key={lot.id} value={lot.id}>
              {isSuper && !companyId && lot.company_name ? `${lot.company_name} / ` : ""}
              {lot.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn" onClick={reloadList} disabled={list.loading}>
          새로고침
        </button>
      </div>

      {isSuper && companies.error && (
        <ErrorBox message={`회사 목록을 불러오지 못했습니다: ${companies.error}`} onRetry={companies.reload} />
      )}
      {lots.error && (
        <ErrorBox message={`주차장 목록을 불러오지 못했습니다: ${lots.error}`} onRetry={lots.reload} />
      )}
      {notice && notice.type === "error" && <ErrorBox message={notice.text} />}
      {notice && notice.type === "info" && <InfoBox message={notice.text} />}
      <ErrorBox message={list.error} onRetry={reloadList} />

      {list.loading && !list.data && <Loading />}

      {list.data && (
        <>
          <div className="violations-grid">
            {items.length === 0 ? (
              <div className="empty-grid">
                {!isSuper && lots.data && lotOptions.length === 0 && lotId === LOT_MINE
                  ? "담당으로 배정된 주차장이 없습니다. 담당 배정은 서비스 운영자에게 요청해 주세요."
                  : tab === "open"
                  ? "미처리 위반이 없습니다."
                  : "처리 이력이 없습니다."}
              </div>
            ) : (
              items.map((v) => {
                const isOpen = v.status !== "RESOLVED";
                const canAcknowledge = v.status === "DETECTED" || v.status === "NOTIFIED";
                const busy = busyId === v.id;
                return (
                  <div
                    key={v.id}
                    className="violation-card"
                    role="button"
                    tabIndex={0}
                    onClick={() => setDetailId(v.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && e.target === e.currentTarget) setDetailId(v.id);
                    }}
                  >
                    <div className="vc-head">
                      <strong className="vc-lot">{v.parking_lot_name || "-"}</strong>
                      <ViolationStatusBadge status={v.status} />
                    </div>
                    <dl className="pg-dl vc-info">
                      {isSuper && (
                        <>
                          <dt>회사</dt>
                          <dd>{v.company_name || "-"}</dd>
                        </>
                      )}
                      <dt>구역</dt>
                      <dd>{v.zone_name || "-"}</dd>
                      <dt>장비</dt>
                      <dd>{v.machine_name || "-"}</dd>
                      <dt>탐지 시각</dt>
                      <dd>{formatDateTime(v.detected_at)}</dd>
                      <dt>차량</dt>
                      <dd>{vehiclePresenceText(v)}</dd>
                      {!isOpen && (
                        <>
                          <dt>처리 시각</dt>
                          <dd>{formatDateTime(v.resolved_at)}</dd>
                          <dt>처리자</dt>
                          <dd>{v.resolved_by_name || "-"}</dd>
                          <dt>처리 내용</dt>
                          <dd>{v.resolution_note || "-"}</dd>
                        </>
                      )}
                    </dl>

                    <ViolationImage violation={v} />

                    <div className="vc-actions" onClick={stop} onKeyDown={stop} role="presentation">
                      {canAcknowledge && (
                        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => acknowledge(v)}>
                          {busy ? "처리 중..." : "확인 (현장 대응 시작)"}
                        </button>
                      )}
                      {isOpen && (
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          disabled={busy}
                          onClick={() => setNoteTarget({ mode: "resolve", violation: v })}
                        >
                          처리 완료
                        </button>
                      )}
                      {!isOpen && isSuper && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setNoteTarget({ mode: "reopen", violation: v })}
                        >
                          상태 복구
                        </button>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>

          <Pagination page={page} size={size} total={total} onChange={setPage} disabled={list.loading} />
        </>
      )}

      {detailId !== null && (
        <ViolationDetailModal
          violationId={detailId}
          showCompany={isSuper}
          canReopen={isSuper}
          onAction={handleDetailAction}
          onClose={() => setDetailId(null)}
        />
      )}

      {noteTarget && noteTarget.mode === "resolve" && (
        <NoteModal
          title="처리 완료"
          description="현장 조치가 끝났다면 처리 완료로 기록합니다. 기록은 처리 이력에 남습니다."
          label="처리 내용"
          placeholder="예: 차량 이동 조치 완료"
          submitLabel="처리 완료"
          onSubmit={submitNote}
          onClose={() => setNoteTarget(null)}
        />
      )}
      {noteTarget && noteTarget.mode === "reopen" && (
        <NoteModal
          title="상태 복구"
          description="처리 완료된 위반을 현장 대응 중 상태로 되돌립니다."
          label="복구 사유"
          placeholder="예: 처리 완료가 잘못 입력됨"
          required
          submitLabel="상태 복구"
          onSubmit={submitNote}
          onClose={() => setNoteTarget(null)}
        />
      )}
    </>
  );
}
