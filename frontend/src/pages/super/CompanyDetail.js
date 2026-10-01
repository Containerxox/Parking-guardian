import React, { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Page from "../../components/ui/Page";
import Modal from "../../components/ui/Modal";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ActiveBadge } from "../../components/ui/Badge";
import ParkingLotCreateForm from "../../components/parking/ParkingLotCreateForm";
import useLoad from "../../hooks/useLoad";
import { adminsApi, companiesApi, parkingLotsApi } from "../../api/client";
import { formatDateTime } from "../../utils/format";

// 관리자 한 명의 담당 주차장을 체크박스로 고르는 Modal
function AssignLotsModal({ admin, lots, onSaved, onClose }) {
  const [selected, setSelected] = useState(() => new Set(admin.parking_lot_ids || []));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const toggle = (lotId, checked) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(lotId);
      else next.delete(lotId);
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await adminsApi.setParkingLots(admin.id, Array.from(selected));
      onSaved(updated);
    } catch (err) {
      setError(err.message || "담당 주차장을 저장하지 못했습니다.");
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`${admin.name} 담당 주차장`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            취소
          </button>
          <button type="button" className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "저장 중..." : "저장"}
          </button>
        </>
      }
    >
      {lots.length === 0 ? (
        <div className="pg-empty">이 회사에 등록된 주차장이 없습니다.</div>
      ) : (
        lots.map((lot) => (
          <label key={lot.id} className="pg-check">
            <input
              type="checkbox"
              checked={selected.has(lot.id)}
              onChange={(e) => toggle(lot.id, e.target.checked)}
            />
            {lot.name}
            <span className="pg-muted pg-small">{lot.address}</span>
          </label>
        ))
      )}
      {error && (
        <div className="pg-error" role="alert">
          {error}
        </div>
      )}
    </Modal>
  );
}

function AdminCreateForm({ companyId, onCreated }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!email.trim() || !name.trim() || !password) {
      setError("이메일, 이름, 비밀번호를 모두 입력해 주세요.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await adminsApi.create({
        email: email.trim(),
        password,
        name: name.trim(),
        company_id: companyId,
      });
      setEmail("");
      setName("");
      setPassword("");
      onCreated();
    } catch (err) {
      setError(err.message || "관리자를 등록하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="pg-card pg-card-pad" onSubmit={handleSubmit} noValidate>
      <h3 className="pg-h3">관리자 등록</h3>
      <div className="pg-form-row">
        <label className="pg-field">
          <span>이메일</span>
          <input
            className="pg-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="예: kim@a.com"
            autoComplete="off"
          />
        </label>
        <label className="pg-field">
          <span>이름</span>
          <input
            className="pg-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="예: 김관리자"
            autoComplete="off"
          />
        </label>
        <label className="pg-field">
          <span>초기 비밀번호</span>
          <input
            className="pg-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="비밀번호"
            autoComplete="new-password"
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

export default function CompanyDetail() {
  const { id } = useParams();
  const companyId = Number(id);

  const companyLoader = useCallback(() => companiesApi.get(companyId), [companyId]);
  const company = useLoad(companyLoader);
  const adminsLoader = useCallback(() => adminsApi.list({ company_id: companyId }), [companyId]);
  const admins = useLoad(adminsLoader);
  const lotsLoader = useCallback(() => parkingLotsApi.list({ company_id: companyId }), [companyId]);
  const lots = useLoad(lotsLoader);

  const [assignTarget, setAssignTarget] = useState(null);
  const [adminError, setAdminError] = useState("");
  const [busyAdminId, setBusyAdminId] = useState(null);

  const adminItems = (admins.data && admins.data.items) || [];
  const lotItems = (lots.data && lots.data.items) || [];
  const lotNameById = {};
  lotItems.forEach((lot) => {
    lotNameById[lot.id] = lot.name;
  });

  const replaceAdmin = (updated) => {
    admins.setData((data) =>
      data ? { ...data, items: data.items.map((a) => (a.id === updated.id ? updated : a)) } : data
    );
  };

  const toggleAdminStatus = async (admin) => {
    const deactivate = admin.status === "ACTIVE";
    if (deactivate && !window.confirm(`${admin.name} 관리자를 비활성화할까요? 비활성화하면 로그인할 수 없습니다.`)) {
      return;
    }
    setBusyAdminId(admin.id);
    setAdminError("");
    try {
      const updated = await adminsApi.update(admin.id, { status: deactivate ? "INACTIVE" : "ACTIVE" });
      replaceAdmin(updated);
    } catch (err) {
      setAdminError(`${admin.name}: ${err.message || "상태를 변경하지 못했습니다."}`);
    } finally {
      setBusyAdminId(null);
    }
  };

  return (
    <Page
      title={company.data ? company.data.name : "회사 상세"}
      actions={
        <Link className="btn" to="/super/companies">
          회사 목록으로 돌아가기
        </Link>
      }
    >
      {/* 회사 정보 */}
      <ErrorBox message={company.error} onRetry={company.reload} />
      {company.loading && !company.data && <Loading />}
      {company.data && (
        <div className="pg-card pg-card-pad">
          <dl className="pg-dl">
            <dt>회사 이름</dt>
            <dd>{company.data.name}</dd>
            <dt>상태</dt>
            <dd>
              <ActiveBadge status={company.data.status} />
            </dd>
            <dt>등록일</dt>
            <dd>{formatDateTime(company.data.created_at)}</dd>
            <dt>관리자 수</dt>
            <dd>{company.data.admin_count ?? 0}명</dd>
            <dt>주차장 수</dt>
            <dd>{company.data.parking_lot_count ?? 0}곳</dd>
            <dt>미처리 위반</dt>
            <dd>{company.data.open_violation_count ?? 0}건</dd>
          </dl>
        </div>
      )}

      {/* 관리자 */}
      <section className="pg-section">
        <div className="pg-section-head">
          <h2 className="pg-h2">관리자 ({adminItems.length})</h2>
        </div>
        <ErrorBox message={adminError} />
        <ErrorBox message={admins.error} onRetry={admins.reload} />
        {admins.loading && !admins.data && <Loading />}
        {admins.data && (
          <div className="pg-card">
            <div className="table-wrap">
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>이름</th>
                    <th>이메일</th>
                    <th>상태</th>
                    <th>담당 주차장</th>
                    <th>관리</th>
                  </tr>
                </thead>
                <tbody>
                  {adminItems.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="pg-empty">
                        등록된 관리자가 없습니다.
                      </td>
                    </tr>
                  ) : (
                    adminItems.map((admin) => {
                      const active = admin.status === "ACTIVE";
                      const lotIds = admin.parking_lot_ids || [];
                      return (
                        <tr key={admin.id}>
                          <td>{admin.name}</td>
                          <td translate="no">{admin.email}</td>
                          <td>
                            <ActiveBadge status={admin.status} />
                          </td>
                          <td className="left">
                            {lotIds.length === 0
                              ? "없음"
                              : lotIds.map((lotId) => lotNameById[lotId] || `주차장 #${lotId}`).join(", ")}
                          </td>
                          <td>
                            <div className="pg-cell-actions">
                              <button type="button" className="btn btn-sm" onClick={() => setAssignTarget(admin)}>
                                담당 주차장 편집
                              </button>
                              <button
                                type="button"
                                className={`btn btn-sm ${active ? "btn-danger" : ""}`}
                                disabled={busyAdminId === admin.id}
                                onClick={() => toggleAdminStatus(admin)}
                              >
                                {active ? "비활성화" : "활성화"}
                              </button>
                            </div>
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
        <AdminCreateForm
          companyId={companyId}
          onCreated={() => {
            admins.reload();
            company.reload();
          }}
        />
      </section>

      {/* 주차장 */}
      <section className="pg-section">
        <div className="pg-section-head">
          <h2 className="pg-h2">주차장 ({lotItems.length})</h2>
        </div>
        <ErrorBox message={lots.error} onRetry={lots.reload} />
        {lots.loading && !lots.data && <Loading />}
        {lots.data && (
          <div className="pg-card">
            <div className="table-wrap">
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>주차장</th>
                    <th>주소</th>
                    <th>상태</th>
                    <th>구역 수</th>
                    <th>장비 수</th>
                    <th>미처리 위반</th>
                  </tr>
                </thead>
                <tbody>
                  {lotItems.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="pg-empty">
                        등록된 주차장이 없습니다.
                      </td>
                    </tr>
                  ) : (
                    lotItems.map((lot) => (
                      <tr key={lot.id}>
                        <td className="left">
                          <Link className="pg-link" to={`/parking-lots/${lot.id}`}>
                            {lot.name}
                          </Link>
                        </td>
                        <td className="left">{lot.address || "-"}</td>
                        <td>
                          <ActiveBadge status={lot.status} />
                        </td>
                        <td>{lot.zone_count ?? 0}</td>
                        <td>{lot.machine_count ?? 0}</td>
                        <td>{lot.open_violation_count ?? 0}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
        <ParkingLotCreateForm
          companyId={companyId}
          onCreated={() => {
            lots.reload();
            company.reload();
          }}
        />
      </section>

      {assignTarget && (
        <AssignLotsModal
          admin={assignTarget}
          lots={lotItems}
          onSaved={(updated) => {
            replaceAdmin(updated);
            setAssignTarget(null);
          }}
          onClose={() => setAssignTarget(null)}
        />
      )}
    </Page>
  );
}
