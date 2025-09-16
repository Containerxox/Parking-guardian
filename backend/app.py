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
    uid = (data.get("user_id") or "").strip()
    pw = data.get("password") or ""

    # 쿼리 실행
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, user_id, password_hash, role FROM users WHERE user_id=?",(uid,)
        ).fetchone()

    # id 존재하지 않는 경우
    if not row:
        return jsonify({"ok": False, "error": "존재하지 않는 아이디입니다."}),200 # 보안을 위해 아이디/비밀번호 불일치로 바꿀 예정
    
    # 비밀번호 해시 처리
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

# 간단 루트
@app.route("/", methods=["GET"])
def index():
    return "Hello from Flask! 🌱", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)