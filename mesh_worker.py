"""Turn a TRELLIS.2 mesh into a clean, real-size STL for printing. Runs in the CadQuery Python (trimesh).

Usage: <cad_python> mesh_worker.py <mesh.glb|obj|ply|stl> <out_dir> (--height MM | --longest MM) [--z-up]
Writes model.stl, metadata.json and (for meshes small enough) view-*.png into out_dir.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh


def repair(mesh, min_frac=0.005):
    m = mesh.copy()
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    parts = m.split(only_watertight=False)
    if len(parts) > 1:
        keep = [c for c in parts if len(c.faces) >= max(5, min_frac * len(m.faces))]
        m = trimesh.util.concatenate(keep) if len(keep) > 1 else keep[0]
        removed = len(parts) - len(keep)
    else:
        removed = 0
    trimesh.repair.fill_holes(m)
    trimesh.repair.fix_normals(m)
    return m, removed


def voxel_remesh(mesh, target_faces=200_000):
    """Rebuild a closed, printable surface from a leaky mesh: voxelize finely (up to 1000 voxels along the longest side),
    fill the inside, contour, then snap the new surface onto the original one so it keeps TRELLIS.2's own detail
    instead of voxel stair-steps. Only real gaps keep the rebuilt surface."""
    sys.path.insert(0, str(Path(__file__).parent))
    from mesh_ops import rebuild, REBUILD_VOXELS
    fixed, _ = rebuild(mesh, voxels=REBUILD_VOXELS, sigma=0.6, passes=20, target_faces=target_faces, snap_to=mesh)
    return fixed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mesh")
    ap.add_argument("out")
    size = ap.add_mutually_exclusive_group(required=True)
    size.add_argument("--height", type=float)
    size.add_argument("--longest", type=float)
    ap.add_argument("--z-up", action="store_true", help="the source is already Z-up (skip the glTF Y-up rotation)")
    a = ap.parse_args(argv)
    src = Path(a.mesh)
    mesh = trimesh.load(src, force="mesh")
    if mesh is None or len(mesh.faces) == 0:
        raise ValueError("The generated mesh is empty")
    if src.suffix.lower() in (".glb", ".gltf") and not a.z_up:
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    mesh, removed = repair(mesh)
    current = mesh.extents[2] if a.height else max(mesh.extents)
    target = a.height or a.longest
    if target <= 0 or current <= 0:
        raise ValueError("Give a size above 0 mm")
    mesh.apply_scale(target / current)
    remeshed = False
    if not mesh.is_watertight:
        try:
            fixed = voxel_remesh(mesh)
            if fixed.is_watertight and fixed.volume > 0:
                mesh, remeshed = fixed, True
                current = mesh.extents[2] if a.height else max(mesh.extents)
                mesh.apply_scale(target / current)  # the voxel surface sits a fraction of a voxel outside
        except Exception as exc:  # keep the original mesh; Bambu Studio can still repair it
            print(f"voxel remesh skipped: {exc}", file=sys.stderr)
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    mesh.export(out / "model.stl")
    watertight = bool(mesh.is_watertight)
    if len(mesh.faces) <= 200_000:
        sys.path.insert(0, str(Path(__file__).parent))
        from preview_render import render_views
        render_views(out / "model.stl", out)
    meta = {"dimensions": [round(float(x), 3) for x in mesh.extents], "volume": round(float(mesh.volume), 3) if watertight else 0.0,
            "solids": len(mesh.split(only_watertight=False)), "valid": watertight, "mesh": True, "faces": int(len(mesh.faces)), "area": round(float(mesh.area), 3),
            "specks_removed": removed, "remeshed": remeshed}
    (out / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    print(json.dumps(meta))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
