# app.py
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import sqlite3
import bcrypt

# 내 DB의 저장 경로
DB_PATH=r"C:\sqlite\parking_guardian.db" 

app = Flask(__name__)
CORS(app)

# DB 연결
def get_db():
    conn =sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        return jsonify({"ok": False, "error": "존재하지 않는 아이디입니다."}),200 # 보안을 위해 아이디/비밀번호 불일치로 바꿀 예정
    
    # 해시 처리된 비밀번호를 bytes로 인코딩
    pw_hash = row["password_hash"]
    if isinstance(pw_hash,str):
        pw_hash = pw_hash.encode("utf-8")

    # pw 틀린 경우
    if not bcrypt.checkpw(pw.encode("utf-8"), pw_hash):
        return jsonify({"ok":False, "error":"비밀번호 불일치"}),200 # 보안을 위해 아이디/비밀번호 불일치로 바꿀 예정

    # id와 pw 모두 정상적으로 일치
    return jsonify({
        "ok": True,
        "user": {"id":row["id"], "user_id": row["user_id"],"role":row["role"]}
    }),200



# /user-register API
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




# 간단 루트
@app.route("/", methods=["GET"])
def index():
    return "Hello from Flask! 🌱", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)