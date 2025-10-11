import React from "react";
import {Navigate} from "react-router-dom";
import {useAuth} from "../state/AuthContext";

export default function ProtectedRoutes({children, role}){
    const {user, ready} = useAuth();

    // 세션 확인 중인 경우(아직 /session API가 끝나지 않은 상태) 로딩 중..을 띄움
    if(!ready) return <div>로딩 중..</div>;

    // 로그인을 안한 상태 -> / 페이지(로그인 페이지)로 강제 이동
    if (!user) return <Navigate to="/" replace />;

    // 권한(role) 체크 (접근 권한이 없는 경우, /403 페이지로 이동) 
    if (role && user.role !== role){
        return <Navigate to="/403" replace />;
    }

    // 위의 if 조건 다 통과 (로그인 + 권한 조건 충족)한 경우, 원래 컴포넌트 렌더링함.
    return children;
}