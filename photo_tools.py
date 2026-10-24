"""Photo measuring for the app: keeps OpenCV in the CadQuery Python and runs photo_worker.py. Standard library only."""
from __future__ import annotations

import base64
import json
import subprocess
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENCV = "opencv-python-headless==4.11.0.86"
STATE = {"status": "unknown", "detail": ""}
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def has_opencv(cad_python):
    try:
        r = subprocess.run([cad_python, "-I", "-c", "import cv2, cv2.aruco"], capture_output=True, text=True, timeout=90, creationflags=FLAGS)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure(cad_python, log_path):
    """Install OpenCV into the CadQuery Python once, in the background (no console window)."""
    try:
        if has_opencv(cad_python):
            STATE.update(status="ready", detail="")
            return
        STATE.update(status="installing", detail="Adding the photo measuring tools (OpenCV, about 40 MB)…")
        # --no-deps: numpy is already there, and OpenCV must not change the numpy CadQuery uses.
        r = subprocess.run([cad_python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", OPENCV],
                           capture_output=True, text=True, timeout=1200, creationflags=FLAGS)
        Path(log_path).write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
        ok = has_opencv(cad_python)
        STATE.update(status="ready" if ok else "missing", detail="" if ok else "OpenCV didn't install: " + (r.stderr or r.stdout)[-300:])
    except Exception as exc:  # never take the app down over an optional tool
        STATE.update(status="missing", detail=str(exc)[-300:])


def start(cad_python, log_path):
    threading.Thread(target=ensure, args=(cad_python, log_path), daemon=True).start()


def run(cad_python, *args, timeout=120):
    r = subprocess.run([cad_python, "-I", "-B", str(ROOT / "photo_worker.py"), *map(str, args)],
                       capture_output=True, text=True, timeout=timeout, creationflags=FLAGS)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout)[-800:]
        if "No module named 'cv2'" in detail:
            raise ValueError("The photo measuring tools are still installing. Try again in a minute.")
        raise ValueError("Measuring failed: " + detail)
    return json.loads(r.stdout)


def card(cad_python, data_dir: Path) -> Path:
    out = data_dir / "reference-card.pdf"
    if not out.is_file() or out.stat().st_mtime < (ROOT / "photo_worker.py").stat().st_mtime:
        run(cad_python, "card", out)
    return out


def measure(cad_python, data_dir: Path, image: bytes, suffix=".jpg"):
    work = data_dir / "measure"
    work.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    photo, overlay = work / (name + suffix), work / (name + "-overlay.png")
    photo.write_bytes(image)
    try:
        result = run(cad_python, "measure", photo, overlay)
        if overlay.is_file():
            result["overlay"] = "data:image/png;base64," + base64.b64encode(overlay.read_bytes()).decode()
        return result
    finally:
        photo.unlink(missing_ok=True)
        overlay.unlink(missing_ok=True)
