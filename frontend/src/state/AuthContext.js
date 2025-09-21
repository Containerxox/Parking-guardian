import React, {createContext, useContext, useEffect, useState} from "react"

// =========================================================================
// 클라우드 (배포용)
// const API_BASE = "https://capston-bajen.run.goorm.site";
// =========================================================================

// 로컬 (개발용)
const API_BASE = "http://localhost:5000";

const AuthCtx = createContext(null); // AuthCtx: “로그인 정보”를 앱 전역에 공유할 통로
export const useAuth = () => useContext(AuthCtx)

export function AuthProvider({children}) {
    const [user,setUser] = useState(null) // user에 로그인한 사용자 정보 담김
    const [ready, setReady] = useState(false) //ready에 초기 세션 체크 끝났는지를 여부를 T/F로 담음

    useEffect(() => {
        (async () => {
            try {
                const res = await fetch(`${API_BASE}/session`, { credentials: "include" });
                const data = await res.json();
                if(data.ok) setUser(data.user);
            }finally{
                setReady(true);
            }
        })();
    },[]);

    // 로그아웃 
    const logout = async () => {
        try{
            await fetch(`${API_BASE}/logout`, {
                method:"POST",
                credentials: "include",
            });
        }finally{
            setUser(null)
        }
    };


    return (
        <AuthCtx.Provider value={{user, setUser, ready, logout}}>
            {children}
        </AuthCtx.Provider>
    );
}
