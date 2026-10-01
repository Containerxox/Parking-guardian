import React, { useCallback } from "react";
import Modal from "../ui/Modal";
import { ErrorBox, Loading } from "../ui/States";
import { ViolationStatusBadge } from "../ui/Badge";
import ViolationImage from "./ViolationImage";
import useLoad from "../../hooks/useLoad";
import { violationsApi } from "../../api/client";
import { HISTORY_ACTION_LABEL, formatDateTime, vehiclePresenceText } from "../../utils/format";

// 위반 한 건의 큰 이미지와 처리 이력(GET /violations/{id})을 보여 준다.
// onAction(kind, violation): kind 는 "acknowledge" | "resolve" | "reopen"
export default function ViolationDetailModal({ violationId, showCompany, canReopen, onAction, onClose }) {
  const loader = useCallback(() => violationsApi.get(violationId), [violationId]);
  const { data: violation, loading, error, reload } = useLoad(loader);

  let footer = null;
  if (violation) {
    const status = violation.status;
    const canAcknowledge = status === "DETECTED" || status === "NOTIFIED";
    const isOpen = status !== "RESOLVED";
    footer = (
      <>
        {canAcknowledge && (
          <button type="button" className="btn" onClick={() => onAction("acknowledge", violation)}>
            확인 (현장 대응 시작)
          </button>
        )}
        {isOpen && (
          <button type="button" className="btn btn-primary" onClick={() => onAction("resolve", violation)}>
            처리 완료
          </button>
        )}
        {!isOpen && canReopen && (
          <button type="button" className="btn" onClick={() => onAction("reopen", violation)}>
            상태 복구
          </button>
        )}
      </>
    );
  }

  return (
    <Modal title="위반 상세" onClose={onClose} wide footer={footer}>
      {loading && !violation && <Loading />}
      <ErrorBox message={error} onRetry={reload} />
      {violation && (
        <div className="vio-detail">
          <ViolationImage violation={violation} large />

          <div className="vio-detail-side">
            <dl className="pg-dl">
              <dt>상태</dt>
              <dd>
                <ViolationStatusBadge status={violation.status} />
              </dd>
              {showCompany && (
                <>
                  <dt>회사</dt>
                  <dd>{violation.company_name || "-"}</dd>
                </>
              )}
              <dt>주차장</dt>
              <dd>{violation.parking_lot_name || "-"}</dd>
              <dt>구역</dt>
              <dd>{violation.zone_name || "-"}</dd>
              <dt>장비</dt>
              <dd>{violation.machine_name || "-"}</dd>
              <dt>탐지 시각</dt>
              <dd>{formatDateTime(violation.detected_at)}</dd>
              <dt>차량</dt>
              <dd>{vehiclePresenceText(violation)}</dd>
              <dt>탐지 횟수</dt>
              <dd>{violation.detection_count ?? 1}회 (장비가 1분마다 확인)</dd>
              {violation.status === "RESOLVED" && (
                <>
                  <dt>처리 시각</dt>
                  <dd>{formatDateTime(violation.resolved_at)}</dd>
                  <dt>처리자</dt>
                  <dd>{violation.resolved_by_name || "-"}</dd>
                  <dt>처리 내용</dt>
                  <dd>{violation.resolution_note || "-"}</dd>
                </>
              )}
            </dl>

            <h3 className="pg-h3" style={{ marginTop: 18 }}>
              처리 이력
            </h3>
            {(violation.history || []).length === 0 ? (
              <div className="pg-empty">이력이 없습니다.</div>
            ) : (
              <ol className="vio-timeline">
                {violation.history.map((h) => (
                  <li key={h.id}>
                    <div className="vio-timeline-time">{formatDateTime(h.created_at)}</div>
                    <div className="vio-timeline-action">
                      {HISTORY_ACTION_LABEL[h.action] || h.action}
                      <span className="vio-timeline-who"> · {h.admin_name || "시스템"}</span>
                    </div>
                    {h.message && <div className="vio-timeline-msg">{h.message}</div>}
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
