"""The planner in front of the chat: turns what you say into one concrete operation. Standard library only.

For mesh versions (TRELLIS meshes, imports, combined parts) the chat can't edit source code, so requests are routed:
resize / scale / stretch / place a part / CAD cut-or-add / ask / answer. Simple size requests are handled by rules
without a model; everything else goes to the local model with a short text-only prompt that returns JSON.
"""
from __future__ import annotations

import json
import re

TOOLS = {"resize", "scale", "stretch", "place", "polish", "cad", "ask", "answer"}
UNIT = {"mm": 1.0, "cm": 10.0, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4}
NUM = r"(\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in\b|\")?"
POLISH_WORDS = re.compile(r"\b(smooth\w*|polish\w*|steppy|stair[- ]?step\w*|stepped|stepping|jagged|rough|bumpy|lumpy|blocky|pixel\w*|terrac\w*)\b", re.I)
FEATURE_WORDS = re.compile(r"\b(hole|holes|slot|bore|drill|tab|boss|thread|insert|screw|nut|notch|groove|text|logo|rib|lip|pocket|recess|flat|fillet|chamfer|longer|shorter|bigger|smaller|scale|resize|stretch|move|rotate|mm|cm|inch|inches)\b", re.I)
CAD_WORDS = re.compile(r"\b(hole|holes|slot|bore|cut|drill|tab|boss|fillet|chamfer|flat|recess|pocket|thread|insert|screw|nut|notch|groove|text|logo|rib|lip|add|remove)\b", re.I)

PLANNER_SYSTEM = """You plan edits to a 3D-printable part. Reply with ONLY one JSON object, no other text.
Pick exactly one tool:
{"tool":"resize","size":[x,y,z],"lock":true}  overall size in mm; null for axes you don't set. lock=true keeps proportions (use it when only one size is given, unless the person says to change just that direction).
{"tool":"scale","factor":F}  uniform scale, e.g. "half size" -> 0.5; "it measures 912 but should be 216" -> 216/912.
{"tool":"stretch","axis":"x|y|z","at":MM,"by":MM}  make one section longer (+) or shorter (-) across the plane axis=at, leaving the ends' details as they are. Use for "make the butt 10 mm longer", "shorten the middle by 5". Pick "at" inside that section (use a clicked spot's coordinate if given).
{"tool":"place","part":"NAME","translate":[x,y,z],"rotate":[rx,ry,rz]}  move or turn one part of a combined design; translate is where the part's bottom-center goes, in mm; rotate in degrees. Omit what doesn't change.
{"tool":"polish","strength":"light|medium|strong"}  smooth away stair-steps, bumps and roughness on the surface; size stays the same.
{"tool":"cad","request":"..."}  anything that adds or removes material at exact sizes: holes, slots, bores, flats, tabs, slots for build plates, chamfers. Restate it precisely in mm and in this part's coordinates.
{"tool":"ask","question":"..."}  only when you truly cannot act; ask one short question.
{"tool":"answer","text":"..."}  questions that need no change (e.g. "how long is it?").
Axes: X left/right, Y front/back, Z up/down, Z=0 is the bed. "Length" means the longest side unless the person says otherwise. "Clicked spots" are exact points on the part."""


def context(version, user_text, recent=()):
    dims = version.get("dimensions", [0, 0, 0])
    boxes = version.get("solid_boxes") or []
    lines = [f"CURRENT VERSION: {'a combined design of parts' if version.get('parts') else 'a sculpted or imported mesh'} (no CAD source).",
             f"Size: X {dims[0]:g} x Y {dims[1]:g} x Z {dims[2]:g} mm. Longest side: {'XYZ'[dims.index(max(dims))]}."]
    if boxes:
        b = boxes[0]
        lines.append(f"Bounding box: X {b[0]:g}..{b[1]:g}, Y {b[2]:g}..{b[3]:g}, Z {b[4]:g}..{b[5]:g} mm.")
    for part in version.get("parts") or []:
        x0, x1, y0, y1, z0, z1 = part["box"]
        lines.append(f"Part \"{part['name']}\": X {x0:g}..{x1:g}, Y {y0:g}..{y1:g}, Z {z0:g}..{z1:g} mm.")
    for turn in recent[-4:]:
        lines.append(f"Earlier ({turn['role']}): {turn['text'][:300]}")
    lines.append("REQUEST: " + user_text)
    return "\n".join(lines)


def parse_plan(raw):
    """The first JSON object in the reply, checked; None if unusable."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", raw or "")
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    plan = json.loads(text[start:i + 1])
                except ValueError:
                    break
                return plan if valid(plan) else None
        start = text.find("{", start + 1)
    return None


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid(plan):
    if not isinstance(plan, dict) or plan.get("tool") not in TOOLS:
        return False
    t = plan["tool"]
    if t == "resize":
        size = plan.get("size")
        return isinstance(size, list) and len(size) == 3 and any(num(s) and s > 0 for s in size) and all(s is None or (num(s) and s > 0) for s in size)
    if t == "scale":
        return num(plan.get("factor")) and 0.01 <= plan["factor"] <= 100
    if t == "stretch":
        return str(plan.get("axis", "")).lower() in ("x", "y", "z") and num(plan.get("at")) and num(plan.get("by")) and plan["by"] != 0
    if t == "place":
        return isinstance(plan.get("part"), str) and (isinstance(plan.get("translate"), list) or isinstance(plan.get("rotate"), list))
    if t == "polish":
        return plan.get("strength") in (None, "light", "medium", "strong")
    if t == "cad":
        return isinstance(plan.get("request"), str) and plan["request"].strip() != ""
    return isinstance(plan.get("question" if t == "ask" else "text"), str)


def mm(value, unit):
    return float(value) * UNIT.get((unit or "mm").lower().strip(), 1.0)


def quick_plan(text, version):
    """Plain size requests, handled without a model. None when the request needs the planner."""
    if POLISH_WORDS.search(text) and not FEATURE_WORDS.search(text):
        strong = re.search(r"\b(very|really|super|much|lot|way|strong\w*|heav\w*|fully|completely)\b", text, re.I)
        light = re.search(r"\b(little|bit|slight\w*|light\w*|gentl\w*|touch)\b", text, re.I)
        return {"tool": "polish", "strength": "strong" if strong else "light" if light else "medium"}
    if CAD_WORDS.search(text) and not re.search(r"\b(longer|shorter|bigger|smaller|scale|resize|stretch|move|rotate|turn|put|place|tall|taller|long|wide|wider|size)\b", text, re.I):
        return {"tool": "cad", "request": text}  # plainly a material change: no planner call needed
    if CAD_WORDS.search(text) or re.search(r"\b(thick|thickness|wide|width|deep|depth|only|just)\b", text, re.I):
        return None  # one direction only, or mixed requests: let the planner decide
    dims = version.get("dimensions") or [0, 0, 0]
    low = text.lower()
    # "the 912 mm needs to be 216", "it's 300 mm, should be 250", "from 120 to 100 mm"
    m = re.search(NUM + r"[^\d]{0,40}?\b(?:needs? to be|should be|to be|->|→|to)\s*" + NUM, low)
    if m and "%" not in low:
        have, want = mm(m[1], m[2] or m[4]), mm(m[3], m[4] or m[2])
        if have > 0 and want > 0 and abs(have - want) > 1e-6:
            return {"tool": "scale", "factor": round(want / have, 6), "why": f"{have:g} mm should be {want:g} mm"}
    # "make it 216 mm long", "180 mm tall"
    m = re.search(r"\b(?:make|set|resize|scale)?\b[^.]*?" + NUM + r"\s*(long|tall|high)\b", low)
    if m:
        size = [None, None, None]
        axis = 2 if m[3] in ("tall", "high") else dims.index(max(dims))
        size[axis] = round(mm(m[1], m[2]), 3)
        return {"tool": "resize", "size": size, "lock": True}
    return None


def resize_factors(dims, plan):
    if plan["tool"] == "scale":
        f = float(plan["factor"])
        return [f, f, f]
    size = plan["size"]
    ratios = [(s / d) if (s and d) else None for s, d in zip(size, dims)]
    if plan.get("lock", True):
        f = next(r for r in ratios if r)
        return [f, f, f]
    return [r or 1.0 for r in ratios]


def place(parts, plan):
    """Updated parts list for a place plan; matches the part by name (case-insensitive, partial) or number."""
    key = plan["part"].strip().lower()
    index = next((i for i, p in enumerate(parts) if p["name"].lower() == key), None)
    if index is None:
        index = next((i for i, p in enumerate(parts) if key in p["name"].lower() or p["name"].lower() in key), None)
    if index is None and key.isdigit() and 0 < int(key) <= len(parts):
        index = int(key) - 1
    if index is None:
        raise ValueError(f"There's no part called \"{plan['part']}\". Parts: " + ", ".join(p["name"] for p in parts))
    updated = [dict(p) for p in parts]
    part = updated[index]
    if isinstance(plan.get("translate"), list) and len(plan["translate"]) == 3:
        part["translate"] = [float(v) if num(v) else float(old) for v, old in zip(plan["translate"], part.get("translate", [0, 0, 0]))]
    if isinstance(plan.get("rotate"), list) and len(plan["rotate"]) == 3:
        part["rotate"] = [float(v) if num(v) else float(old) for v, old in zip(plan["rotate"], part.get("rotate", [0, 0, 0]))]
    return updated, part["name"]
