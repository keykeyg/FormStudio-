"""Form Studio: a loopback-only, dependency-free local photo/chat-to-CAD app."""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import http.client
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import mesh_mode  # noqa: E402
import photo_tools  # noqa: E402
import slicer  # noqa: E402
# Projects live next to the app, so they're easy to find and back up. FORM_STUDIO_DATA overrides.
DATA = Path(os.environ.get("FORM_STUDIO_DATA") or (Path(__file__).resolve().parent / "data"))
LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
CONNS: dict[str, http.client.HTTPConnection] = {}
TOKEN = secrets.token_urlsafe(32)
PREVIEW = False
REMOTE = False  # set at startup from settings: also listen on the home network (passcode required)
PORT = 8765
LOGIN_FAILS: dict[str, list] = {}
DEFAULT_RUNNER = str(Path.home() / "mcp-servers" / "overnight-ai")
DEFAULT_LMS = str(Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms"))
# Same switches the overnight runner sends: Qwen thinking burns the reply budget before any code is written.
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
# Windows' registry often maps .js to text/plain, which browsers refuse for module scripts.
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".json": "application/json",
                 ".py": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8", ".stl": "model/stl", ".step": "application/step"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class Cancelled(Exception):
    pass


SYSTEM = '''You have NO tools. All files and context are included below. You are a collaborative CAD designer running locally.
You see a reference photo, the conversation, current CadQuery source and renders when available.
Reply format: a short natural-language message, then (for a design request) exactly ONE ```python fenced block with the complete program.
For a design request, return full executable CadQuery source, not a patch. For questions, missing critical measurements, or a discussion, reply with the message only and no code block.
Preserve all features and dimensions the user did not ask to change. Current source is authoritative, even if older conversation differs.
Use millimeters, Z-up, named UPPERCASE numeric parameters at the top. Center the finished part on the origin in X and Y with its base at Z=0, so features placed at known coordinates land on it. Never treat visual estimates as measured dimensions. Explain any estimates briefly.
Only imports allowed: import cadquery as cq; import math. Assign the finished solid(s) to result (cq.Workplane or cq.Shape).
No file access, exporters, importers, network, shell, GUI, print, show_object, package installs, or private attributes.
Use ordinary CadQuery 2.x APIs: Workplane, box, circle, rect, slot2D, polyline, close, extrude, cut, cutBlind, union, hole, fillet, chamfer, shell, mirror, faces, edges, workplane, translate, rotate, loft, revolve.
Fillets and chamfers fail easily: keep radii small, apply them last, select edges simply, and you may wrap a fillet in try/except so a failed fillet doesn't lose the part.
For an extruded side profile, draw it on Workplane("XZ") and use .extrude(WIDTH / 2, both=True): that centers the part on Y=0, so bosses and holes placed at Y=0 land on it.
To get a side profile right, trace it: look at the photo's side view and list 8-20 (x, z) outline points in mm, in order around the outline, as a PROFILE = [...] parameter, noting where the part is thin and where it is deep. Build it with .polyline(PROFILE).close(). Don't approximate an outline with boxes and circles stuck together.
CadQuery details that trip people up: a plain Workplane("XZ").extrude(d) goes toward -Y (its normal is -Y), Workplane("XY") toward +Z, Workplane("YZ") toward +X; check that parts you union actually overlap, because union only fuses solids that touch. translate and moves take ONE tuple, e.g. .translate((0, -W/2, 0)). For several holes use .pushPoints([(x1, y1), (x2, y2)]).hole(d) instead of chained .center() calls (center() is cumulative). On faces(">Z").workplane() the origin is the global origin projected onto that face, so use global X/Y positions.
Only use modest bounded loops (under 1000). Avoid expensive meshes and tessellation in generated code.
Prefer robust simple geometry. For multiple disconnected solids use cq.Compound.makeCompound([...]) or Workplane().newObject([...]). If the design is meant to be several separate parts (a box and its lid), add PARTS = <count> so the checker knows.
A car photo can be approximated as parametric solid geometry; do not promise exact invisible surfaces or a detailed sculpt. Ask whether approximation is acceptable when it matters.
If a previous execution failed, fix that error and return the complete corrected source. Never claim a successful export before execution.
'''
SAMPLE = '''import cadquery as cq
WIDTH = 80.0
DEPTH = 40.0
THICKNESS = 5.0
HOLE_DIAMETER = 6.0
HOLE_SPACING = 60.0
CORNER_RADIUS = 3.0
result = cq.Workplane("XY").box(WIDTH, DEPTH, THICKNESS)
result = result.edges("|Z").fillet(CORNER_RADIUS)
result = result.faces(">Z").workplane().pushPoints([(-HOLE_SPACING/2,0),(HOLE_SPACING/2,0)]).hole(HOLE_DIAMETER)
'''


def fit_rules(cfg):
    c = float(cfg.get("fit_clearance", 0.2))
    return (f"\nPRINT FIT (this prints on a Bambu Lab X2D): for anything that takes a screw, pin, rail, insert or another part, add CLEARANCE = {c:g} "
            "to the hole diameter or slot width and keep it as a named parameter. Screw clearance holes: M2 2.4, M2.5 2.9, M3 3.4, M4 4.5, M5 5.5, "
            "M6 6.6, #8 4.5, #10 5.3 mm. Heat-set inserts (typical, check the insert): M2 3.2 mm hole x 4 mm deep, M2.5 3.6 x 5, M3 4.0 x 5.5, "
            "M4 5.6 x 8, M5 6.4 x 10. Hex nut traps across flats: M3 5.5, M4 7.0, M5 8.0, M6 10.0 mm, plus CLEARANCE. "
            "Keep walls at least 1.2 mm and avoid unsupported overhangs past 45 degrees where you can.\n")


MESH_EDIT = """
THIS VERSION IS A SCULPTED MESH, so it has no CAD source and you can't change the mesh itself.
To change it, write CadQuery that defines `cut` (solids to remove: holes, slots, a flat base, a recess) and/or `add` (solids to add: a mounting tab, a boss, a rail), positioned in the mesh's own coordinates. Do not assign result.
Mesh bounding box: X {x0:g}..{x1:g}, Y {y0:g}..{y1:g}, Z {z0:g}..{z1:g} mm (centered in X and Y, base at Z=0). The renders below show the mesh.
Example: cut = cq.Workplane("XY").pushPoints([(-20, 0), (20, 0)]).circle(2.7).extrude(200).translate((0, 0, -50))
"""


def atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


def defaults():
    candidates = [os.environ.get("PHOTO2CAD_CAD_PYTHON", ""), str(Path(DEFAULT_RUNNER) / "cadenv" / "Scripts" / "python.exe"), str(Path(DEFAULT_RUNNER) / "cadenv" / "python.exe")]
    cad_python = next((x for x in candidates if x and Path(x).is_file()), sys.executable)
    return {"lm_url": "http://127.0.0.1:1234", "cad_python": cad_python, "runner_root": DEFAULT_RUNNER, "lms_path": DEFAULT_LMS,
            "bed": [256, 256, 260], "timeout": 900, "max_tokens": 8192, "disable_thinking": True,
            "max_attempts": 4, "self_review_rounds": 3, "bambu_path": "",
            "comfy_root": mesh_mode.default_comfy_root(), "comfy_url": "http://127.0.0.1:8188", "mesh_workflow": str(ROOT / "workflows" / "trellis2_mesh.json"),
            "fit_clearance": 0.2, "material": "PLA", "walls": 2, "infill": 15}


def config():
    c = defaults()
    if (DATA / "settings.json").is_file():
        c.update(json.loads((DATA / "settings.json").read_text(encoding="utf-8")))
    return c


def public_config():
    c = {k: v for k, v in config().items() if not k.startswith("remote_pass")}
    c["remote_passcode_set"] = bool(config().get("remote_passcode_hash"))
    c["remote_active"] = REMOTE
    c["remote_urls"] = [f"http://{ip}:{PORT}" for ip in lan_addresses()] if REMOTE or c.get("remote_access") else []
    return c


def hash_passcode(code, salt=None):
    import hashlib
    salt = salt or secrets.token_hex(16)
    return salt, hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), 200_000).hex()


def lan_addresses():
    """This PC's home-network (and Tailscale) addresses, for the phone URL."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the outward-facing interface
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def session_store():
    try:
        return set(json.loads((DATA / "remote_sessions.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def remember_session(token):
    import hashlib
    hashes = session_store() | {hashlib.sha256(token.encode()).hexdigest()}
    atomic(DATA / "remote_sessions.json", sorted(hashes)[-50:])


def local_url(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme != "http" or p.hostname not in ("localhost", "127.0.0.1", "::1") or p.username or p.password or p.query or p.fragment or p.path not in ("", "/", "/v1"):
        raise ValueError("LM Studio address must be a local HTTP address, such as http://127.0.0.1:1234")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "", "", ""))


def runner_config(cfg):
    try:
        return json.loads((Path(cfg["runner_root"]) / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def lm_headers(cfg):
    """LM_API_TOKEN, else the token the overnight runner already uses. Never sent to the browser."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("LM_API_TOKEN") or runner_config(cfg).get("lmstudio_api_token") or ""
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def loadable_models(cfg):
    """Models the overnight runner is configured to use, with its exact load settings."""
    rc = runner_config(cfg)
    out = []
    for m in rc.get("models", []):
        if m.get("enabled", True) is False or not m.get("key") or str(m["key"]).upper().startswith("REPLACE"):
            continue
        out.append({"name": m.get("name", m["key"]), "key": m["key"], "context_length": m.get("context_length")})
    return out


def lm_request(cfg, path, payload=None, timeout=5):
    headers = lm_headers(cfg)
    req = urllib.request.Request(local_url(cfg["lm_url"]) + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    # Never send local requests through a system HTTP proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


def loaded_models(cfg):
    errors = []
    for path in ("/api/v1/models", "/api/v0/models"):
        try:
            with lm_request(cfg, path) as r:
                data = json.load(r)
            models = []
            if path.startswith("/api/v1"):
                for model in data.get("models", []):
                    if model.get("type") not in ("llm", "vlm"):
                        continue
                    for instance in model.get("loaded_instances", []):
                        models.append({"id": instance["id"], "name": model.get("display_name", instance["id"]), "vision": model.get("capabilities", {}).get("vision")})
            else:
                models = [{"id": m["id"], "name": m["id"], "vision": True if m.get("type") == "vlm" else None} for m in data.get("data", []) if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")]
            return models
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("Cannot read loaded models from LM Studio. Start its local server. " + errors[-1])


def vision_by_key(cfg):
    """Every downloaded model's vision flag (LM Studio sets it when the model's mmproj file is present)."""
    try:
        with lm_request(cfg, "/api/v1/models") as r:
            data = json.load(r)
        return {m.get("key"): bool((m.get("capabilities") or {}).get("vision")) for m in data.get("models", []) if m.get("key")}
    except Exception:
        return {}


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        # Never os.kill() on Windows: any signal there terminates the process.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


RECEIPT_TERMINAL = {"accepted", "blocked", "completed", "failed", "rejected", "rolled_back", "cancelled", "canceled", "success", "passed", "done"}
RECEIPT_ACTIVE = {"running", "drafting", "reviewing", "testing", "executing", "in_progress", "in-progress", "draft", "review", "final"}


def runner_status(cfg):
    """Read-only guard: the runner's PID file, recent build receipts, and Windows Task Scheduler state."""
    root = Path(cfg["runner_root"])
    active = []
    pid_file = root / "state" / "run_night.pid"
    try:
        pid = int((pid_file.read_text(encoding="utf-8-sig").split() or ["0"])[0])
        if pid_alive(pid):
            active.append(f"overnight run (pid {pid})")
    except (OSError, ValueError):
        pass
    # Receipts: feature_state is the runner's field; status/state kept for older receipts.
    # Only recent ones count, so an old receipt left mid-stage can't block design sessions forever.
    paths = sorted((root / "state" / "builds").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200] if root.is_dir() else []
    for p in paths:
        try:
            if time.time() - p.stat().st_mtime > 6 * 3600:
                continue
            receipt = json.loads(p.read_text(encoding="utf-8-sig"))
            state = str(receipt.get("feature_state") or receipt.get("status") or receipt.get("state") or "").lower()
            if state in RECEIPT_TERMINAL:
                continue
            stage = str(receipt.get("stage", receipt.get("phase", ""))).lower()
            if state in RECEIPT_ACTIVE or stage in RECEIPT_ACTIVE or state == "pending-review":
                active.append(p.parent.name)
        except (ValueError, OSError):
            continue
    task_checked = False
    if os.name == "nt":
        # Fixed script, no user strings interpolated; query only.
        script = "$ErrorActionPreference='Stop'; @(Get-ScheduledTask | Where-Object { $_.State -eq 'Running' -and (($_.TaskName -match 'overnight') -or (($_.Actions | Out-String) -match 'overnight-ai')) } | Select-Object -ExpandProperty TaskName) | ConvertTo-Json -Compress"
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            task_checked = r.returncode == 0
            if task_checked and r.stdout.strip():
                names = json.loads(r.stdout)
                active.extend(names if isinstance(names, list) else [names])
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return {"busy": bool(active), "active": active, "checked": root.is_dir() or task_checked, "root_found": root.is_dir()}


def require_idle(cfg):
    state = runner_status(cfg)
    if state["busy"]:
        raise ValueError("The overnight builder is active. Wait until it finishes before using LM Studio. " + ", ".join(state["active"]))


def project_path(pid):
    if not re.fullmatch(r"[a-f0-9]{32}", pid):
        raise ValueError("Invalid project")
    return DATA / "projects" / pid


def read_project(pid):
    return json.loads((project_path(pid) / "project.json").read_text(encoding="utf-8"))


def save_project(p):
    p["updated"] = time.time()
    atomic(project_path(p["id"]) / "project.json", p)


def public_project(p):
    return p


def new_project(name="Untitled design"):
    p = {"id": uuid.uuid4().hex, "name": str(name)[:100], "created": time.time(), "updated": time.time(), "messages": [], "versions": [], "active": None, "images": []}
    save_project(p)
    return p


def add_images(p, images):
    result = []
    for item in images[:3]:
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", item.get("data", ""))
        if not match:
            raise ValueError("Use a PNG, JPEG or WebP photo")
        raw = base64.b64decode(match[2], validate=True)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Each photo must be under 8 MB")
        mime = match[1]
        if (mime == "png" and not raw.startswith(b"\x89PNG\r\n\x1a\n")) or (mime == "jpeg" and not raw.startswith(b"\xff\xd8")) or (mime == "webp" and not (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
            raise ValueError("Photo contents do not match the image type")
        name = "image-" + uuid.uuid4().hex[:12] + "." + ("jpg" if mime == "jpeg" else mime)
        path = project_path(p["id"]) / name
        path.write_bytes(raw)
        entry = {"file": name, "name": str(item.get("name", "Reference photo"))[:150]}
        p["images"].append(entry)
        result.append(entry)
    return result


def image_content(path):
    mime = IMAGE_TYPES.get(Path(path).suffix.lower(), "image/png")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()}}


def parameters(code):
    params = {}
    for n in ast.parse(code).body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", n.targets[0].id):
            try:
                value = ast.literal_eval(n.value)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and n.targets[0].id != "PARTS":
                    params[n.targets[0].id] = value
            except (ValueError, TypeError):
                pass
    return params


FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n([\s\S]*?)```")


def parse_reply(raw):
    """Message text plus the last ```python block. JSON {"message","code"} is still accepted."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"^[\s\S]*?</think>", "", raw).strip()  # template that opened <think> before the reply
    if not raw:
        raise ValueError("The model returned an empty reply.")
    code, message_parts, last = None, [], 0
    for m in FENCE.finditer(raw):
        info, body = m[1].lower(), m[2]
        message_parts.append(raw[last:m.start()])
        last = m.end()
        if info in ("python", "py", "python3") or (info == "" and ("cadquery" in body or "result" in body)):
            code = body.strip()
        elif info == "json":
            message_parts.append(body)
    message_parts.append(raw[last:])
    if not code and re.search(r"```[ \t]*(python|py)\b", raw, re.I) and not re.search(r"```[ \t]*(python|py)\b[\s\S]*?```", raw, re.I):
        raise ValueError("The code block was cut off before it finished. Return the complete program in one ```python block.")
    if code:
        message = "\n".join(p.strip() for p in message_parts if p.strip()) or "I prepared a CAD revision."
        return {"message": message[:12000], "code": code}
    # Backward-compatible JSON reply; strict=False tolerates raw newlines inside the code string.
    for candidate in [raw] + [m[2] for m in FENCE.finditer(raw)]:
        start = candidate.find("{")
        if start >= 0:
            try:
                obj, _ = json.JSONDecoder(strict=False).raw_decode(candidate[start:])
                if isinstance(obj, dict) and isinstance(obj.get("message"), str) and (obj.get("code") is None or isinstance(obj.get("code"), str)):
                    return {"message": obj["message"][:12000], "code": obj.get("code") or None}
            except ValueError:
                pass
    # No code: a question or discussion.
    return {"message": raw[:12000], "code": None}


def update_job(jid, **values):
    with LOCK:
        JOBS[jid].update(values)


def cancelled(jid):
    with LOCK:
        return bool(JOBS.get(jid, {}).get("cancel"))


def infer(cfg, model, messages, jid):
    require_idle(cfg)
    loaded = {m["id"]: m for m in loaded_models(cfg)}
    if model not in loaded:
        raise ValueError("That model is not loaded. Use Load, or load it in LM Studio, then refresh.")
    has_images = any(isinstance(part, dict) and part.get("type") == "image_url"
                     for msg in messages if isinstance(msg.get("content"), list) for part in msg["content"])
    if has_images and loaded[model].get("vision") is False:
        # Never let a text-only model pretend it looked at the photos.
        raise ValueError(f"{loaded[model]['name']} can't see photos in LM Studio (its vision file, mmproj, is missing). Load a model marked as seeing photos.")
    payload = {"model": model, "messages": messages, "temperature": float(cfg.get("temperature", 0.2)), "max_tokens": int(cfg["max_tokens"]), "stream": True}
    if cfg.get("disable_thinking", True):
        payload.update(NO_THINK)
    parts = urllib.parse.urlsplit(local_url(cfg["lm_url"]))
    # A raw connection, so Stop can close the socket even while the model is still reading the prompt.
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=int(cfg["timeout"]))
    with LOCK:
        CONNS[jid] = conn
    pieces, reasoning = [], 0
    try:
        if cancelled(jid):
            raise Cancelled()
        conn.request("POST", "/v1/chat/completions", body=json.dumps(payload).encode(), headers=lm_headers(cfg))
        r = conn.getresponse()
        if r.status != 200:
            raise ValueError(f"LM Studio returned HTTP {r.status}: {r.read(3000).decode(errors='replace')}")
        while True:
            line = r.readline()
            if not line:
                break
            if cancelled(jid):
                raise Cancelled()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                raise ValueError(str(chunk["error"]))
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                value = delta.get("content")
                if isinstance(value, str):
                    pieces.append(value)
                think = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(think, str):
                    reasoning += len(think)
            update_job(jid, characters=sum(map(len, pieces)) + reasoning)
        return "".join(pieces)
    except (OSError, http.client.HTTPException) as exc:
        if cancelled(jid):
            raise Cancelled() from exc
        raise ValueError(f"Lost the connection to LM Studio: {exc}") from exc
    finally:
        with LOCK:
            CONNS.pop(jid, None)
        conn.close()


def cancel_job(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job or job.get("status") != "running":
            raise ValueError("No running job to stop")
        job["cancel"] = True
        job["phase"] = "Stopping"
        conn = CONNS.get(jid)
    if conn is not None and conn.sock is not None:
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked read; LM Studio stops generating
        except OSError:
            pass


def build_cad(cfg, code, destination):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "model.py").write_text(code, encoding="utf-8")
    cmd = [cfg["cad_python"], "-I", "-B", str(ROOT / "cad_worker.py"), str(destination / "model.py"), str(destination)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        raise ValueError("CAD Python was not found. Set the existing cadenv Python path in Settings.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("CAD execution exceeded 120 seconds. Simplify the design.") from exc
    (destination / "execution.log").write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout)[-5000:])
    meta = json.loads((destination / "metadata.json").read_text())
    return meta


def build_mesh_edit(cfg, code, base_folder, destination):
    """Mesh versions: build the model's cut/add solids, then apply them to the mesh."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "tools.py").write_text(code, encoding="utf-8")
    flow = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    steps = ([cfg["cad_python"], "-I", "-B", str(ROOT / "cad_worker.py"), str(destination / "tools.py"), str(destination), "--tools"],
             [cfg["cad_python"], "-I", "-B", str(ROOT / "mesh_ops.py"), str(base_folder / "model.stl"), str(destination), str(destination)])
    log = ""
    for cmd in steps:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=flow)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Applying the CAD change to the mesh took too long.") from exc
        log += r.stdout + "\n" + r.stderr + "\n"
        (destination / "execution.log").write_text(log, encoding="utf-8")
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout)[-5000:])
    meta = json.loads((destination / "metadata.json").read_text())
    meta.update(tools=True, base=base_folder.name)
    return meta


OVERALL_NAMES = {"LENGTH", "WIDTH", "HEIGHT", "DEPTH"}


def size_hints(params, dims):
    """Overall-size parameters that no bounding-box dimension matches (e.g. WIDTH = 30 but the part is 15 wide)."""
    hints = []
    for key, value in params.items():
        overall = key in OVERALL_NAMES or (key.split("_", 1)[0] in ("OVERALL", "TOTAL", "BODY") and key.rsplit("_", 1)[-1] in OVERALL_NAMES)
        if overall and value > 0 and not any(abs(d - value) <= max(0.5, 0.02 * value) for d in dims):
            hints.append(f"{key} = {value:g}, but no overall dimension matches it (bounding box X={dims[0]:g}, Y={dims[1]:g}, Z={dims[2]:g} mm).")
    return hints


PART_WORDS = re.compile(r"(hole|slot|boss|screw|bolt|wall|fillet|chamfer|pin|lip|tab|rib|groove|notch|pocket|cut|opening|thread|text|handle|hook|base|plate|flange)\w*\W+(\w+\W+){0,2}$", re.I)
UNIT = {"mm": 1.0, "cm": 10.0, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4}


def stated_sizes(texts):
    """Overall sizes the user typed, e.g. 'overall length is 100 mm', '80 mm wide'. Part-specific ones are skipped."""
    found = []
    label_first = re.compile(r"\b(length|long|width|wide|height|tall|depth|deep)\b[^0-9\n]{0,15}?(\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in\b|\")", re.I)
    number_first = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in\b|\")\s*(long|wide|tall|deep)\b", re.I)
    for text in texts:
        for m in label_first.finditer(text):
            if not PART_WORDS.search(text[max(0, m.start() - 30):m.start()]):
                found.append((m[1].lower(), round(float(m[2]) * UNIT[m[3].lower().strip()], 3)))
        for m in number_first.finditer(text):
            if not PART_WORDS.search(text[max(0, m.start() - 30):m.start()]):
                found.append((m[3].lower(), round(float(m[1]) * UNIT[m[2].lower().strip()], 3)))
    return found


SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in\b|\")", re.I)


def mesh_size(text):
    """How big a mesh should be, from loose wording: 'make it 118 mm tall', 'Total Height (...): ~4.65 inches',
    '4.1 in long', or a lone '120 mm'. Returns ("height" | "longest", mm) or None."""
    heights, lengths, bare = [], [], []
    for m in SIZE.finditer(text):
        mm = round(float(m[1]) * UNIT[m[2].lower().strip()], 2)
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[max(line_start, m.start() - 70):m.start()].lower()
        after = text[m.end():m.end() + 12].lower()
        if re.search(r"\b(height|tall|high)\b", before) or re.match(r"\s*(tall|high)\b", after):
            heights.append(mm)
        elif re.search(r"\b(length|long)\b", before) or re.match(r"\s*long\b", after):
            lengths.append(mm)
        elif not re.search(r"\b(width|wide|depth|deep|thick|diameter|weight|pin|hole|screw|bore)\b", before + " " + after):
            bare.append(mm)
    if heights:
        return ("height", max(heights))  # 'total height' beats a partial height like 'grip height'
    if lengths:
        return ("longest", max(lengths))
    if len(bare) == 1:
        return ("longest", bare[0])
    return None


def defects(meta, code, user_texts, previous=None):
    """Problems the app can measure itself, phrased for the model."""
    dims = meta["dimensions"]
    out = []
    try:
        intended = int(re.search(r"^PARTS\s*=\s*(\d+)", code or "", re.M)[1])
    except (TypeError, ValueError):
        intended = 1
    if meta.get("solids", 1) > max(1, intended):
        boxes = "; ".join(f"solid {i + 1}: X {b[0]:g}..{b[1]:g}, Y {b[2]:g}..{b[3]:g}, Z {b[4]:g}..{b[5]:g} (center {round((b[0]+b[1])/2, 1):g}, {round((b[2]+b[3])/2, 1):g}, {round((b[4]+b[5])/2, 1):g})"
                          for i, b in enumerate(meta.get("solid_boxes", [])))
        out.append(f"The result is {meta['solids']} separate solids that don't touch ({boxes}). Move the parts so they overlap, then union them into one solid. "
                   "If the main body came from Workplane(\"XZ\").extrude(...), it sits on the -Y side: use .extrude(WIDTH / 2, both=True) to center it on Y=0.")
    out += ["Size mismatch: " + h for h in size_hints(parameters(code), dims)]
    for label, value in stated_sizes(user_texts):
        tol = max(0.5, 0.02 * value)
        if label in ("length", "long"):
            if abs(max(dims) - value) > tol:
                out.append(f"You were asked for an overall {label} of {value:g} mm; the longest side is {max(dims):g} mm.")
        elif not any(abs(d - value) <= tol for d in dims):
            out.append(f"You were asked for {label} {value:g} mm; no side measures that (X={dims[0]:g}, Y={dims[1]:g}, Z={dims[2]:g}).")
    if previous and previous["dimensions"] == dims and abs(previous.get("volume", 0) - meta.get("volume", 0)) < 0.01:
        out.append("Your last fix produced exactly the same geometry as before: whatever you cut or added did not touch the part, so its coordinates are outside the part's extents above. Compare the coordinates you used with those extents.")
    return out


def review_message(code, meta, project_dir, folder, user_texts=(), previous=None):
    dims = meta["dimensions"]
    text = (f"SELF-CHECK. You just built this. Measured size: X={dims[0]:g} Y={dims[1]:g} Z={dims[2]:g} mm, "
            f"{meta.get('solids', 1)} solid(s).\n")
    for i, b in enumerate(meta.get("solid_boxes", [])[:3]):
        text += f"Solid {i + 1} occupies X {b[0]:g}..{b[1]:g}, Y {b[2]:g}..{b[3]:g}, Z {b[4]:g}..{b[5]:g} (global coordinates). Holes and cuts must be inside these ranges.\n"
    for problem in defects(meta, code, user_texts, previous):
        text += "PROBLEM: " + problem + "\n"
    text += ("Below are renders of what you built (isometric, top, front). Compare them with the reference photos and every request so far: "
             "overall size, each requested feature (holes must actually cut through the part, not sit outside it), and the silhouette. "
             "For the silhouette, compare the front render's outline with the photo's side view end to end: where it is thin, where it is deep, the shape of each end. "
             "If they differ, retrace the outline points from the photo rather than nudging numbers.\n"
             "If it is right, reply with just DONE. Otherwise reply with one short sentence on what you fixed, then the complete corrected program in one ```python block.")
    content = [{"type": "text", "text": text}]
    for f in ("view-isometric.png", "view-top.png", "view-front.png"):
        path = folder / f
        if path.is_file():
            content.extend([{"type": "text", "text": "Render: " + f}, image_content(path)])
    return {"role": "user", "content": content}


def severity(problems):
    """Separate solids make an unprintable part; other measured problems are size details."""
    return sum(3 if "separate solids" in x else 1 for x in problems)


def self_review(cfg, model, messages, raw, code, meta, folder, p, pid, jid):
    """After a successful build, show the model its renders plus measured problems, and keep only real improvements."""
    rounds = max(0, min(4, int(cfg.get("self_review_rounds", 3))))
    user_texts = [m["text"] for m in p["messages"] if m["role"] == "user"]
    best = severity(defects(meta, code, user_texts))
    kept, previous = meta, None
    for i in range(rounds):
        messages = messages + [{"role": "assistant", "content": raw}, review_message(code, meta, project_path(pid), folder, user_texts, previous)]
        update_job(jid, phase=f"Checking against your photos ({i + 1}/{rounds})", characters=0)
        try:
            raw = infer(cfg, model, messages, jid)
        except Cancelled:
            break
        (project_path(pid) / "turns" / f"{jid}-review{i}.txt").write_text(raw, encoding="utf-8")
        try:
            reply = parse_reply(raw)
        except ValueError:
            break
        if not reply["code"]:  # DONE, or a comment without a fix
            break
        new_folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
        update_job(jid, phase="Rebuilding with the fix")
        try:
            new_meta = build_cad(cfg, reply["code"], new_folder)
        except ValueError as exc:
            # Show the model its broken fix next round; keep the version that built.
            previous, code, raw = meta, reply["code"], raw + "\n\n(That fix failed to build: " + str(exc)[-800:] + ")"
            continue
        count = severity(defects(new_meta, reply["code"], user_texts, meta))
        same_as_kept = new_meta["dimensions"] == kept["dimensions"] and abs(new_meta["volume"] - kept["volume"]) < 0.01
        # Keep a fix only if it is measurably better, or clean (no measurable problems) and actually different.
        if (count < best or count == 0) and not same_as_kept:
            commit_version(p, new_folder, reply["code"], new_meta, "Self-check: " + reply["message"], model, p["active"])
            best, kept = count, new_meta
        previous, code, meta, folder = meta, reply["code"], new_meta, new_folder
    # Tell the person about anything the app can still measure as wrong in the version that's active.
    active = next((v for v in p["versions"] if v["id"] == p["active"]), None)
    if active:
        src = (project_path(pid) / active["id"] / "model.py").read_text(encoding="utf-8")
        left = defects(active, src, user_texts)
        if left:
            p["messages"].append({"role": "assistant", "text": "Heads-up, the current version still has issues: " + " ".join(left), "time": time.time()})
            save_project(p)


def commit_version(p, folder, code, meta, message, model, parent):
    version = {"id": folder.name, "number": len(p["versions"]) + 1, "created": time.time(), "message": message, "model": model, "parent": parent, "parameters": parameters(code), **meta}
    p["versions"].append(version)
    p["active"] = version["id"]
    p["messages"].append({"role": "assistant", "text": message, "version": version["id"], "time": time.time()})
    save_project(p)
    return version


def version_of(p, vid):
    return next((v for v in p["versions"] if v["id"] == vid), None)


def build_messages(p, pid, parent, content, cfg=None):
    messages = [{"role": "system", "content": SYSTEM + fit_rules(cfg or config())}]
    # Include discussion for continuity, but current source wins after restore/parameter edits.
    for turn in p["messages"][-14:-1]:
        messages.append({"role": turn["role"], "content": turn["text"]})
    context = content
    pv = version_of(p, parent) if parent else None
    if pv and pv.get("mesh"):
        box = (pv.get("solid_boxes") or [[-pv["dimensions"][0] / 2, pv["dimensions"][0] / 2, -pv["dimensions"][1] / 2, pv["dimensions"][1] / 2, 0, pv["dimensions"][2]]])
        x0, x1 = min(b[0] for b in box), max(b[1] for b in box)
        y0, y1 = min(b[2] for b in box), max(b[3] for b in box)
        z0, z1 = min(b[4] for b in box), max(b[5] for b in box)
        context += "\n" + MESH_EDIT.format(x0=x0, x1=x1, y0=y0, y1=y1, z0=z0, z1=z1)
    elif parent and (project_path(pid) / parent / "model.py").is_file():
        source = (project_path(pid) / parent / "model.py").read_text(encoding="utf-8")
        context += "\n\nCURRENT ACTIVE CAD SOURCE (preserve unrequested features):\n```python\n" + source + "\n```"
    user = [{"type": "text", "text": context}]
    for im in p["images"][-3:]:
        user.append({"type": "text", "text": "Reference photo: " + im["name"]})
        user.append(image_content(project_path(pid) / im["file"]))
    if parent:
        for f in ("view-isometric.png", "view-top.png", "view-front.png"):
            path = project_path(pid) / parent / f
            if path.is_file():
                user.extend([{"type": "text", "text": "Current CAD render: " + f}, image_content(path)])
    messages.append({"role": "user", "content": user})
    return messages


def design_turn(cfg, model, messages, p, pid, jid, parent, label=""):
    """One design request: generate, build, retry on errors, then self-check. Returns True if a version was made."""
    messages = list(messages)
    tries = max(1, min(6, int(cfg.get("max_attempts", 4))))
    prefix = f"[{label}] " if label else ""
    for attempt in range(tries):
        update_job(jid, phase=prefix + ("Designing" if attempt == 0 else f"Correcting CAD ({attempt}/{tries - 1})"), characters=0)
        raw = infer(cfg, model, messages, jid)
        log_dir = project_path(pid) / "turns"
        log_dir.mkdir(exist_ok=True)
        (log_dir / f"{jid}-{re.sub(r'[^A-Za-z0-9._-]+', '_', label) or 'm'}-{attempt}.txt").write_text(raw, encoding="utf-8")
        try:
            reply = parse_reply(raw)
            code = reply["code"]
            if not code:
                p["messages"].append({"role": "assistant", "text": prefix + reply["message"], "time": time.time()})
                save_project(p)
                return False
            update_job(jid, phase=prefix + "Building & checking geometry")
            folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
            pv = version_of(p, parent) if parent else None
            if pv and pv.get("mesh"):
                meta = build_mesh_edit(cfg, code, project_path(pid) / parent, folder)
                commit_version(p, folder, code, meta, prefix + reply["message"], model, parent)
                return True
            meta = build_cad(cfg, code, folder)
            commit_version(p, folder, code, meta, prefix + reply["message"], model, parent)
            self_review(cfg, model, messages, raw, code, meta, folder, p, pid, jid)
            return True
        except (ValueError, SyntaxError) as exc:
            if attempt == tries - 1:
                raise
            if cancelled(jid):
                raise Cancelled() from exc
            messages.extend([{"role": "assistant", "content": raw}, {"role": "user", "content": "That failed. Fix it and reply with a short message plus the complete corrected program in one ```python block. Error:\n" + str(exc)[-4000:]}])
    return False


def load_model(cfg, name, jid):
    """Unload everything, then load one of the runner's models with the runner's own settings. Returns its key."""
    require_idle(cfg)
    choice = next((m for m in loadable_models(cfg) if m["name"] == name), None)
    if not choice:
        raise ValueError("Unknown model. Choose one from the list.")
    lms = cfg.get("lms_path") or DEFAULT_LMS
    if not Path(lms).is_file():
        raise ValueError(f"LM Studio's lms command was not found at {lms}. Set it in Settings.")
    flags = [str(x) for x in runner_config(cfg).get("lms_load_flags", ["-y"])]
    args = [lms, "load", choice["key"]]
    if choice.get("context_length"):
        args += ["--context-length", str(int(choice["context_length"]))]
    flow = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    update_job(jid, phase="Unloading models")
    subprocess.run([lms, "unload", "--all"], capture_output=True, text=True, timeout=180, creationflags=flow)
    update_job(jid, phase=f"Loading {choice['name']} (can take a minute)")
    try:
        r = subprocess.run(args + flags, capture_output=True, text=True, timeout=900, creationflags=flow)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Loading the model took too long.") from exc
    if r.returncode != 0:
        raise ValueError("Loading failed: " + (r.stderr or r.stdout)[-2000:])
    return choice["key"]


def compare_models(cfg, messages, p, pid, jid, parent):
    """Run the same request on each configured model (one at a time) and keep each result as its own version."""
    vision = vision_by_key(cfg)
    has_photos = bool(p["images"]) or bool(parent)
    names = [m["name"] for m in loadable_models(cfg) if not has_photos or vision.get(m["key"]) is not False][:3]
    if len(names) < 2:
        raise ValueError("Compare needs two models that can see photos.")
    loaded_ids = {m["id"] for m in loaded_models(cfg)}
    # Start with whatever is already loaded, to save one load.
    names.sort(key=lambda n: 0 if next(m["key"] for m in loadable_models(cfg) if m["name"] == n) in loaded_ids else 1)
    summary = []
    for name in names:
        if cancelled(jid):
            raise Cancelled()
        key = next(m["key"] for m in loadable_models(cfg) if m["name"] == name)
        try:
            if key not in {m["id"] for m in loaded_models(cfg)}:
                key = load_model(cfg, name, jid)
            started = time.time()
            made = design_turn(cfg, key, messages, p, pid, jid, parent, label=name)
            v = next((v for v in p["versions"] if v["id"] == p["active"]), None) if made else None
            took = round(time.time() - started)
            summary.append(f"{name}: version {v['number']}, " + " × ".join(f"{x:g}" for x in v["dimensions"]) + f" mm, {took}s" if v else f"{name}: no version ({took}s)")
        except Cancelled:
            raise
        except Exception as exc:
            summary.append(f"{name}: failed ({str(exc)[:160]})")
    p["messages"].append({"role": "assistant", "text": "Compare: " + "; ".join(summary) + ". Restore whichever you prefer from Versions.", "time": time.time()})
    save_project(p)


def mesh_turn(cfg, request, p, pid, jid):
    """Photo -> TRELLIS.2 (ComfyUI) -> repaired, real-size mesh version. LM Studio is unloaded first to free the GPU."""
    require_idle(cfg)
    content = str(request.get("message", "")).strip()[:4000]
    imgs = add_images(p, request.get("images", []))
    p["messages"].append({"role": "user", "text": "[Mesh] " + content, "images": imgs, "time": time.time()})
    if p["name"] == "Untitled design":
        p["name"] = content[:45] or "Mesh"
    save_project(p)
    if not p["images"]:
        raise ValueError("Mesh mode needs a photo: attach one clear, angled photo of the part, then send again.")
    size = mesh_size(content)
    if not size:
        raise ValueError("Mesh mode needs the real size to scale the model. Add it to your message, e.g. 'make it 118 mm tall' or '4.65 in tall' (or '200 mm long').")
    height = size[1] if size[0] == "height" else None
    longest = size[1] if size[0] == "longest" else None
    workflow = Path(cfg.get("mesh_workflow") or ROOT / "workflows" / "trellis2_mesh.json")
    if not workflow.is_file():
        workflow = ROOT / "workflows" / "trellis2_mesh.json"
    if not workflow.is_file():
        raise ValueError("Mesh mode isn't set up yet: the TRELLIS.2 workflow file is missing.")
    image = project_path(pid) / p["images"][-1]["file"]
    base = cfg.get("comfy_url") or "http://127.0.0.1:8188"
    lms = cfg.get("lms_path") or DEFAULT_LMS
    if Path(lms).is_file():
        update_job(jid, phase="Freeing the GPU (unloading LM Studio)")
        subprocess.run([lms, "unload", "--all"], capture_output=True, text=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    update_job(jid, phase="Checking ComfyUI's background remover")
    attention = mesh_mode.ensure_deps(cfg.get("comfy_root") or mesh_mode.default_comfy_root(), DATA / "comfyui-setup.log",
                                      progress=lambda text: update_job(jid, phase=text))
    update_job(jid, phase="Starting ComfyUI")
    proc = mesh_mode.start(cfg.get("comfy_root") or mesh_mode.default_comfy_root(), base, DATA / "comfyui.log")
    try:
        update_job(jid, phase="TRELLIS.2 is building the mesh")
        try:
            src = mesh_mode.generate(base, workflow, image, cfg.get("comfy_root") or mesh_mode.default_comfy_root(),
                                     progress=lambda sec: update_job(jid, phase=f"TRELLIS.2 is building the mesh ({sec}s) · {mesh_mode.last_log_line(DATA / 'comfyui.log')}"),
                                     cancelled=lambda: cancelled(jid), attention=attention,
                                     running=lambda: proc is None or proc.poll() is None)
        except InterruptedError as exc:
            raise Cancelled() from exc
    finally:
        update_job(jid, phase="Freeing ComfyUI")
        mesh_mode.stop(base, proc)
    folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    raw = folder / ("raw" + src.suffix.lower())
    raw.write_bytes(src.read_bytes())
    update_job(jid, phase="Repairing and scaling the mesh")
    size_args = ["--height", str(height)] if height else ["--longest", str(longest)]
    r = subprocess.run([cfg["cad_python"], "-I", "-B", str(ROOT / "mesh_worker.py"), str(raw), str(folder), *size_args],
                       capture_output=True, text=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (folder / "execution.log").write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        raise ValueError("Couldn't clean up the mesh: " + (r.stderr or r.stdout)[-1500:])
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    dims = " × ".join(f"{x:g}" for x in meta["dimensions"])
    text = (f"Mesh from TRELLIS.2: {dims} mm, {meta['faces']:,} triangles, "
            f"{('watertight (rebuilt as a closed solid for printing, so fine detail is slightly softened)' if meta.get('remeshed') else 'watertight') if meta['valid'] else 'NOT watertight (it may need repair in Bambu Studio)'}. "
            "Use Make printable to lay it flat and split it to your bed.")
    commit_version(p, folder, "", meta, text, "TRELLIS.2", p["active"])


def make_printable(cfg, folder):
    out = folder / "print"
    bed = [str(float(x)) for x in cfg["bed"]]
    cmd = [cfg["cad_python"], "-I", "-B", str(ROOT / "printable_worker.py"), str(folder / "model.stl"), str(out), *bed]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Making it printable took too long.") from exc
    if r.returncode != 0:
        detail = (r.stderr or r.stdout)[-1500:]
        if "No module named" in detail:
            detail += " (Run Install-Form-Studio.cmd once to add the printing helpers.)"
        raise ValueError("Couldn't make it printable: " + detail)
    return json.loads((out / "report.json").read_text(encoding="utf-8"))


def bambu_path(cfg):
    for c in (cfg.get("bambu_path"), r"C:\Program Files\Bambu Studio\bambu-studio.exe", r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe"):
        if c and Path(c).is_file():
            return c
    return None


def open_in_bambu(cfg, pid, version_id):
    folder = project_path(pid) / version_id
    pieces = sorted((folder / "print").glob("piece_*.stl"))
    files = pieces or [folder / "model.stl"]
    exe = bambu_path(cfg)
    if not exe:
        raise ValueError("Bambu Studio wasn't found. Set its path in Settings.")
    subprocess.Popen([exe, *map(str, files)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return len(files)


def templates():
    found = []
    for f in sorted((ROOT / "templates").glob("*.py")):
        head = dict(re.findall(r"^# (name|about): (.+)$", f.read_text(encoding="utf-8"), re.M))
        found.append({"id": f.stem, "name": head.get("name", f.stem), "about": head.get("about", "")})
    return found


def template_turn(cfg, request, p, pid, jid, parent):
    tid = str(request.get("template", ""))
    path = ROOT / "templates" / f"{tid}.py"
    if not re.fullmatch(r"[a-z0-9_]+", tid) or not path.is_file():
        raise ValueError("Unknown template")
    code = path.read_text(encoding="utf-8")
    info = next(t for t in templates() if t["id"] == tid)
    if p["name"] == "Untitled design":
        p["name"] = info["name"]
    update_job(jid, phase=f"Building the {info['name']} template")
    folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
    meta = build_cad(cfg, code, folder)
    commit_version(p, folder, code, meta, f"Started from the {info['name']} template. {info['about']} Change sizes under Dimensions, or describe changes in chat.", "Template", parent)


def print_pieces(cfg, p, pid, version, jid):
    folder = project_path(pid) / version["id"]
    pieces = sorted((folder / "print").glob("piece_*.stl"))
    if not pieces:
        update_job(jid, phase="Laying it flat and fitting it to your bed")
        version["print"] = make_printable(cfg, folder)
        save_project(p)
        pieces = sorted((folder / "print").glob("piece_*.stl"))
    return pieces


def slice_turn(cfg, request, p, pid, jid, parent):
    """Slice the print pieces with Bambu Studio's command line, then open the sliced plate in Bambu Studio."""
    version = version_of(p, parent)
    if not version:
        raise ValueError("Generate a design first")
    exe = bambu_path(cfg)
    if not exe:
        raise ValueError("Bambu Studio wasn't found. Set its path in Settings.")
    material = str(request.get("material") or cfg.get("material") or "PLA")
    if material not in slicer.MATERIALS:
        raise ValueError("Unknown material")
    walls = max(1, min(10, int(request.get("walls") or cfg.get("walls", 2))))
    infill = max(0, min(100, int(request.get("infill", cfg.get("infill", 15)))))
    pieces = print_pieces(cfg, p, pid, version, jid)
    update_job(jid, phase=f"Slicing for the X2D ({material}, {walls} walls, {infill}% infill)")
    flow = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        target, est = slicer.slice_pieces(exe, pieces, project_path(pid) / parent / "print", material, walls, infill)
    except (ValueError, OSError, subprocess.TimeoutExpired, KeyError) as exc:
        if not request.get("open", True):
            raise
        subprocess.Popen([exe, *map(str, pieces)], creationflags=flow)
        p["messages"].append({"role": "assistant", "time": time.time(), "text":
                              f"Form Studio couldn't slice it ({str(exc)[:300]}), so I opened the print pieces in Bambu Studio instead: press Slice plate there."})
        save_project(p)
        return
    version["slice"] = {**est, "material": material, "walls": walls, "infill": infill, "time": time.time()}
    hours, minutes = divmod(round(est["seconds"] / 60), 60)
    p["messages"].append({"role": "assistant", "time": time.time(), "text":
                          f"Sliced for the X2D: {f'{hours} h ' if hours else ''}{minutes} min, {est['grams']:g} g of {material} ({walls} walls, {infill}% infill). "
                          + ("It's open in Bambu Studio: check the plate, then press Print." if request.get("open", True) else "")})
    save_project(p)
    if request.get("open", True):
        subprocess.Popen([exe, str(target)], creationflags=flow)


TEMPS = [0.2, 0.55, 0.85, 0.35, 0.7, 0.95, 0.45, 0.6]


def variations_turn(cfg, request, p, pid, jid, parent):
    """Several independent attempts at one request, split across the models; the best is made active."""
    require_idle(cfg)
    count = max(2, min(8, int(request.get("count", 4))))
    content = str(request.get("message", "")).strip()[:12000]
    if not content:
        raise ValueError("Describe what you want to make or change")
    if not request.get("stored"):
        imgs = add_images(p, request.get("images", []))
        p["messages"].append({"role": "user", "text": f"[Variations ×{count}] " + content, "images": imgs, "time": time.time()})
        if p["name"] == "Untitled design":
            p["name"] = content[:45]
        save_project(p)
    messages = build_messages(p, pid, parent, content, cfg)
    vision = vision_by_key(cfg)
    needs_eyes = bool(p["images"]) or bool(parent)
    models = [m for m in loadable_models(cfg) if not needs_eyes or vision.get(m["key"]) is not False]
    if not models:
        raise ValueError("No model that can see photos is set up")
    loaded = {m["id"] for m in loaded_models(cfg)}
    models.sort(key=lambda m: 0 if m["key"] in loaded else 1)
    share = [count // len(models) + (1 if i < count % len(models) else 0) for i in range(len(models))]
    made, n = [], 0
    user_texts = [m["text"] for m in p["messages"] if m["role"] == "user"]
    for model, k in zip(models, share):
        if not k:
            continue
        if runner_status(cfg).get("busy"):
            p["messages"].append({"role": "assistant", "text": "Stopped the variations early: the overnight builder started.", "time": time.time()})
            break
        key = model["key"] if model["key"] in {m["id"] for m in loaded_models(cfg)} else load_model(cfg, model["name"], jid)
        for _ in range(k):
            if cancelled(jid):
                raise Cancelled()
            n += 1
            label = f"Variation {n} of {count} · {model['name']}"
            try:
                if design_turn(dict(cfg, temperature=TEMPS[(n - 1) % len(TEMPS)]), key, messages, p, pid, jid, parent, label=label):
                    made.append((version_of(p, p["active"]), label))
            except Cancelled:
                raise
            except Exception as exc:
                p["messages"].append({"role": "assistant", "text": f"[{label}] failed: {str(exc)[:200]}", "time": time.time(), "failed": True})
                save_project(p)
    if not made:
        raise ValueError("None of the variations produced a part")
    scored = []
    for v, label in made:
        src_file = project_path(pid) / v["id"] / ("tools.py" if v.get("tools") else "model.py")
        src = src_file.read_text(encoding="utf-8") if src_file.is_file() else ""
        scored.append((severity(defects(v, src, user_texts)), -v["number"], v, label))
    scored.sort(key=lambda x: (x[0], x[1]))
    best = scored[0][2]
    p["active"] = best["id"]
    lines = [f"Version {v['number']} ({label.split(' · ')[-1]}): " + (f"{score} measured problem(s)" if score else "no measured problems")
             + (" ← picked" if v is best else "") for score, _, v, label in sorted(scored, key=lambda x: x[2]["number"])]
    p["messages"].append({"role": "assistant", "time": time.time(), "text":
                          f"Variations done: {len(made)} of {count} built. " + "; ".join(lines) + ". Compare them in Versions and restore any you like better."})
    save_project(p)


SCHEDULE: dict[str, dict] = {}


def save_schedule():
    atomic(DATA / "schedule.json", SCHEDULE)


def schedule_variations(pid, obj):
    """Store the request and its photos now; the scheduler starts it at the chosen time."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(obj.get("start_at", "")))
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("Pick a start time like 01:00")
    now = time.localtime()
    at = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(m[1]), int(m[2]), 0, 0, 0, -1))
    if at <= time.time():
        at += 86400
    content = str(obj.get("message", "")).strip()[:12000]
    if not content:
        raise ValueError("Describe what you want to make or change")
    count = max(2, min(8, int(obj.get("count", 4))))
    p = read_project(pid)
    imgs = add_images(p, obj.get("images", []))
    when = time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")
    p["messages"].append({"role": "user", "text": f"[Variations ×{count}, scheduled for {when}] " + content, "images": imgs, "time": time.time()})
    p["messages"].append({"role": "assistant", "time": time.time(), "text":
                          f"Scheduled {count} variations for {when}. Leave Form Studio open (the PC can't be asleep). They start only when the overnight builder is idle."})
    if p["name"] == "Untitled design":
        p["name"] = content[:45]
    save_project(p)
    with LOCK:
        SCHEDULE[pid] = {"at": at, "count": count, "request": {"kind": "variations", "count": count, "message": content, "stored": True}}
        save_schedule()
    return at


def scheduler_loop():
    while True:
        time.sleep(20)
        try:
            for pid, item in list(SCHEDULE.items()):
                if item["at"] > time.time():
                    continue
                if time.time() - item["at"] > 6 * 3600:
                    with LOCK:
                        SCHEDULE.pop(pid, None)
                        save_schedule()
                    p = read_project(pid)
                    p["messages"].append({"role": "assistant", "time": time.time(), "failed": True,
                                          "text": "The scheduled variations didn't run: Form Studio or the overnight builder was busy for 6 hours."})
                    save_project(p)
                    continue
                cfg = config()
                if runner_status(cfg).get("busy") or not JOB_LOCK.acquire(blocking=False):
                    continue
                with LOCK:
                    SCHEDULE.pop(pid, None)
                    save_schedule()
                    jid = uuid.uuid4().hex
                    JOBS[jid] = {"status": "running", "phase": "Starting scheduled variations", "characters": 0, "started": time.time(), "project_id": pid, "kind": "variations"}
                threading.Thread(target=job_run, args=(jid, pid, item["request"], cfg), daemon=True).start()
        except Exception:
            pass


def job_run(jid, pid, request, cfg):
    try:
        p = read_project(pid)
        parent = p["active"]
        if request.get("kind") == "sample":
            if p["name"] == "Untitled design":
                p["name"] = "Sample mounting bracket"
            code = SAMPLE
            message = "Here is an 80 × 40 × 5 mm sample bracket with two 6 mm holes, 60 mm apart. This sample verifies the CAD engine without using AI."
            update_job(jid, phase="Building sample")
            folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
            meta = build_cad(cfg, code, folder)
            commit_version(p, folder, code, meta, message, "Sample", parent)
        elif request.get("kind") == "parameters":
            if not parent:
                raise ValueError("Generate a design first")
            pv = version_of(p, parent)
            code_path = project_path(pid) / parent / ("tools.py" if pv.get("tools") else "model.py")
            if not code_path.is_file():
                raise ValueError("This version is a mesh, so it has no editable dimensions")
            code = code_path.read_text(encoding="utf-8")
            allowed = parameters(code)
            changes = request.get("parameters", {})
            lines = code.splitlines(keepends=True)
            replacements = []
            for n in ast.parse(code).body:
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                    name = n.targets[0].id
                    if name in allowed and name in changes:
                        value = float(changes[name])
                        if not (-100000 < value < 100000):
                            raise ValueError("Parameter is outside the allowed range")
                        replacements.append((n.lineno - 1, n.end_lineno, f"{name} = {value!r}\n"))
            for start, end, value in sorted(replacements, reverse=True):
                lines[start:end] = [value]
            code = "".join(lines)
            update_job(jid, phase="Updating dimensions")
            folder = project_path(pid) / ("v-" + uuid.uuid4().hex[:12])
            meta = build_mesh_edit(cfg, code, project_path(pid) / pv["base"], folder) if pv.get("tools") else build_cad(cfg, code, folder)
            msg = "Updated parameters: " + ", ".join(f"{k} = {v}" for k, v in changes.items() if k in allowed)
            commit_version(p, folder, code, meta, msg, "Parameters", parent)
        elif request.get("kind") == "mesh":
            mesh_turn(cfg, request, p, pid, jid)
        elif request.get("kind") == "template":
            template_turn(cfg, request, p, pid, jid, parent)
        elif request.get("kind") == "slice":
            slice_turn(cfg, request, p, pid, jid, parent)
        elif request.get("kind") == "variations":
            variations_turn(cfg, request, p, pid, jid, parent)
        elif request.get("kind") == "printable":
            if not parent:
                raise ValueError("Generate a design first")
            update_job(jid, phase="Laying it flat and fitting it to your bed")
            report = make_printable(cfg, project_path(pid) / parent)
            version = next(v for v in p["versions"] if v["id"] == parent)
            version["print"] = report
            n = len(report["pieces"])
            sizes = ", ".join(" × ".join(f"{x:g}" for x in size) for size in report["sizes"])
            text = (f"Ready to print: {n} piece{'s' if n != 1 else ''} ({sizes} mm), laid on {'their' if n != 1 else 'its'} largest flat side and centered. ")
            if report.get("split") and report["split"]["cuts"]:
                pins = len(report["split"]["pins"])
                text += (f"It was split {len(report['split']['cuts'])} time(s) to fit your bed; {pins} joint(s) have holes for "
                         f"{report['split']['pin_diameter']:g} mm × {report['split']['pin_length']:g} mm dowel pins. ")
            if not report.get("watertight", True):
                text += ("The mesh has gaps, so it couldn't be split; it's still bigger than your bed. " if not report.get("fits", True)
                         else "The mesh has small gaps; Bambu Studio usually repairs them on import. ")
            text += "Use Print files to download, or Open in Bambu Studio."
            p["messages"].append({"role": "assistant", "text": text, "time": time.time()})
            save_project(p)
        else:
            require_idle(cfg)
            content = str(request.get("message", "")).strip()[:12000]
            if not content:
                raise ValueError("Describe what you want to make or change")
            imgs = add_images(p, request.get("images", []))
            p["messages"].append({"role": "user", "text": content, "images": imgs, "time": time.time()})
            if p["name"] == "Untitled design":
                p["name"] = content[:45]
            save_project(p)
            messages = build_messages(p, pid, parent, content, cfg)
            if request.get("kind") == "compare":
                compare_models(cfg, messages, p, pid, jid, parent)
            else:
                design_turn(cfg, str(request.get("model", "")), messages, p, pid, jid, parent)
        update_job(jid, status="done", phase="Ready", project=public_project(p))
    except Cancelled:
        note_failure(pid, request, "Stopped before finishing. The previous version is unchanged.")
        update_job(jid, status="cancelled", phase="Stopped", error="Stopped. The previous version is unchanged.")
    except Exception as exc:
        note_failure(pid, request, "That attempt failed, so the previous version is unchanged. Error: " + str(exc)[-600:])
        update_job(jid, status="error", phase="Stopped", error=str(exc)[-6000:])
    finally:
        JOB_LOCK.release()


def note_failure(pid, request, text):
    """Keep the chat coherent: a failed or stopped chat turn still gets an assistant reply."""
    if request.get("kind") in ("sample", "parameters"):
        return
    try:
        p = read_project(pid)
        if p["messages"] and p["messages"][-1]["role"] == "user":
            p["messages"].append({"role": "assistant", "text": text, "time": time.time(), "failed": True})
            save_project(p)
    except (OSError, ValueError, KeyError):
        pass


def load_job(jid, request, cfg):
    try:
        load_model(cfg, request.get("name"), jid)
        update_job(jid, status="done", phase="Ready", loaded=request.get("name"))
    except Exception as exc:
        update_job(jid, status="error", phase="Stopped", error=str(exc)[-3000:])
    finally:
        JOB_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200, content_type="application/json"):
        raw = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def authed(self):
        """A phone or other computer that logged in with the passcode (only when phone access is on)."""
        import hashlib
        from http.cookies import SimpleCookie
        if not REMOTE:
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("fs_auth")
        except Exception:
            return False
        return bool(cookie) and hashlib.sha256(cookie.value.encode()).hexdigest() in session_store()

    def valid_host(self):
        if PREVIEW:
            return True
        if self.client_is_local():
            # Loopback clients must also use a loopback Host name (blocks DNS-rebinding pages).
            return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")
        return self.authed()

    def login(self):
        ip = self.client_address[0]
        now = time.time()
        fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 600]
        if len(fails) >= 5:
            return self.reply({"error": "Too many tries. Wait 10 minutes."}, 429)
        length = int(self.headers.get("Content-Length", 0))
        obj = json.loads(self.rfile.read(min(length, 4096)) or b"{}") if length else {}
        cfg = config()
        salt, good = cfg.get("remote_passcode_salt"), cfg.get("remote_passcode_hash")
        if not salt or not good or not secrets.compare_digest(hash_passcode(str(obj.get("passcode", "")), salt)[1], good):
            LOGIN_FAILS[ip] = fails + [now]
            return self.reply({"error": "Wrong passcode"}, 401)
        LOGIN_FAILS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        remember_session(token)
        raw = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Set-Cookie", f"fs_auth={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if REMOTE and not self.client_is_local() and path in ("/login", "/login.js", "/style.css", "/favicon.png"):
                name = "login.html" if path == "/login" else path[1:]
                return self.reply((ROOT / "static" / name).read_bytes(), content_type=CONTENT_TYPES[Path(name).suffix])
            if not self.valid_host():
                if REMOTE and not self.client_is_local():
                    return self.reply({"error": "Log in first"}, 401) if path.startswith(("/api/", "/files/")) else self.redirect("/login")
                return self.reply({"error": "Local access only"}, 403)
            if path == "/api/session":
                return self.reply({"token": TOKEN, "preview": PREVIEW, "local": self.client_is_local()})
            if path == "/api/config":
                return self.reply(public_config())
            if path == "/api/status":
                cfg = config()
                status = {"models": [], "lm_error": None, "runner": runner_status(cfg), "cad_python_exists": Path(cfg["cad_python"]).is_file(), "busy": JOB_LOCK.locked(),
                          "loadable": [], "lms_found": Path(cfg.get("lms_path") or DEFAULT_LMS).is_file(), "bambu_found": bool(bambu_path(cfg))}
                vision = {} if PREVIEW else vision_by_key(cfg)
                status["loadable"] = [{"name": m["name"], "vision": vision.get(m["key"])} for m in loadable_models(cfg)]
                if PREVIEW:
                    status["lm_error"] = "Preview mode · run on your PC for local AI"
                else:
                    try:
                        status["models"] = loaded_models(cfg)
                    except Exception as exc:
                        status["lm_error"] = str(exc)
                status["photo_tools"] = dict(photo_tools.STATE)
                status["schedule"] = [{"project_id": k, "at": v["at"], "count": v["count"]} for k, v in SCHEDULE.items()]
                status["materials"] = {k: [v[1], v[2]] for k, v in slicer.MATERIALS.items()}
                with LOCK:
                    status["active_job"] = next(({"id": k, "project_id": v["project_id"], "kind": v.get("kind")} for k, v in JOBS.items() if v["status"] == "running"), None)
                return self.reply(status)
            if path == "/api/projects":
                projects = []
                for f in (DATA / "projects").glob("*/project.json"):
                    try:
                        p = json.loads(f.read_text(encoding="utf-8"))
                        projects.append({k: p[k] for k in ("id", "name", "updated", "active")})
                    except (OSError, ValueError):
                        pass
                return self.reply(sorted(projects, key=lambda x: x["updated"], reverse=True))
            if path == "/api/templates":
                return self.reply(templates())
            if path == "/api/reference-card.pdf":
                raw = photo_tools.card(config()["cad_python"], DATA).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="form-studio-reference-card.pdf"')
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(raw)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})", path)
            if match:
                return self.reply(public_project(read_project(match[1])))
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path)
            if match:
                with LOCK:
                    job = copy.deepcopy({k: v for k, v in JOBS.get(match[1], {}).items() if k != "cancel"}) or None
                return self.reply(job or {"error": "Job not found"}, 200 if job else 404)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/archive", path)
            if match:
                memory = io.BytesIO()
                with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
                    base = project_path(match[1])
                    for f in base.rglob("*"):
                        if f.is_file():
                            archive.write(f, f.relative_to(base))
                return self.reply(memory.getvalue(), content_type="application/zip")
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/versions/(v-[a-f0-9]{12})/print\.zip", path)
            if match:
                folder = project_path(match[1]) / match[2] / "print"
                memory = io.BytesIO()
                with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
                    for f in sorted(folder.glob("piece_*.stl")):
                        archive.write(f, f.name)
                return self.reply(memory.getvalue(), content_type="application/zip")
            match = re.fullmatch(r"/files/([a-f0-9]{32})/(.+)", path)
            if match:
                base = project_path(match[1]).resolve()
                file = (base / urllib.parse.unquote(match[2])).resolve()
                if not file.is_relative_to(base) or not file.is_file() or file.suffix.lower() not in (".stl", ".step", ".py", ".png", ".jpg", ".webp", ".json", ".log"):
                    raise FileNotFoundError()
                return self.reply(file.read_bytes(), content_type=CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream"))
            if path in ("/", "/app.js", "/viewer.js", "/style.css", "/favicon.png", "/login.js"):
                name = "index.html" if path == "/" else path[1:]
                return self.reply((ROOT / "static" / name).read_bytes(), content_type=CONTENT_TYPES[Path(name).suffix])
            self.reply({"error": "Not found"}, 404)
        except (FileNotFoundError, KeyError):
            self.reply({"error": "Not found"}, 404)
        except Exception as exc:
            self.reply({"error": str(exc)}, 400)

    def do_POST(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/login" and REMOTE and not self.client_is_local():
                return self.login()
            if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-Form-Token", ""), TOKEN):
                return self.reply({"error": "Refresh the app before continuing"}, 403)
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
            if not self.client_is_local():
                allowed.add("http://" + self.headers.get("Host", ""))
            if origin and not PREVIEW and origin not in allowed:
                return self.reply({"error": "Local requests only"}, 403)
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 35 * 1024 * 1024:
                raise ValueError("Request too large")
            obj = json.loads(self.rfile.read(length))
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/config":
                if not self.client_is_local() and not PREVIEW:
                    return self.reply({"error": "Settings can only be changed on the PC"}, 403)
                if JOB_LOCK.locked():
                    raise ValueError("Wait for the current revision before changing settings")
                cfg = config()
                if "remote_access" in obj:
                    cfg["remote_access"] = bool(obj["remote_access"])
                code = str(obj.get("remote_passcode") or "")
                if code:
                    if len(code) < 6:
                        raise ValueError("Use a passcode of at least 6 characters")
                    cfg["remote_passcode_salt"], cfg["remote_passcode_hash"] = hash_passcode(code)
                    (DATA / "remote_sessions.json").unlink(missing_ok=True)  # new passcode logs everyone out
                if cfg.get("remote_access") and not cfg.get("remote_passcode_hash"):
                    raise ValueError("Set a passcode before turning on phone access")
                cfg["lm_url"] = local_url(str(obj.get("lm_url", cfg["lm_url"])))
                for key in ("cad_python", "runner_root", "lms_path", "bambu_path", "comfy_root"):
                    cfg[key] = str(obj.get(key, cfg[key]))
                if "fit_clearance" in obj:
                    if not 0 <= float(obj["fit_clearance"]) <= 2:
                        raise ValueError("Use a fit clearance between 0 and 2 mm")
                    cfg["fit_clearance"] = float(obj["fit_clearance"])
                if obj.get("material") in slicer.MATERIALS:
                    cfg["material"] = obj["material"]
                bed = obj.get("bed", cfg["bed"])
                if len(bed) != 3 or not all(1 <= float(x) <= 5000 for x in bed):
                    raise ValueError("Enter three valid bed dimensions")
                cfg["bed"] = list(map(float, bed))
                atomic(DATA / "settings.json", cfg)
                return self.reply(public_config())
            if path == "/api/projects":
                return self.reply(new_project(obj.get("name", "Untitled design")), 201)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/restore", path)
            if match:
                if JOB_LOCK.locked():
                    raise ValueError("Wait for the current revision")
                p = read_project(match[1])
                if obj.get("version") not in {v["id"] for v in p["versions"]}:
                    raise ValueError("Version not found")
                p["active"] = obj["version"]
                save_project(p)
                return self.reply(p)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/jobs", path)
            if match:
                if PREVIEW and obj.get("kind") not in ("sample", "parameters", "printable", "template"):  # AI and slicing need the PC
                    raise ValueError("Run Form Studio on your Windows PC to connect your local AI")
                read_project(match[1])
                if obj.get("kind") == "variations" and obj.get("start_at"):
                    if match[1] in SCHEDULE:
                        raise ValueError("This project already has a scheduled run. Cancel it first.")
                    return self.reply({"scheduled": schedule_variations(match[1], obj)}, 202)
                if not JOB_LOCK.acquire(blocking=False):
                    raise ValueError("A revision is already running")
                jid = uuid.uuid4().hex
                with LOCK:
                    JOBS[jid] = {"status": "running", "phase": "Starting", "characters": 0, "started": time.time(), "project_id": match[1], "kind": obj.get("kind") or "chat"}
                threading.Thread(target=job_run, args=(jid, match[1], obj, config()), daemon=True).start()
                return self.reply({"job": jid}, 202)
            if path == "/api/load":
                if PREVIEW:
                    raise ValueError("Run Form Studio on your Windows PC to load a model")
                if not JOB_LOCK.acquire(blocking=False):
                    raise ValueError("Wait for the current job to finish")
                jid = uuid.uuid4().hex
                with LOCK:
                    JOBS[jid] = {"status": "running", "phase": "Starting", "characters": 0, "started": time.time(), "project_id": None, "kind": "load"}
                threading.Thread(target=load_job, args=(jid, obj, config()), daemon=True).start()
                return self.reply({"job": jid}, 202)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/delete", path)
            if match:
                source = project_path(match[1])
                if not (source / "project.json").is_file():
                    raise ValueError("That project doesn't exist")
                with LOCK:
                    busy = any(j.get("status") == "running" and j.get("project_id") == match[1] for j in JOBS.values())
                if busy:
                    raise ValueError("Stop the running job before deleting this project")
                trash = DATA / "trash"
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(trash / f"{match[1]}-{int(time.time())}"))
                return self.reply({"deleted": match[1]})
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/bambu", path)
            if match:
                if PREVIEW:
                    raise ValueError("Open Bambu Studio from the app on your PC")
                p = read_project(match[1])
                version = str(obj.get("version") or p["active"] or "")
                if version not in {v["id"] for v in p["versions"]}:
                    raise ValueError("Generate a design first")
                return self.reply({"opened": open_in_bambu(config(), match[1], version)})
            if path == "/api/measure":
                if PREVIEW:
                    raise ValueError("Measure photos in the app on your PC")
                if obj.get("project"):
                    image = (project_path(str(obj["project"])) / Path(str(obj.get("file", ""))).name)
                    if image.suffix.lower() not in IMAGE_TYPES or not image.is_file():
                        raise ValueError("Photo not found")
                    raw = image.read_bytes()
                else:
                    m = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", str(obj.get("data", "")))
                    if not m:
                        raise ValueError("Use a PNG, JPEG or WebP photo")
                    raw = base64.b64decode(m[2], validate=True)
                return self.reply(photo_tools.measure(config()["cad_python"], DATA, raw))
            if path == "/api/schedule/cancel":
                with LOCK:
                    item = SCHEDULE.pop(str(obj.get("project", "")), None)
                    save_schedule()
                if item:
                    p = read_project(str(obj["project"]))
                    p["messages"].append({"role": "assistant", "text": "Cancelled the scheduled variations.", "time": time.time()})
                    save_project(p)
                return self.reply({"cancelled": bool(item)})
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", path)
            if match:
                cancel_job(match[1])
                return self.reply({"ok": True})
            if path == "/api/quit":
                if JOB_LOCK.locked():
                    raise ValueError("Stop the current job before quitting")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self.reply({"ok": True})
            return self.reply({"error": "Not found"}, 404)
        except Exception as exc:
            self.reply({"error": str(exc)}, 400)


def app_browser():
    """Chrome or Edge, for a standalone app window (no tabs or address bar)."""
    env = os.environ
    candidates = [Path(env.get(v, "")) / rel for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                  for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe")]
    return next((str(c) for c in candidates if c.is_absolute() and c.is_file()), None)


def open_window(address):
    exe = app_browser() if os.name == "nt" else None
    if exe:
        try:
            subprocess.Popen([exe, f"--app={address}", "--new-window"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except OSError:
            pass
    webbrowser.open(address)


def already_running(port):
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/api/session", timeout=2) as r:
            return "token" in json.load(r)
    except (OSError, ValueError):
        return False


def fatal(message):
    """pythonw has no console, so show problems in a dialog and a log file."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "form-studio.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass
    print(message, file=sys.stderr)
    if os.name == "nt" and (sys.stderr is None or "pythonw" in Path(sys.executable).name.lower()):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Form Studio", 0x10)
        except Exception:
            pass
    raise SystemExit(1)


def main():
    global PREVIEW, PORT, REMOTE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--strictPort", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.preview and args.host != "127.0.0.1":
        parser.error("The local application only binds to 127.0.0.1")
    PREVIEW, PORT = args.preview, args.port
    address = f"http://127.0.0.1:{PORT}"
    if not PREVIEW and already_running(PORT):
        # Second click on the shortcut: just bring up a window on the running app.
        if not args.no_browser:
            open_window(address)
        return
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        cfg = config()
        REMOTE = bool(cfg.get("remote_access") and cfg.get("remote_passcode_hash")) and not PREVIEW
        server = ThreadingHTTPServer(("0.0.0.0" if (PREVIEW or REMOTE) else "127.0.0.1", PORT), Handler)
    except OSError as exc:
        fatal(f"Form Studio could not start on port {PORT}: {exc}. Another program may be using it.")
    if not PREVIEW:
        photo_tools.start(cfg["cad_python"], DATA / "opencv-install.log")
        try:
            SCHEDULE.update(json.loads((DATA / "schedule.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        threading.Thread(target=scheduler_loop, daemon=True).start()
    print(f"Form Studio: {address}", flush=True)
    if not args.no_browser and not PREVIEW:
        threading.Timer(0.5, lambda: open_window(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
