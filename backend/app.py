# app.py
from flask import Flask, request, jsonify, session, g
from flask_cors import CORS
import sqlite3, bcrypt
from functools import wraps

# ====================================================================================================
# 클라우드 (배포 용) DB 저장 경로
# DB_PATH=r"backend\parking_guardian.db"
# ====================================================================================================

# 로컬 (개발 용) DB 저장 경
DB_PATH=r"C:\sqlite\parking_guardian.db"

app = Flask(__name__)
app.secret_key = "capstone-team3-random" # 세션에 사용될 랜덤키 (나중에 환경변수로 관리할 예정)

# 로컬 개발용 (Http) (front:3000 <-> back:5000) 
app.config["SESSION_COOKIE_SAMESITE"] = "Lax" # 교차사이트 요청엔 쿠키 전송 X
app.config["SESSION_COOKIE_SECURE"] = False # HTTP에서는 쿠키 전송 X (HTTPS에서만 쿠키 전송 O)
ALLOWED_ORIGINS = ["http://localhost:3000"] # Frontend 3000번 포트만 허용

# ====================================================================================================
# 클라우드 용 (Https) (front:3000 <-> back:5000) 
# app.config["SESSION_COOKIE_SAMESITE"] = "None" #교차사이트 요청에도 쿠키 전송 O
# app.config["SESSION_COOKIE_SECURE"] = True # HTTPS에서만 쿠키 전송 O
# ALLOWED_ORIGINS = ["https://capston-bajen.run.goorm.site"]
# ====================================================================================================

CORS(
    app, 
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},
    allow_headers=["Content-Type", "Authorization"], # 요청에서 허용할 헤더
    methods=["POST","GET","OPTIONS","DELETE"], # 허용할 메서드
    supports_credentials=True, # 세션/쿠키 전송 허용
    )

# DB 연결
def get_db():
    conn =sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# 로그인 여부 확인 (세션 확인)
def require_login(f):
    @wraps(f)
    def wrapper(*args,**kwargs):
        if "uid" not in session:
            return jsonify({"ok": False, "error": "로그인이 필요합니다."}),401
        g.user_id = session.get("user_id")
        g.role = session.get("role")
        return f(*args,**kwargs)
    return wrapper

# role(권한)이 관리자인지 확인
def require_admin(f):
    @wraps(f)
    @require_login
    def wrapper(*args, **kwargs):
        if g.role != "admin":
            return jsonify({"ok": False, "error": "접근 권한이 없습니다."}),403
        return f(*args, **kwargs)
    return wrapper

# /login API
@app.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    uid = data.get("user_id").strip()
    pw = data.get("password")

    # 쿼리 실행
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, user_id, password_hash, role FROM users WHERE user_id=?",(uid,)
        ).fetchone()

    # id 존재하지 않는 경우
    if not row:
        return jsonify({"ok": False, "error": "아이디 또는 비밀번호가 불일치합니다."}),200 # 
    
    # 해시 처리된 비밀번호를 bytes로 인코딩 (비밀번호 검사)
    pw_hash = row["password_hash"]
    if isinstance(pw_hash,str):
        pw_hash = pw_hash.encode("utf-8")

    # pw 틀린 경우
    if not bcrypt.checkpw(pw.encode("utf-8"), pw_hash):
        return jsonify({"ok":False, "error":"아이디 또는 비밀번호가 불일치합니다."}),200 
    
    # 세션 발급
    session.clear()
    session["uid"] = row["id"]
    session["user_id"] = row["user_id"]
    session["role"] = row["role"]

    # id와 pw 모두 정상적으로 일치
    return jsonify({
        "ok": True,
        "user": {"id":row["id"], "user_id": row["user_id"],"role":row["role"]}
    }),200


# /session (세션)
@app.get("/session")
def get_session():
    if "uid" not in session:
        return jsonify({"ok":False, "user":None}),200
    return jsonify({
        "ok":True,
        "user":{"id":session.get("uid"), "user_id": session.get("user_id"), "role":session.get("role")}
    }),200


# /logout (로그아웃)
@app.post("/logout")
def logout():
    session.clear() # 세션 초기화
    return jsonify({"ok": True}),200


# /user-register API (고객 회원가입)
@app.post("/user-register")
def register():
    data = request.get_json(silent=True) or {}
    uid = data.get("user_id").strip()
    pw = data.get("password") 
    building_name = data.get("building_name").strip()
    building_addr = data.get("building_address").strip()

    # 비밀번호 해시 처리
    pw_hash = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt())

    try:
        with get_db() as conn:
            cur = conn.cursor()

            building_id = None
            # 입력받은 건물명, 건물주소를 buildings 테이블에 삽입
            cur.execute("INSERT INTO buildings(name, address) VALUES(?, ?)",(building_name,building_addr))
            # 입력받은 건물명, 건물주소가 저장된 행을 조회하여 해당 행의 id 컬럼 값(=건물ID)을 가져옴
            cur.execute("SELECT id FROM buildings WHERE name=? AND address=?", (building_name, building_addr))
            row_b = cur.fetchone()
            if not row_b:
                return jsonify({"ok": False, "error": "건물 ID 조회에 실패했습니다."}),200
            # 건물ID를 building_id 변수에 저장
            building_id = row_b["id"] if row_b else None 
            
            # users 테이블에 ID, PW, 건물ID를 삽입
            cur.execute("INSERT INTO users (user_id, password_hash, role, building_id) VALUES (?, ?, 'user', ?)",(uid,pw_hash, building_id))

    except sqlite3.IntegrityError:
        # users.user_id UNIQUE, buildings UNIQUE(name,address) 충돌 발생하는 경우
        return jsonify({"ok": False, "error":"이미 존재하는 아이디 또는 건물입니다."}),200
    
    except Exception as e:
        return jsonify({"ok":False, "error":"서버 오류"}),200
    
    return jsonify({"ok":True, "message": "회원가입이 완료되었습니다."})


# /machine-register API
# 입력 예시 : {"username": "홍길동","machine_id": "abdfvadafefew"}
@app.post("/machine-register")
def register_machine():
    # 1. 클라이언트로부터 JSON 데이터를 받습니다.
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    machine_id = data.get("machine_id")

    # 2. 필수 정보가 누락되었는지 확인합니다.
    if not username or not machine_id:
        return jsonify({"ok": False, "error": "사용자 이름과 기기 번호를 모두 입력해주세요."}), 200

    # 3. DB에 데이터를 저장합니다.
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO machine (username, machine_id) VALUES (?, ?)",
                (username.strip(), machine_id.strip())
            )
            # conn.commit() is called automatically by 'with' statement
            
    except sqlite3.IntegrityError:
        # machine_id가 UNIQUE 제약 조건을 위반할 경우 (이미 존재할 경우)
        return jsonify({"ok": False, "error": "이미 등록된 기기 번호입니다."}), 200
    
    except Exception as e:
        # 기타 예상치 못한 서버 오류 처리
        # print(f"An error occurred: {e}") # 디버깅용
        return jsonify({"ok": False, "error": "서버 처리 중 오류가 발생했습니다."}), 500

    # 4. 성공적으로 등록된 경우
    return jsonify({"ok": True, "message": "기기 등록이 완료되었습니다."}), 200


# /admin/users-devices API  
# (AdminDashboard에서 표현될 전체 사용자 정보) ( idx | 사용자ID | 건물ID | 주소 | 설치기기수 )
@app.get("/admin/users-devices")
@require_admin # admin만 호출 가능
def admin_users_devices():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT
                    u.id          AS idx,
                    u.user_id     AS user_id,
                    u.building_id AS building_id,
                    b.address     AS address,
                    COUNT(m.id)   AS device_count
                FROM users u
                LEFT JOIN buildings b
                ON b.id = u.building_id
                LEFT JOIN machine m
                ON m.username = u.user_id
                WHERE u.role = 'user' 
                GROUP BY u.id, u.user_id, u.building_id, b.address
                ORDER BY u.id
            """).fetchall()

        data = [
            {
                "idx": r["idx"],
                "user_id": r["user_id"],
                "building_id": r["building_id"],         
                "address": r["address"],                  
                "device_count": int(r["device_count"]),   
            }
            for r in rows
        ]
        return jsonify({"ok": True, "rows": data}), 200
    
    except sqlite3.DatabaseError as e:
        app.logger.exception("DatabaseError in /admin/users-devices")
        return jsonify({
            "ok":False,
            "error":"데이터베이스 오류가 발생했습니다."
        }),500
    
    except Exception as e:
        app.logger.exception("Unhandled error in /admin/users-devices")
        return jsonify({
            "ok":False,
            "error":"서버 처리 중 오류가 발생했습니다."
        }),500
    
# /admin/user-delete/<user_id> API
# AdminDashboard에서 관리자가 사용자 단위로 삭제
# ㄴ 해당 user_id 행만 제거, 같은 건물의 다른 사용자는 유지 (다만, 같은 건물을 사용하는 다른 사용자가 없으면 건물도 함께 삭제시킴)
# ㄴ 해당 사용자의 machine은 FK CASCADE로 자동 삭제
# ㄴ 그 사용자의 건물을 중복 사용하는 다른 사용자가 0명일 때만 buildings에서도 삭제 
@app.delete("/admin/user-delete/<user_id>")
@require_admin
def admin_delete_user(user_id: str):
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # 대상 사용자ID 조회
            u = cur.execute(
                "SELECT user_id, building_id FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not u:
                return jsonify({"ok": False, "error": "해당 사용자가 존재하지 않습니다."}), 404

            building_id = u["building_id"]

            # username(=사용자ID)을 기반으로 삭제될 machine 수 집계
            machine_cnt = cur.execute(
                "SELECT COUNT(*) AS c FROM machine WHERE username = ?",
                (user_id,)
            ).fetchone()["c"]

            # 사용자ID 삭제 -> machine은 CASCADE로 자동 삭제됨.
            cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            users_deleted = cur.rowcount or 0

            # 건물 orphan 여부 확인 후 정리
            buildings_deleted = 0
            if building_id is not None:
                remain = cur.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE building_id = ?",
                    (building_id,)
                ).fetchone()["c"]
                if remain == 0:
                    cur.execute("DELETE FROM buildings WHERE id = ?", (building_id,))
                    buildings_deleted = cur.rowcount or 0

        return jsonify({
            "ok": True,
            "deleted": {
                "users": users_deleted,
                "machines": machine_cnt,
                "buildings": buildings_deleted
            }
        }), 200

    except Exception:
        return jsonify({"ok": False, "error": "서버 처리 중 오류가 발생했습니다."}), 500

# 간단 루트
@app.route("/", methods=["GET"])
def index():
    return "Hello from Flask! 🌱", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)