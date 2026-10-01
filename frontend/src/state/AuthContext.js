import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { authApi, clearAccessToken, refreshSession, setAuthHandlers } from "../api/client";

const AuthCtx = createContext(null); // 로그인 정보를 앱 전역에 공유하는 통로
export const useAuth = () => useContext(AuthCtx);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null); // 로그인한 Admin 객체
  const [ready, setReady] = useState(false); // 초기 세션 복구 시도가 끝났는지

  useEffect(() => {
    let alive = true;
    setAuthHandlers({
      // Refresh까지 실패하면 로그인 상태를 지운다. ProtectedRoute가 로그인 화면으로 보낸다.
      onUnauthorized: () => setUser(null),
      onRefreshed: (admin) => {
        if (admin) setUser(admin);
      },
    });

    // 앱 시작 시 Refresh 쿠키로 세션 복구. 실패는 "로그인 안 됨"으로 조용히 처리한다.
    refreshSession()
      .catch(() => {})
      .finally(() => {
        if (alive) setReady(true);
      });

    return () => {
      alive = false;
      setAuthHandlers({});
    };
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await authApi.login(email, password);
    setUser(data.admin);
    return data.admin;
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch (e) {
      // 서버 로그아웃이 실패해도 화면에서는 로그아웃 처리한다.
    } finally {
      clearAccessToken();
      setUser(null);
    }
  }, []);

  const value = useMemo(() => ({ user, ready, login, logout }), [user, ready, login, logout]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
