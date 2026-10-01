import React, { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Page from "../../components/ui/Page";
import Modal from "../../components/ui/Modal";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ActiveBadge, OnlineBadge } from "../../components/ui/Badge";
import useLoad from "../../hooks/useLoad";
import { useAuth } from "../../state/AuthContext";
import { adminsApi, machinesApi, parkingLotsApi, zonesApi } from "../../api/client";
import { formatDateTime } from "../../utils/format";

const noAdmins = () => Promise.resolve(null);

// 발급된 장비 API Key 를 한 번만 보여 주는 창.
// 실수로 닫히지 않도록 "키를 보관했습니다" 버튼으로만 닫는다.
function IssuedKeyModal({ issued, onClose }) {
  const [copyState, setCopyState] = useState("");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(issued.api_key);
      setCopyState("복사했습니다.");
    } catch (e) {
      setCopyState("자동 복사에 실패했습니다. 키를 직접 선택해 복사해 주세요.");
    }
  };

  return (
    <Modal title={issued.rotated ? "장비 API Key 재발급" : "장비 API Key 발급"} onClose={onClose} dismissible={false}>
      <div className="pg-keybox" role="alert">
        <p className="pg-keybox-title">
          {issued.machine ? `${issued.machine.name} (${issued.machine.serial_number})` : "장비"} API Key
        </p>
        <p className="pg-keybox-warn">
          이 키는 지금 한 번만 표시됩니다. 이 창을 닫으면 다시 볼 수 없으니 반드시 지금 복사해 장비에 설정하세요.
          {issued.rotated ? " 이전 키는 더 이상 사용할 수 없습니다." : ""}
        </p>
        <code className="pg-keybox-key" translate="no">
          {issued.api_key}
        </code>
        <div className="pg-keybox-actions">
          <button type="button" className="btn btn-sm" onClick={copy}>
            복사
          </button>
          <button type="button" className="btn btn-sm btn-primary" onClick={onClose}>
            키를 보관했습니다 (닫기)
          </button>
          {copyState && <span className="pg-small">{copyState}</span>}
        </div>
      </div>
    </Modal>
  );
}

// canEdit: 서비스 운영자(SUPER_ADMIN)만 true. 회사 관리자에게는 목록만 보여 준다.
function ZoneSection({ lot, onChanged, canEdit }) {
  const [zoneName, setZoneName] = useState("");
  const [description, setDescription] = useState("");
  const [machineId, setMachineId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const zones = lot.zones || [];
  const machines = lot.machines || [];
  const machineName = (id) => {
    if (id === null || id === undefined) return "-";
    const machine = machines.find((m) => m.id === id);
    return machine ? machine.name : `장비 #${id}`;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!zoneName.trim()) {
      setError("구역 이름을 입력해 주세요.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const body = { zone_name: zoneName.trim() };
      if (description.trim()) body.location_description = description.trim();
      if (machineId) body.machine_id = Number(machineId);
      await zonesApi.create(lot.id, body);
      setZoneName("");
      setDescription("");
      setMachineId("");
      onChanged();
    } catch (err) {
      setError(err.message || "구역을 추가하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="pg-section">
      <div className="pg-section-head">
        <h2 className="pg-h2">구역 ({zones.length})</h2>
      </div>
      <div className="pg-card">
        <div className="table-wrap">
          <table className="pg-table">
            <thead>
              <tr>
                <th>구역 이름</th>
                <th>위치 설명</th>
                <th>연결된 장비</th>
              </tr>
            </thead>
            <tbody>
              {zones.length === 0 ? (
                <tr>
                  <td colSpan={3} className="pg-empty">
                    등록된 구역이 없습니다.
                  </td>
                </tr>
              ) : (
                zones.map((zone) => (
                  <tr key={zone.id}>
                    <td>{zone.zone_name}</td>
                    <td className="left">{zone.location_description || "-"}</td>
                    <td>{machineName(zone.machine_id)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {canEdit && (
      <form className="pg-card pg-card-pad" onSubmit={handleSubmit}>
        <h3 className="pg-h3">구역 추가</h3>
        <div className="pg-form-row">
          <label className="pg-field">
            <span>구역 이름</span>
            <input
              className="pg-input"
              value={zoneName}
              onChange={(e) => setZoneName(e.target.value)}
              placeholder="예: A-01"
            />
          </label>
          <label className="pg-field">
            <span>위치 설명 (선택)</span>
            <input
              className="pg-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="예: 지하 1층 입구 옆"
            />
          </label>
          <label className="pg-field is-narrow">
            <span>장비 (선택)</span>
            <select className="pg-input" value={machineId} onChange={(e) => setMachineId(e.target.value)}>
              <option value="">선택 안 함</option>
              {machines.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.serial_number})
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "추가 중..." : "구역 추가"}
          </button>
        </div>
        {error && (
          <div className="pg-error" role="alert">
            {error}
          </div>
        )}
      </form>
      )}
    </section>
  );
}

// onIssued({machine, api_key, rotated}): 발급된 키는 화면 전체에서 한 번만 보여 주므로 부모가 보관한다.
function MachineSection({ lot, onChanged, onIssued, canEdit }) {
  const [serialNumber, setSerialNumber] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [rotateError, setRotateError] = useState("");
  const [rotatingId, setRotatingId] = useState(null);

  const machines = lot.machines || [];

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!serialNumber.trim() || !name.trim()) {
      setFormError("시리얼 번호와 장비 이름을 입력해 주세요.");
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      const result = await machinesApi.create({
        parking_lot_id: lot.id,
        serial_number: serialNumber.trim(),
        name: name.trim(),
      });
      onIssued({ machine: result.machine, api_key: result.api_key, rotated: false });
      setSerialNumber("");
      setName("");
      onChanged();
    } catch (err) {
      setFormError(err.message || "장비를 등록하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  const rotateKey = async (machine) => {
    const ok = window.confirm(
      `${machine.name} 장비의 키를 재발급할까요?\n기존 키는 즉시 사용할 수 없게 되며, 장비에 새 키를 설정해야 합니다.`
    );
    if (!ok) return;
    setRotatingId(machine.id);
    setRotateError("");
    try {
      const result = await machinesApi.rotateKey(machine.id);
      onIssued({ machine: result.machine || machine, api_key: result.api_key, rotated: true });
    } catch (err) {
      setRotateError(err.message || "키를 재발급하지 못했습니다.");
    } finally {
      setRotatingId(null);
    }
  };

  return (
    <section className="pg-section">
      <div className="pg-section-head">
        <h2 className="pg-h2">장비 ({machines.length})</h2>
      </div>

      <ErrorBox message={rotateError} />

      <div className="pg-card">
        <div className="table-wrap">
          <table className="pg-table">
            <thead>
              <tr>
                <th>장비 이름</th>
                <th>시리얼 번호</th>
                <th>상태</th>
                <th>연결</th>
                <th>마지막 신호</th>
                {canEdit && <th>관리</th>}
              </tr>
            </thead>
            <tbody>
              {machines.length === 0 ? (
                <tr>
                  <td colSpan={canEdit ? 6 : 5} className="pg-empty">
                    등록된 장비가 없습니다.
                  </td>
                </tr>
              ) : (
                machines.map((machine) => (
                  <tr key={machine.id}>
                    <td>{machine.name}</td>
                    <td translate="no">{machine.serial_number}</td>
                    <td>
                      <ActiveBadge status={machine.status} />
                    </td>
                    <td>
                      <OnlineBadge online={machine.online} />
                    </td>
                    <td>{machine.last_heartbeat_at ? formatDateTime(machine.last_heartbeat_at) : "수신 기록 없음"}</td>
                    {canEdit && (
                      <td>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={rotatingId === machine.id}
                          onClick={() => rotateKey(machine)}
                        >
                          {rotatingId === machine.id ? "재발급 중..." : "키 재발급"}
                        </button>
                      </td>
                    )}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {canEdit && (
      <form className="pg-card pg-card-pad" onSubmit={handleSubmit}>
        <h3 className="pg-h3">장비 등록</h3>
        <div className="pg-form-row">
          <label className="pg-field">
            <span>시리얼 번호</span>
            <input
              className="pg-input"
              value={serialNumber}
              onChange={(e) => setSerialNumber(e.target.value)}
              placeholder="예: RPI-0001"
            />
          </label>
          <label className="pg-field">
            <span>장비 이름</span>
            <input
              className="pg-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="예: Camera-01"
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "등록 중..." : "장비 등록"}
          </button>
        </div>
        <p className="pg-sub pg-small">등록하면 장비용 API Key 가 한 번만 표시됩니다.</p>
        {formError && (
          <div className="pg-error" role="alert">
            {formError}
          </div>
        )}
      </form>
      )}
    </section>
  );
}

function AdminSection({ lot, isSuper }) {
  const companyId = lot.company_id;
  const lotId = lot.id;
  const [savingId, setSavingId] = useState(null);
  const [saveError, setSaveError] = useState("");

  // COMPANY_ADMIN 은 Token 의 회사로 정해지므로 company_id 를 보내지 않는다.
  const loader = useCallback(
    () => adminsApi.list(isSuper ? { company_id: companyId } : undefined),
    [isSuper, companyId]
  );
  const admins = useLoad(companyId === null || companyId === undefined ? noAdmins : loader);
  const setAdminsData = admins.setData;

  const items = ((admins.data && admins.data.items) || []).filter((a) => a.role === "COMPANY_ADMIN");

  // 체크박스를 바꾸면 그 관리자의 담당 주차장 전체 목록을 새로 보낸다.
  const toggle = async (admin, checked) => {
    const current = admin.parking_lot_ids || [];
    const next = checked
      ? Array.from(new Set([...current, lotId]))
      : current.filter((id) => id !== lotId);
    setSavingId(admin.id);
    setSaveError("");
    try {
      const updated = await adminsApi.setParkingLots(admin.id, next);
      setAdminsData((data) =>
        data ? { ...data, items: data.items.map((a) => (a.id === updated.id ? updated : a)) } : data
      );
    } catch (err) {
      setSaveError(`${admin.name}: ${err.message || "담당 주차장을 변경하지 못했습니다."}`);
    } finally {
      setSavingId(null);
    }
  };

  return (
    <section className="pg-section">
      <div className="pg-section-head">
        <h2 className="pg-h2">담당 관리자</h2>
      </div>
      <p className="pg-sub pg-small" style={{ marginBottom: 10 }}>
        {isSuper
          ? "체크한 관리자가 이 주차장을 담당하고 위반 알림을 받습니다. 변경은 바로 저장됩니다."
          : "담당으로 표시된 관리자가 이 주차장의 위반 알림을 받습니다."}
      </p>
      <ErrorBox message={saveError} />
      <ErrorBox message={admins.error} onRetry={admins.reload} />
      {admins.loading && !admins.data && <Loading />}
      {admins.data && (
        <div className="pg-card">
          <div className="table-wrap">
            <table className="pg-table">
              <thead>
                <tr>
                  <th>담당</th>
                  <th>이름</th>
                  <th>이메일</th>
                  <th>상태</th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="pg-empty">
                      이 회사에 등록된 관리자가 없습니다.
                    </td>
                  </tr>
                ) : (
                  items.map((admin) => {
                    const assigned = (admin.parking_lot_ids || []).includes(lotId);
                    return (
                      <tr key={admin.id}>
                        <td>
                          {isSuper ? (
                            <>
                              <input
                                type="checkbox"
                                aria-label={`${admin.name} 담당 여부`}
                                checked={assigned}
                                disabled={savingId !== null}
                                onChange={(e) => toggle(admin, e.target.checked)}
                              />
                              {savingId === admin.id && <span className="pg-small pg-muted"> 저장 중...</span>}
                            </>
                          ) : assigned ? (
                            "담당"
                          ) : (
                            <span className="pg-muted">-</span>
                          )}
                        </td>
                        <td>{admin.name}</td>
                        <td translate="no">{admin.email}</td>
                        <td>
                          <ActiveBadge status={admin.status} />
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
  );
}

// 주차장 상세. SUPER_ADMIN 과 COMPANY_ADMIN 이 함께 쓴다.
export default function ParkingLotDetail() {
  const { id } = useParams();
  const { user } = useAuth();
  const isSuper = user.role === "SUPER_ADMIN";

  const loader = useCallback(() => parkingLotsApi.get(id), [id]);
  const { data: lot, loading, error, reload } = useLoad(loader);
  // 목록을 다시 불러오다 실패해도 발급된 키가 사라지지 않도록 화면 최상위에서 보관한다.
  const [issuedKey, setIssuedKey] = useState(null);

  const backTo = isSuper ? (lot ? `/super/companies/${lot.company_id}` : "/super/companies") : "/parking-lots";
  const backLabel = isSuper ? "회사 상세로 돌아가기" : "주차장 목록으로 돌아가기";

  return (
    <Page
      title={lot ? lot.name : "주차장 상세"}
      subtitle={lot ? lot.address : ""}
      actions={
        <Link className="btn" to={backTo}>
          {backLabel}
        </Link>
      }
    >
      <ErrorBox message={error} onRetry={reload} />
      {loading && !lot && <Loading />}

      {lot && (
        <>
          <div className="pg-card pg-card-pad">
            <dl className="pg-dl">
              <dt>회사</dt>
              <dd>{lot.company_name || "-"}</dd>
              <dt>주소</dt>
              <dd>{lot.address || "-"}</dd>
              <dt>상태</dt>
              <dd>
                <ActiveBadge status={lot.status} />
              </dd>
              <dt>미처리 위반</dt>
              <dd>{lot.open_violation_count ?? 0}건</dd>
            </dl>
          </div>

          {!isSuper && (
            <p className="pg-sub pg-small" style={{ margin: "12px 0" }}>
              이 화면은 조회 전용입니다. 구역, 장비, 담당 관리자 변경은 서비스 운영자에게 요청해 주세요.
            </p>
          )}

          <ZoneSection lot={lot} onChanged={reload} canEdit={isSuper} />
          <MachineSection lot={lot} onChanged={reload} onIssued={setIssuedKey} canEdit={isSuper} />
          <AdminSection lot={lot} isSuper={isSuper} />
        </>
      )}

      {issuedKey && <IssuedKeyModal issued={issuedKey} onClose={() => setIssuedKey(null)} />}
    </Page>
  );
}
