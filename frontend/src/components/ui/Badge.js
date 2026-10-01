import React from "react";
import {
  ACTIVE_STATUS_LABEL,
  VIOLATION_STATUS_LABEL,
  VIOLATION_STATUS_TONE,
} from "../../utils/format";

export function Badge({ tone = "gray", children }) {
  return <span className={`pg-badge is-${tone}`}>{children}</span>;
}

export function ViolationStatusBadge({ status }) {
  return (
    <Badge tone={VIOLATION_STATUS_TONE[status] || "gray"}>
      {VIOLATION_STATUS_LABEL[status] || status || "-"}
    </Badge>
  );
}

export function ActiveBadge({ status }) {
  return (
    <Badge tone={status === "ACTIVE" ? "green" : "gray"}>
      {ACTIVE_STATUS_LABEL[status] || status || "-"}
    </Badge>
  );
}

export function OnlineBadge({ online }) {
  return <Badge tone={online ? "green" : "gray"}>{online ? "온라인" : "오프라인"}</Badge>;
}
