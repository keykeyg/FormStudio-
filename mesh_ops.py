"""Mesh operations for Form Studio. Runs in the CadQuery Python (trimesh + manifold3d + numpy).

  boolean  <base.stl> <tools dir> <out>          apply tool_cut.stl / tool_add.stl from the tools dir (CAD edits on meshes)
  scale    <base.stl> <out> <sx> <sy> <sz>       scale about the centre, base stays at Z=0
  stretch  <base.stl> <out> <axis> <at> <by> [band]   lengthen (+) or shorten (-) the part across a plane, inside a band
  import   <file> <out>                           STL / OBJ / PLY / GLB / 3MF -> parts in out/parts, assembled model.stl
  assemble <out>                                  rebuild out/model.stl from out/parts.json

Every command writes model.stl, metadata.json and (for meshes up to 200k triangles) view-*.png into <out>.
The legacy form `mesh_ops.py <base.stl> <tools dir> <out>` still means boolean.
"""
import json
import math
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

AXES = {"x": 0, "y": 1, "z": 2}


def load(path):
    path = Path(path)
    mesh = load_3mf(path) if path.suffix.lower() == ".3mf" else trimesh.load(path, force="mesh")
    if mesh is None or len(mesh.faces) == 0:
        raise ValueError(f"{path.name} has no triangles")
    return weld(mesh)


def weld(mesh):
    mesh.merge_vertices()
    if not mesh.is_watertight:
        welded = mesh.copy()
        welded.merge_vertices(digits_vertex=2)
        welded.update_faces(welded.nondegenerate_faces())
        welded.remove_unreferenced_vertices()
        if welded.is_watertight:
            return welded
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_normals(mesh)
    return mesh


# ---- 3MF (standard library XML; handles components, transforms, units, Bambu's per-object model files) ----
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def matrix(text):
    if not text:
        return np.eye(4)
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        return np.eye(4)
    # 3MF uses row vectors: [x y z 1] * M, with the translation in the last row.
    return np.array([[v[0], v[3], v[6], v[9]], [v[1], v[4], v[7], v[10]], [v[2], v[5], v[8], v[11]], [0, 0, 0, 1.0]])


def load_3mf_parts(path):
    """Each build item becomes one part: (name, trimesh) in millimetres."""
    z = zipfile.ZipFile(path)
    names = {n.lower(): n for n in z.namelist()}
    main = "3D/3dmodel.model"
    rels = names.get("_rels/.rels")
    if rels:
        m = re.search(rb'Target="/?([^"]+\.model)"', z.read(rels))
        if m:
            main = m[1].decode()
    roots = {}

    def root(p):
        p = p.lstrip("/")
        key = names.get(p.lower(), p)
        if key not in roots:
            roots[key] = ET.fromstring(z.read(key))
        return roots[key]

    scale = UNITS.get(root(main).get("unit", "millimeter"), 1.0)

    def collect(p, oid, t, depth=0):
        if depth > 16:
            raise ValueError("3MF components nest too deeply")
        obj = next((o for o in root(p).iter("{%s}object" % NS["m"]) if o.get("id") == oid), None)
        if obj is None:
            return []
        out = []
        mesh = obj.find("m:mesh", NS)
        if mesh is not None:
            verts = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))] for v in mesh.find("m:vertices", NS)], dtype=float)
            tris = np.array([[int(t_.get("v1")), int(t_.get("v2")), int(t_.get("v3"))] for t_ in mesh.find("m:triangles", NS)], dtype=np.int64)
            if len(verts) and len(tris):
                verts = (t @ np.c_[verts, np.ones(len(verts))].T).T[:, :3]
                out.append(trimesh.Trimesh(verts, tris, process=False))
        comps = obj.find("m:components", NS)
        if comps is not None:
            for c in comps:
                out += collect(c.get(PROD) or p, c.get("objectid"), t @ matrix(c.get("transform")), depth + 1)
        return out

    parts = []
    build = root(main).find("m:build", NS)
    objects = {o.get("id"): o for o in root(main).iter("{%s}object" % NS["m"])}
    for i, item in enumerate(build if build is not None else []):
        pieces = collect(item.get(PROD) or main, item.get("objectid"), matrix(item.get("transform")))
        if pieces:
            mesh = trimesh.util.concatenate(pieces)
            mesh.apply_scale(scale)
            obj = objects.get(item.get("objectid"))
            name = obj.get("name") if obj is not None else None
            parts.append((name or f"part {i + 1}", mesh))
    if not parts:
        raise ValueError("No printable objects were found in this 3MF")
    return parts


def load_3mf(path):
    return trimesh.util.concatenate([m for _, m in load_3mf_parts(path)])


# ---- finishing ----
def place_on_bed(mesh):
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return mesh


def finish(mesh, out, extra=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise ValueError("Nothing was left of the part")
    mesh.export(out / "model.stl")
    if len(mesh.faces) <= 200_000:
        sys.path.insert(0, str(Path(__file__).parent))
        from preview_render import render_views
        render_views(out / "model.stl", out)
    pieces = mesh.split(only_watertight=False)
    boxes = [[round(float(v), 2) for v in (p.bounds[0][0], p.bounds[1][0], p.bounds[0][1], p.bounds[1][1], p.bounds[0][2], p.bounds[1][2])] for p in pieces[:10]]
    meta = {"dimensions": [round(float(x), 3) for x in mesh.extents], "volume": round(float(mesh.volume), 3) if mesh.is_watertight else 0.0,
            "area": round(float(mesh.area), 3), "solids": len(pieces), "valid": bool(mesh.is_watertight), "mesh": True,
            "faces": int(len(mesh.faces)), "solid_boxes": boxes, **(extra or {})}
    (out / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    print(json.dumps(meta))
    return meta


# ---- operations ----
def boolean(base, tools, out):
    tools = Path(tools)
    mesh = load(base)
    if not mesh.is_watertight:
        raise ValueError("This mesh has holes, so CAD cuts can't be applied to it. Use Make printable or repair it in Bambu Studio first.")
    if (tools / "tool_add.stl").is_file():
        mesh = trimesh.boolean.union([mesh, load(tools / "tool_add.stl")], engine="manifold")
    if (tools / "tool_cut.stl").is_file():
        mesh = trimesh.boolean.difference([mesh, load(tools / "tool_cut.stl")], engine="manifold")
    return finish(mesh, out)


def scale(base, out, sx, sy, sz):
    factors = [float(sx), float(sy), float(sz)]
    if not all(0.01 <= f <= 100 for f in factors):
        raise ValueError("Scale each axis by 1% to 10000%")
    mesh = load(base)
    lo, hi = mesh.bounds
    center = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]]
    mesh.apply_translation([-c for c in center])
    mesh.apply_transform(np.diag(factors + [1.0]))
    return finish(place_on_bed(mesh), out, {"scaled": factors})


def stretch(base, out, axis, at, by, band=None):
    """Lengthen or shorten across the plane axis=at. Only a band around the plane is stretched or squeezed;
    everything past the band just moves, so holes and details outside the band keep their shape."""
    k = AXES[str(axis).lower()]
    at, by = float(at), float(by)
    mesh = load(base)
    lo, hi = float(mesh.bounds[0][k]), float(mesh.bounds[1][k])
    if not lo < at < hi:
        raise ValueError(f"The stretch plane {axis.upper()}={at:g} is outside the part ({lo:g}..{hi:g} mm)")
    width = float(band) if band else max(2 * abs(by), 0.1 * (hi - lo))
    a, b = max(lo, at - width / 2), min(hi, at + width / 2)
    if b - a + by <= 0.2 * (b - a) or b - a <= 0:
        raise ValueError(f"Can't shorten by {abs(by):g} mm across a {b - a:g} mm band; pick a spot with more straight length, or shorten in smaller steps")
    v = mesh.vertices.copy()
    c = v[:, k].copy()
    inside = (c > a) & (c < b)
    v[inside, k] = a + (c[inside] - a) * (b - a + by) / (b - a)
    v[c >= b, k] = c[c >= b] + by
    mesh.vertices = v
    return finish(place_on_bed(mesh), out, {"stretched": {"axis": axis.lower(), "at": at, "by": by, "band": [round(a, 2), round(b, 2)]}})


def import_model(path, out):
    out = Path(out)
    (out / "parts").mkdir(parents=True, exist_ok=True)
    path = Path(path)
    named = load_3mf_parts(path) if path.suffix.lower() == ".3mf" else [(path.stem, trimesh.load(path, force="mesh"))]
    parts, x = [], 0.0
    for i, (name, mesh) in enumerate(named[:12]):
        mesh = place_on_bed(weld(mesh))
        file = f"parts/p{i + 1}.stl"
        mesh.export(out / file)
        width = float(mesh.extents[0])
        parts.append({"name": str(name)[:60], "file": file, "translate": [x + width / 2, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0]})
        x += width + 10.0
    if len(parts) > 1:  # center the row of parts
        shift = -x / 2 + 5.0
        for part in parts:
            part["translate"][0] += shift
    else:
        parts[0]["translate"] = [0.0, 0.0, 0.0]
    (out / "parts.json").write_text(json.dumps(parts, indent=1), encoding="utf-8")
    return assemble(out)


def transformed(part, folder):
    mesh = trimesh.load(Path(folder) / part["file"], force="mesh")
    rx, ry, rz = (math.radians(float(a)) for a in part.get("rotate", [0, 0, 0]))
    for angle, axis in ((rx, [1, 0, 0]), (ry, [0, 1, 0]), (rz, [0, 0, 1])):
        if angle:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(angle, axis, mesh.bounds.mean(axis=0)))
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])  # rotation keeps the part sitting on its base
    mesh.apply_translation(part.get("translate", [0, 0, 0]))
    return weld(mesh)


def assemble(out):
    out = Path(out)
    parts = json.loads((out / "parts.json").read_text(encoding="utf-8"))
    meshes = [transformed(p, out) for p in parts]
    if len(meshes) > 1 and all(m.is_watertight for m in meshes):
        try:
            mesh = trimesh.boolean.union(meshes, engine="manifold")
        except Exception:
            mesh = trimesh.util.concatenate(meshes)
    else:
        mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
    lo = mesh.bounds[0]
    if lo[2] < -0.01:  # nothing below the bed
        mesh.apply_translation([0, 0, -lo[2]])
    boxes = []
    for p, m in zip(parts, meshes):
        b = m.bounds
        boxes.append({"name": p["name"], "box": [round(float(b[0][0]), 1), round(float(b[1][0]), 1), round(float(b[0][1]), 1),
                                                 round(float(b[1][1]), 1), round(float(b[0][2]), 1), round(float(b[1][2]), 1)]})
    return finish(mesh, out, {"parts": boxes})


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv and argv[0] in ("boolean", "scale", "stretch", "import", "assemble") else ("boolean", argv)
    {"boolean": boolean, "scale": scale, "stretch": stretch, "import": import_model, "assemble": assemble}[cmd](*args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
