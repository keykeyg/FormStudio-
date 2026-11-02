#!/usr/bin/env python3
"""Form Studio connector for the Claude app (MCP over stdio). Standard library only.

Lets Claude (Claude Desktop, Claude Code, or a Claude chat linked to this PC) work on your Form Studio designs:
  list_projects            your recent designs
  get_design               a design's active version: size, CAD source, last messages, renders
  get_rebuild_context      for "Rebuild as CAD": the mesh measurements, the best CAD so far, its deviation report, renders
  build_and_compare        build a CadQuery program, check it against the mesh, keep it as a new version
  start_rebuild            run Form Studio's own rebuild (measured CAD + your local model)
  job_status               wait for a running Form Studio job
  set_active_version       switch the design to another version

Form Studio must be open (it talks to http://127.0.0.1:8765). Every change shows up live in Form Studio.
Add it to Claude Desktop with Connect-Claude.cmd (or connect_claude.py).
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("FORM_STUDIO_URL", "http://127.0.0.1:8765").rstrip("/")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost through a proxy
VERSION = "1.7"


class Problem(Exception):
    pass


# ---- talking to Form Studio ----
def http(path, payload=None, raw=False, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    if payload is not None:
        headers["X-Form-Token"] = token()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error")
        except ValueError:
            message = None
        raise Problem(message or f"Form Studio returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise Problem("Form Studio isn't running. Open it from the desktop shortcut, then try again.") from exc
    return body if raw else json.loads(body)


_TOKEN = None


def token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = http("/api/session")["token"]
    return _TOKEN


def post(path, payload):
    global _TOKEN
    try:
        return http(path, payload)
    except Problem as exc:
        if "Refresh the app" in str(exc):  # Form Studio restarted: new token
            _TOKEN = None
            return http(path, payload)
        raise


def find_project(ref):
    projects = http("/api/projects")
    if not projects:
        raise Problem("There are no Form Studio projects yet.")
    ref = str(ref or "").strip()
    if not ref:
        return http(f"/api/projects/{projects[0]['id']}")
    for p in projects:
        if p["id"] == ref:
            return http(f"/api/projects/{p['id']}")
    matches = [p for p in projects if ref.lower() in p["name"].lower()]
    if not matches:
        raise Problem(f"No project matches \"{ref}\". Recent ones: " + "; ".join(p["name"] for p in projects[:8]))
    return http(f"/api/projects/{matches[0]['id']}")


def version(p, ref=None):
    if ref in (None, "", 0):
        return next(v for v in p["versions"] if v["id"] == p["active"])
    for v in p["versions"]:
        if v["id"] == ref or str(v["number"]) == str(ref).lstrip("v").lstrip("#"):
            return v
    raise Problem(f"Version {ref} isn't in \"{p['name']}\".")


def file_text(p, v, name):
    try:
        return http(f"/files/{p['id']}/{v['id']}/{name}", raw=True).decode("utf-8")
    except Problem:
        return None


def image(p, v, name):
    try:
        data = http(f"/files/{p['id']}/{v['id']}/{name}", raw=True)
    except Problem:
        return None
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def run_job(p, payload, wait=50):
    started = post(f"/api/projects/{p['id']}/jobs", payload)
    return wait_job(started["job"], wait)


def wait_job(jid, wait=50):
    deadline = time.time() + max(5, min(int(wait), 55))
    while True:
        job = http(f"/api/jobs/{jid}")
        job["id"] = jid
        if job.get("status") != "running":
            return job
        if time.time() >= deadline:
            return job
        time.sleep(1)


def describe(v):
    dims = " x ".join(f"{d:g}" for d in v["dimensions"])
    kind = "mesh" if v.get("mesh") else "CAD"
    extra = ""
    if v.get("rebuild"):
        rb = v["rebuild"]
        extra = f", rebuild fit {rb['within'] * 100:.0f}% within ±{rb['tol']:g} mm (mean {rb['mean']:g} mm)"
    return f"version {v['number']} ({kind}, {dims} mm, by {v.get('model', '?')}{extra})"


# ---- tools ----
def list_projects(limit=10):
    projects = http("/api/projects")[: max(1, min(int(limit), 30))]
    lines = []
    for item in projects:
        p = http(f"/api/projects/{item['id']}")
        v = next((v for v in p["versions"] if v["id"] == p["active"]), None)
        lines.append(f"- {p['name']} (id {p['id']}): {describe(v) if v else 'no versions yet'}, {len(p['versions'])} versions")
    return [text("\n".join(lines) or "No projects yet.")]


def get_design(project="", version_ref=""):
    p = find_project(project)
    v = version(p, version_ref)
    out = [f"PROJECT: {p['name']} (id {p['id']})", "ACTIVE: " + describe(v), "", "Recent messages:"]
    for m in p["messages"][-6:]:
        out.append(f"  {m['role']}: {m['text'][:400]}")
    code = file_text(p, v, "model.py")
    if code:
        out += ["", "CADQUERY SOURCE:", "```python", code, "```"]
    elif v.get("mesh"):
        out += ["", "This version is a mesh (no CAD source). Use get_rebuild_context to rebuild it as CAD."]
    content = [text("\n".join(out))]
    for name in ("view-isometric.png", "view-front.png"):
        img = image(p, v, name)
        if img:
            content.append(img)
    return content


def _reference(p):
    rb = p.get("rebuild") or {}
    if rb.get("reference"):
        return version(p, rb["reference"])
    v = version(p)
    if v.get("mesh"):
        return v
    raise Problem("This design has no mesh to rebuild from. Make the mesh version active in Form Studio first.")


def get_rebuild_context(project=""):
    p = find_project(project)
    ref = _reference(p)
    meas = file_text(p, ref, "measure.json")
    if meas is None:
        job = run_job(p, {"kind": "measure", "reference": ref["id"]}, wait=55)
        if job.get("status") == "running":
            return [text(f"Measuring the mesh (job {job['id']}). Call get_rebuild_context again in a minute.")]
        if job.get("status") == "error":
            raise Problem(job.get("error") or "Measuring failed")
        p = find_project(p["id"])
        meas = file_text(p, ref, "measure.json")
    meas = json.loads(meas)
    rb = p.get("rebuild") or {}
    tol = rb.get("tol", 0.5)
    lines = [f"PROJECT: {p['name']} (id {p['id']})", f"REFERENCE MESH: {describe(ref)}", "", meas["summary"], ""]
    best = version(p, rb["best"]) if rb.get("best") else None
    content = []
    if best:
        code = file_text(p, best, "model.py")
        dev = json.loads(file_text(p, best, "deviation.json") or "{}")
        lines += [f"BEST CAD SO FAR: {describe(best)}", "```python", code or "", "```", "", dev.get("report", "")]
    else:
        lines += ["NO CAD YET. Form Studio's measured baseline (it builds; start from it):", "```python", meas["program"], "```"]
    lines += ["", f"HOW TO WORK: keep the mesh's coordinates (X and Y centered, base at Z=0). Send the complete program to build_and_compare; "
              f"it builds it, compares it with the mesh (tolerance ±{tol:g} mm), keeps it as a new version and tells you where material is missing or extra. "
              "Make it a clean parametric part (UPPERCASE parameters, straight lines and arcs where the outline is straight or round, exact round holes)."]
    content.append(text("\n".join(lines)))
    for v, name, label in ((ref, "view-isometric.png", "Mesh, isometric"), (ref, "view-top.png", "Mesh, top"),
                           (best, "view-isometric.png", "Best CAD so far, isometric")):
        if v:
            img = image(p, v, name)
            if img:
                content += [text(label + ":"), img]
    return content


def build_and_compare(project, code, message="", tolerance=None):
    p = find_project(project)
    ref = _reference(p)
    payload = {"kind": "rebuild_build", "code": code, "reference": ref["id"], "message": message or "New CAD from Claude.",
               "label": "Rebuild · Claude (chat)"}
    if tolerance:
        payload["tolerance"] = float(tolerance)
    job = run_job(p, payload, wait=55)
    if job.get("status") == "running":
        return [text(f"Still building (job id {job['id']}). Call job_status, then get_rebuild_context.")]
    if job.get("status") == "error":
        raise Problem("The program didn't build or couldn't be compared:\n" + str(job.get("error"))[-3000:])
    p = find_project(p["id"])
    v = version(p)
    dev = json.loads(file_text(p, v, "deviation.json") or "{}")
    best = (p.get("rebuild") or {}).get("best") == v["id"]
    content = [text(f"Built {describe(v)}. {'It is the best match so far.' if best else 'An earlier version still matches better.'}\n\n{dev.get('report', '')}")]
    for name in ("view-isometric.png", "view-top.png"):
        img = image(p, v, name)
        if img:
            content.append(img)
    return content


def start_rebuild(project="", engine="local"):
    p = find_project(project)
    status = http("/api/status")
    model = (status.get("models") or [{}])[0].get("id", "")
    job = run_job(p, {"kind": "rebuild", "engine": engine if engine in ("local", "chat", "api") else "local", "model": model}, wait=55)
    if job.get("status") == "running":
        return [text(f"Form Studio is rebuilding \"{p['name']}\" (job {job['id']}): {job.get('phase', '')}. Call job_status with that id.")]
    return [text(_job_result(job, p))]


def _job_result(job, p):
    if job.get("status") == "error":
        return "Failed: " + str(job.get("error"))
    p = find_project(p["id"])
    last = next((m["text"] for m in reversed(p["messages"]) if m["role"] == "assistant"), "")
    return f"Done. {last}"


def job_status(job_id, wait=50):
    job = wait_job(job_id, wait)
    if job.get("status") == "running":
        return [text(f"Still running: {job.get('phase', '')}. Call job_status again.")]
    pid = job.get("project_id")
    return [text(_job_result(job, http(f"/api/projects/{pid}")) if pid and job.get("status") != "error" else f"{job.get('status')}: {job.get('error', '')}")]


def set_active_version(project, version_ref):
    p = find_project(project)
    v = version(p, version_ref)
    post(f"/api/projects/{p['id']}/restore", {"version": v["id"]})
    return [text(f"\"{p['name']}\" now shows {describe(v)}.")]


def text(value):
    return {"type": "text", "text": value}


S = {"type": "string"}
TOOLS = {
    "list_projects": (list_projects, "List recent Form Studio designs with their active version.",
                      {"limit": {"type": "integer", "description": "How many (default 10)"}}, []),
    "get_design": (get_design, "Show a design's active (or chosen) version: size, CadQuery source, recent messages and renders.",
                   {"project": {**S, "description": "Project name (part of it is fine) or id; empty = most recent"},
                    "version_ref": {**S, "description": "Version number, e.g. 4; empty = active"}}, []),
    "get_rebuild_context": (get_rebuild_context, "For rebuilding a mesh as CAD: the mesh's measurements (sections, bands, holes), the best CAD so far "
                            "(or Form Studio's measured baseline program) with its deviation report, and renders.",
                            {"project": {**S, "description": "Project name or id; empty = most recent"}}, []),
    "build_and_compare": (build_and_compare, "Build a complete CadQuery program (assign result; mesh coordinates; only cadquery and math), "
                          "compare it with the reference mesh, and keep it as a new version. Returns the deviation report and renders.",
                          {"project": {**S, "description": "Project name or id"}, "code": {**S, "description": "The complete program"},
                           "message": {**S, "description": "One sentence on what changed (shown in Form Studio)"},
                           "tolerance": {"type": "number", "description": "Match tolerance in mm (default: the project's, usually 0.5)"}},
                          ["project", "code"]),
    "start_rebuild": (start_rebuild, "Start Form Studio's own Rebuild as CAD (measured CAD, then the loaded local model refines it).",
                      {"project": {**S, "description": "Project name or id"}, "engine": {**S, "description": "local (default), chat or api"}}, []),
    "job_status": (job_status, "Wait for a Form Studio job and report how it ended.",
                   {"job_id": {**S}, "wait": {"type": "integer", "description": "Seconds to wait, up to 55"}}, ["job_id"]),
    "set_active_version": (set_active_version, "Make another version of a design the active one.",
                           {"project": {**S}, "version_ref": {**S, "description": "Version number"}}, ["project", "version_ref"]),
}


# ---- MCP (JSON-RPC 2.0 over stdio, one message per line) ----
def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # a notification
    if method == "initialize":
        return {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "form-studio", "version": VERSION},
                "instructions": "Form Studio is the user's local CAD app. Use get_rebuild_context before build_and_compare."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": props, "required": req}}
                          for n, (_, d, props, req) in TOOLS.items()]}
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return {"content": [text(f"Unknown tool {name}")], "isError": True}
        try:
            return {"content": TOOLS[name][0](**args)}
        except Problem as exc:
            return {"content": [text(str(exc))], "isError": True}
        except TypeError as exc:
            return {"content": [text(f"Bad arguments: {exc}")], "isError": True}
        except Exception as exc:  # keep the connector alive
            return {"content": [text(f"{type(exc).__name__}: {exc}")], "isError": True}
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            result = handle(msg)
            if result is None:
                continue
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        except LookupError:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": f"Method not found: {msg.get('method')}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
