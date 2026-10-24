"""Apply CAD cuts/additions to a mesh version. Runs in the CadQuery Python (trimesh + manifold3d).

Usage: <cad_python> mesh_ops.py <base model.stl> <tools dir> <out dir>
Reads tool_cut.stl / tool_add.stl from the tools dir; writes model.stl, metadata.json and views into out dir.
"""
import json
import sys
from pathlib import Path

import trimesh


def load(path):
    mesh = trimesh.load(path, force="mesh")
    mesh.merge_vertices()
    if not mesh.is_watertight:
        welded = mesh.copy()
        welded.merge_vertices(digits_vertex=2)
        welded.update_faces(welded.nondegenerate_faces())
        welded.remove_unreferenced_vertices()
        if welded.is_watertight:
            mesh = welded
        else:
            trimesh.repair.fill_holes(mesh)
            trimesh.repair.fix_normals(mesh)
    return mesh


def main(base, tools, out):
    tools, out = Path(tools), Path(out)
    mesh = load(base)
    if not mesh.is_watertight:
        raise ValueError("This mesh has holes, so CAD cuts can't be applied to it. Use Make printable or repair it in Bambu Studio first.")
    if (tools / "tool_add.stl").is_file():
        mesh = trimesh.boolean.union([mesh, load(tools / "tool_add.stl")], engine="manifold")
    if (tools / "tool_cut.stl").is_file():
        mesh = trimesh.boolean.difference([mesh, load(tools / "tool_cut.stl")], engine="manifold")
    if mesh.is_empty or len(mesh.faces) == 0:
        raise ValueError("Nothing was left after the cut")
    out.mkdir(parents=True, exist_ok=True)
    mesh.export(out / "model.stl")
    if len(mesh.faces) <= 200_000:
        sys.path.insert(0, str(Path(__file__).parent))
        from preview_render import render_views
        render_views(out / "model.stl", out)
    parts = mesh.split(only_watertight=False)
    boxes = [[round(float(v), 2) for v in (p.bounds[0][0], p.bounds[1][0], p.bounds[0][1], p.bounds[1][1], p.bounds[0][2], p.bounds[1][2])] for p in parts[:10]]
    meta = {"dimensions": [round(float(x), 3) for x in mesh.extents], "volume": round(float(mesh.volume), 3) if mesh.is_watertight else 0.0,
            "area": round(float(mesh.area), 3), "solids": len(parts), "valid": bool(mesh.is_watertight), "mesh": True,
            "faces": int(len(mesh.faces)), "solid_boxes": boxes}
    (out / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    print(json.dumps(meta))


if __name__ == "__main__":
    try:
        main(*sys.argv[1:4])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
