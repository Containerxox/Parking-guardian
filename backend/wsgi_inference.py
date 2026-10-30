"""Entry point of the inference service.

Container (Linux):  gunicorn -c gunicorn_inference.conf.py wsgi_inference:app
Local (Windows, where gunicorn does not run):  python wsgi_inference.py   (http://127.0.0.1:8000)
"""
import os

from inference_service.runtime import apply_thread_env

# Before anything imports torch (models load lazily, so this is early enough). The service runs 1 thread by
# default; the Prototype API process never goes through this file and keeps the torch default.
apply_thread_env(int(os.environ.get("TORCH_NUM_THREADS") or 1))

from inference_service import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    from inference_service.runtime import start_background

    start_background(app)
    app.run(host=os.environ.get("FLASK_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
            threaded=True)  # same "one running + a few waiting" load as gunicorn thread pool
