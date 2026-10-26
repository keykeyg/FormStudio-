"""Mesh mode: run TRELLIS.2 in the local ComfyUI on a reference photo. Standard library only.

The workflow is either ComfyUI's API format or an editor workflow (converted with the running ComfyUI's
node list); either way the image loader's input is the text "__INPUT_IMAGE__".
Form Studio starts ComfyUI when needed (after LM Studio has been unloaded) and stops it afterwards,
so both never hold the GPU at the same time.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MESH_EXT = (".glb", ".gltf", ".obj", ".ply", ".stl")
PLACEHOLDER = "__INPUT_IMAGE__"


def default_comfy_root():
    return str(Path.home() / "ComfyUI_windows_portable")


def _open(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


def get_json(url, timeout=30):
    with _open(url, timeout=timeout) as r:
        return json.load(r)


def post_json(url, payload, timeout=60):
    with _open(url, json.dumps(payload).encode(), {"Content-Type": "application/json"}, timeout) as r:
        return json.load(r)


def upload_image(base, path: Path):
    boundary = "----formstudio" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    with _open(base + "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 120) as r:
        resp = json.load(r)
    return f"{resp['subfolder']}/{resp['name']}" if resp.get("subfolder") else resp["name"]


def alive(base):
    try:
        get_json(base + "/system_stats", timeout=3)
        return True
    except (OSError, ValueError):
        return False


# TRELLIS.2 needs: onnxruntime for its background removal (without it rembg calls sys.exit inside the ComfyUI
# worker, which silently kills the job), and flash-attn or xformers for sparse attention. Check before starting.
ONNXRUNTIME = "onnxruntime==1.22.1"
XFORMERS = {"2.10": "0.0.34", "2.9": "0.0.33.post2", "2.8": "0.0.32.post2", "2.7": "0.0.30"}  # torch -> matching xformers


def ensure_deps(comfy_root, log_path, progress=lambda text: None):
    """Install what TRELLIS.2 needs into ComfyUI's Python if missing. Returns {"flash_attn": bool}."""
    py = Path(comfy_root) / "python_embeded" / "python.exe"
    if not py.is_file():
        return {"flash_attn": True}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def has(code):
        return subprocess.run([str(py), "-s", "-c", code], capture_output=True, timeout=300, creationflags=flags).returncode == 0

    def pip(*args):
        r = subprocess.run([str(py), "-s", "-m", "pip", "install", "--disable-pip-version-check", *args],
                           capture_output=True, text=True, timeout=1800, creationflags=flags)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n[Form Studio] pip install {' '.join(args)}\n" + r.stdout[-4000:] + r.stderr[-4000:])
        return r

    if not has("import onnxruntime, rembg"):
        progress("Adding the background remover runtime to ComfyUI")
        r = pip(ONNXRUNTIME, "rembg")
        if not has("import onnxruntime, rembg"):
            raise ValueError("ComfyUI's Python is missing onnxruntime for background removal, and installing it failed: " + (r.stderr or r.stdout)[-600:])
    flash = has("import flash_attn")
    if not flash and not has("import xformers.ops"):
        version = subprocess.run([str(py), "-s", "-c", "import torch; print(torch.__version__)"], capture_output=True, text=True,
                                 timeout=300, creationflags=flags).stdout.strip()
        base, _, cuda = version.partition("+")
        match = XFORMERS.get(".".join(base.split(".")[:2]))
        if not match or not cuda.startswith("cu"):
            raise ValueError(f"TRELLIS.2 needs flash-attn or xformers, and there's no known xformers build for torch {version}.")
        progress("Adding xformers (attention for TRELLIS.2) to ComfyUI")
        # --no-deps and PyTorch's own index for this CUDA build, so torch itself is never replaced.
        r = pip("--no-deps", f"xformers=={match}", "--index-url", f"https://download.pytorch.org/whl/{cuda}")
        if not has("import xformers.ops"):
            raise ValueError("Installing xformers for TRELLIS.2 failed: " + (r.stderr or r.stdout)[-600:])
    return {"flash_attn": flash}


def start(comfy_root, base, log_path, wait_s=240):
    """Start ComfyUI portable without a window. Returns the process (None if it was already running)."""
    if alive(base):
        return None
    root = Path(comfy_root)
    py = root / "python_embeded" / "python.exe"
    main = root / "ComfyUI" / "main.py"
    if not py.is_file() or not main.is_file():
        raise ValueError(f"ComfyUI portable wasn't found at {root}. Set its folder in Settings.")
    port = base.rsplit(":", 1)[-1].strip("/")
    log = open(log_path, "wb")
    # Unbuffered, so progress bars reach the log while TRELLIS.2 runs (Form Studio shows the latest line).
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([str(py), "-s", str(main), "--listen", "127.0.0.1", "--port", port, "--disable-auto-launch"],
                            cwd=str(root), stdout=log, stderr=subprocess.STDOUT, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ValueError(f"ComfyUI stopped while starting (see {log_path})")
        if alive(base):
            return proc
        time.sleep(2)
    proc.terminate()
    raise ValueError("ComfyUI took too long to start")


def stop(base, proc):
    try:
        post_json(base + "/free", {"unload_models": True, "free_memory": True}, timeout=15)
    except (OSError, ValueError):
        pass
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def last_log_line(log_path, limit=90):
    """The latest progress line ComfyUI wrote, e.g. a sampler's percentage."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    parts = [ANSI.sub("", x).strip() for x in tail.replace("\r", "\n").split("\n")]
    parts = [x for x in parts if x and not x.startswith(("aimdo", "[INFO] got prompt"))]
    return parts[-1][:limit] if parts else ""


def find_mesh(outputs):
    def walk(o):
        if isinstance(o, dict):
            if str(o.get("filename", "")).lower().endswith(MESH_EXT):
                return o
            for v in o.values():
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(o, list):
            for v in o:
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(o, str) and o.lower().endswith(MESH_EXT):
            return {"filename": o}
        return None
    for key in sorted(outputs):
        hit = walk(outputs[key])
        if hit:
            return hit
    return None


def newest_mesh(folder: Path, since: float):
    files = [f for f in folder.rglob("*") if f.suffix.lower() in MESH_EXT and f.stat().st_mtime >= since - 1]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}
WIDGET_KINDS = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def _spec(spec):
    spec = list(spec) if isinstance(spec, (list, tuple)) else [spec]
    kind = spec[0] if spec else None
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    return kind, opts


def _choices(kind, opts):
    if isinstance(kind, list):
        return kind
    return opts.get("options") if kind == "COMBO" else None


def _is_widget(kind, opts):
    return not opts.get("forceInput") and (isinstance(kind, list) or kind in WIDGET_KINDS)


def _default(kind, opts):
    if "default" in opts:
        return opts["default"]
    choices = _choices(kind, opts)
    if choices:
        return choices[0]
    return {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False}.get(kind)


def _valid(kind, opts, value):
    choices = _choices(kind, opts)
    if choices is not None:
        return value in choices
    if kind == "BOOLEAN":
        return isinstance(value, bool)
    if kind in ("INT", "FLOAT"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return (opts.get("min") is None or value >= opts["min"]) and (opts.get("max") is None or value <= opts["max"])
    if kind == "STRING":
        return isinstance(value, str)
    return True


def ui_to_api(ui, node_info, targets, overrides=None):
    """Convert a ComfyUI editor workflow (the .json you save from the UI) into the API format /prompt takes.

    node_info(type) returns that node's /object_info entry. Only the targets and what feeds them are kept.
    overrides: {node type: {input: value}}. Returns (prompt, notes); notes lists values that had to fall back to defaults.
    """
    nodes = {n["id"]: n for n in ui.get("nodes", [])}
    links = {}
    for link in ui.get("links", []):
        if isinstance(link, dict):
            links[link["id"]] = (link["origin_id"], link["origin_slot"])
        else:
            links[link[0]] = (link[1], link[2])

    def source(link_id):
        origin, slot = links[link_id]
        while nodes[origin]["type"] == "Reroute":
            upstream = next((i["link"] for i in nodes[origin].get("inputs", []) if i.get("link") is not None), None)
            if upstream is None:
                raise ValueError("A reroute in the mesh workflow isn't connected")
            origin, slot = links[upstream]
        return [str(origin), slot]

    wanted = [n["id"] for n in nodes.values() if n["type"] in targets and n.get("mode", 0) == 0]
    if not wanted:
        raise ValueError(f"The mesh workflow has no {' or '.join(targets)} node")
    keep, stack = set(), list(wanted)
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        node = nodes[nid]
        if node["type"] != "Reroute":
            keep.add(nid)
        for inp in node.get("inputs", []):
            if inp.get("link") is not None:
                stack.append(links[inp["link"]][0])
    prompt, notes = {}, []
    for nid in sorted(keep):
        node = nodes[nid]
        kind_name = node["type"]
        if node.get("mode", 0) != 0:
            raise ValueError(f"The mesh workflow's {kind_name} node is muted or bypassed")
        info = node_info(kind_name)
        if not info:
            raise ValueError(f"ComfyUI doesn't have the {kind_name} node (is ComfyUI-Trellis2 installed?)")
        groups = info.get("input", {})
        order = info.get("input_order") or {}
        names = list(order.get("required", groups.get("required", {}))) + list(order.get("optional", groups.get("optional", {})))
        specs = {**groups.get("required", {}), **groups.get("optional", {})}
        linked = {i["name"]: i["link"] for i in node.get("inputs", []) if i.get("link") is not None}
        values = node.get("widgets_values") or []
        inputs, idx = {}, 0
        for name in names:
            kind, opts = _spec(specs.get(name))
            if not _is_widget(kind, opts):
                continue
            if isinstance(values, dict):
                present, value = name in values, values.get(name)
            else:
                present = idx < len(values)
                value = values[idx] if present else None
                idx += 1
                if present and kind == "INT" and idx < len(values) and isinstance(values[idx], str) and values[idx] in CONTROL_VALUES:
                    idx += 1
                if present and opts.get("image_upload") and idx < len(values) and values[idx] == "image":
                    idx += 1
            if name in linked:
                continue
            if not present or not _valid(kind, opts, value):
                if present:
                    notes.append(f"{kind_name}.{name}: {value!r} isn't allowed, using {_default(kind, opts)!r}")
                value = _default(kind, opts)
            inputs[name] = value
        for name, link_id in linked.items():
            inputs[name] = source(link_id)
        inputs.update((overrides or {}).get(kind_name, {}))
        prompt[str(nid)] = {"class_type": kind_name, "inputs": inputs}
    return prompt, notes


def load_workflow(base, workflow_path: Path):
    """Read an API-format workflow, or convert an editor workflow using the running ComfyUI's node list."""
    workflow = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    if not isinstance(workflow.get("nodes"), list):
        return workflow, []
    settings = (workflow.get("extra") or {}).get("formstudio") or {}
    targets = settings.get("targets") or ["Trellis2ExportMesh"]
    cache = {}

    def node_info(kind):
        if kind not in cache:
            try:
                cache[kind] = get_json(base + "/object_info/" + urllib.parse.quote(kind), timeout=60).get(kind)
            except (OSError, ValueError):
                cache[kind] = None
        return cache[kind]
    return ui_to_api(workflow, node_info, targets, settings.get("overrides"))


def generate(base, workflow_path: Path, image: Path, comfy_root, progress, cancelled, timeout_s=2700, attention=None, running=lambda: True):
    """Queue the workflow on the photo and return the generated mesh file's path."""
    workflow, _notes = load_workflow(base, workflow_path)
    if attention is not None and not attention.get("flash_attn"):
        for node in workflow.values():  # no flash-attn: dense attention via PyTorch, sparse via xformers
            if node.get("class_type") == "Trellis2LoadModel":
                node["inputs"].update(backend="sdpa", sparse_backend="xformers")
    name = upload_image(base, image)
    hits = 0
    for node in workflow.values():
        for k, v in node.get("inputs", {}).items():
            if v == PLACEHOLDER:
                node["inputs"][k] = name
                hits += 1
    if not hits:
        raise ValueError(f"The mesh workflow has no {PLACEHOLDER} input")
    started, quiet_since = time.time(), None
    resp = post_json(base + "/prompt", {"prompt": workflow, "client_id": uuid.uuid4().hex})
    if not resp.get("prompt_id") or resp.get("error") or resp.get("node_errors"):
        raise ValueError(f"ComfyUI rejected the workflow: {json.dumps(resp)[:1500]}")
    pid = resp["prompt_id"]
    while True:
        if cancelled():
            try:
                post_json(base + "/interrupt", {}, timeout=10)
            except (OSError, ValueError):
                pass
            raise InterruptedError()
        try:
            hist = get_json(base + "/history/" + pid, timeout=60)
            quiet_since = None
        except (OSError, ValueError):
            # ComfyUI stops answering while TRELLIS.2 runs heavy steps; that's not a failure unless it lasts or the process died.
            if not running():
                raise ValueError("ComfyUI stopped while building the mesh (see data/comfyui.log)")
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since > 900:
                raise ValueError("ComfyUI stopped responding for 15 minutes while building the mesh")
            progress(int(time.time() - started))
            time.sleep(5)
            continue
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                raise ValueError("TRELLIS.2 failed: " + (json.dumps(msgs[-1][1])[:1500] if msgs else "see ComfyUI log"))
            info = find_mesh(entry.get("outputs", {}))
            out_dir = Path(comfy_root) / "ComfyUI" / "output"
            if info:
                candidate = out_dir / info.get("subfolder", "") / Path(info["filename"]).name
                if not candidate.is_file():
                    candidate = Path(info["filename"])
                if candidate.is_file():
                    return candidate
            found = newest_mesh(out_dir, started)
            if found:
                return found
            raise ValueError("TRELLIS.2 finished but no mesh file was found in ComfyUI's output folder")
        if time.time() - started > timeout_s:
            raise ValueError(f"TRELLIS.2 took longer than {timeout_s // 60} minutes")
        progress(int(time.time() - started))
        time.sleep(2)
