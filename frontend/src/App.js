import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./state/AuthContext";
import ProtectedRoute from "./routes/ProtectedRoute";

import Login from "./pages/auth/Login";
import Forbidden403 from "./pages/status/Forbidden403";

import Companies from "./pages/super/Companies";
import CompanyDetail from "./pages/super/CompanyDetail";
import SuperViolations from "./pages/super/SuperViolations";
import SuperMachines from "./pages/super/SuperMachines";
import AuditLogs from "./pages/super/AuditLogs";

import Violations from "./pages/company/Violations";
import ParkingLots from "./pages/company/ParkingLots";
import ParkingLotDetail from "./pages/company/ParkingLotDetail";
import Notifications from "./pages/company/Notifications";

const SUPER = ["SUPER_ADMIN"];
const COMPANY = ["COMPANY_ADMIN"];
const BOTH = ["SUPER_ADMIN", "COMPANY_ADMIN"];

const guard = (roles, element) => <ProtectedRoute roles={roles}>{element}</ProtectedRoute>;

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Login />} />
          <Route path="/403" element={<Forbidden403 />} />

          {/* SUPER_ADMIN 전용 */}
          <Route path="/super/companies" element={guard(SUPER, <Companies />)} />
          <Route path="/super/companies/:id" element={guard(SUPER, <CompanyDetail />)} />
          <Route path="/super/violations" element={guard(SUPER, <SuperViolations />)} />
          <Route path="/super/machines" element={guard(SUPER, <SuperMachines />)} />
          <Route path="/super/audit-logs" element={guard(SUPER, <AuditLogs />)} />

          {/* COMPANY_ADMIN 전용 */}
          <Route path="/violations" element={guard(COMPANY, <Violations />)} />
          <Route path="/parking-lots" element={guard(COMPANY, <ParkingLots />)} />
          <Route path="/notifications" element={guard(COMPANY, <Notifications />)} />

          {/* 주차장 상세는 두 Role이 함께 쓴다 */}
          <Route path="/parking-lots/:id" element={guard(BOTH, <ParkingLotDetail />)} />

          {/* 그 밖의 주소는 로그인 페이지로 */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
