"""Entry point.

Local development:
    python wsgi.py            (http://127.0.0.1:5000)
    flask --app wsgi db upgrade
    flask --app wsgi seed
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
