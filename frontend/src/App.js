import React from 'react';
import { BrowserRouter, Routes, Route } from "react-router-dom"; //npm install react-router-dom
import { AuthProvider } from './state/AuthContext';
import ProtectedRoute from './routes/ProtectedRoute';

import UserDashboard from './pages/user/UserDashboard';
import AdminDashboard from './pages/admin/AdminDashboard';
import Login from './pages/auth/Login';
import UserRegistration from './pages/admin/UserRegistration';
import Forbidden403 from "./pages/status/Forbidden403";



function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Login />} />

          {/* 로그인한 admin만 접근 허용*/}
          <Route path="/admin-dashboard" element={<ProtectedRoute role="admin"><AdminDashboard /></ProtectedRoute>} />
          {/* 로그인한 user 접근 허용 */}
          <Route path="/user-dashboard" element={<ProtectedRoute role="user"><UserDashboard /></ProtectedRoute>} />
          <Route path="/user-registration" element={<UserRegistration />} />
          <Route path="*" element={<Login/>} />
          <Route path="/403" element={<Forbidden403 />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
    
  );
}

export default App;

