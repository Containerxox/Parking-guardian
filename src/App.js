import React from 'react';
import { BrowserRouter, Routes, Route } from "react-router-dom"; //npm install react-router-dom
import UserDashboard from './pages/user/UserDashboard';
import AdminDashboard from './pages/admin/AdminDashboard';
import Login from './pages/auth/Login';
import UserRegistration from './pages/admin/UserRegistration';



function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Login />} />
        <Route path="/admin-dashboard" element={<AdminDashboard />} />
        <Route path="/user-dashboard" element={<UserDashboard />} />
        <Route path="/user-registration" element={<UserRegistration />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;

