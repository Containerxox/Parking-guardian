# app.py
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

# 간단 루트
@app.route("/", methods=["GET"])
def index():
    return "Hello from Flask! 🌱", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)