import os
import sys

_ROOT = os.path.dirname(__file__)
# repo root on path -> `from backend import app` works (app.py uses package-relative imports);
# backend/ on path -> flat-module imports like `from session import Session` work too.
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))
# app.py mounts StaticFiles(directory=STATIC_DIR) at import; point it at the real static dir so
# importing the app in tests doesn't fail on the production default (/app/static).
os.environ.setdefault("STATIC_DIR", os.path.join(_ROOT, "static"))
