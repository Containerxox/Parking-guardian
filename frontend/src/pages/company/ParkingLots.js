import React, { useCallback } from "react";
import { Link } from "react-router-dom";
import Page from "../../components/ui/Page";
import { ErrorBox, Loading } from "../../components/ui/States";
import { ActiveBadge } from "../../components/ui/Badge";
import useLoad from "../../hooks/useLoad";
import { parkingLotsApi } from "../../api/client";

// 회사 관리자용 주차장 목록. 조회 전용이다.
// 주차장, 구역, 장비, 담당 관리자 변경은 서비스 운영자만 할 수 있다.
export default function ParkingLots() {
  const loader = useCallback(() => parkingLotsApi.list(), []);
  const { data, loading, error, reload } = useLoad(loader);
  const lots = (data && data.items) || [];

  return (
    <Page
      title="주차장"
      subtitle="우리 회사의 주차장 목록입니다. 이름을 누르면 구역, 장비, 담당 관리자를 볼 수 있습니다. 변경은 서비스 운영자에게 요청해 주세요."
    >
      <section className="pg-section">
        <ErrorBox message={error} onRetry={reload} />
        {loading && !data && <Loading />}
        {data && (
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
                    <th>담당 관리자 수</th>
                  </tr>
                </thead>
                <tbody>
                  {lots.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="pg-empty">
                        등록된 주차장이 없습니다.
                      </td>
                    </tr>
                  ) : (
                    lots.map((lot) => (
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
                        <td>{(lot.admin_ids || []).length}</td>
                      </tr>
                    ))
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
