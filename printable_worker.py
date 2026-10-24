"""Make a model printable: lay it on its largest flat side, center it on the bed, and split it
into pieces that fit the printer with dowel-pin holes. Runs in the CadQuery Python (trimesh + manifold3d).

Usage: <cad_python> printable_worker.py <model.stl> <out_dir> <bed_x> <bed_y> <bed_z> [--margin 5] [--no-pins]
Writes <out_dir>/piece_01.stl ... and <out_dir>/report.json.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh


def lay_flat(mesh):
    """Rotate so the largest flat face of the convex hull rests on the bed (works for CAD and organic meshes)."""
    hull = mesh.convex_hull
    if len(hull.facets):
        i = int(np.argmax(hull.facets_area))
        normal, area = hull.facets_normal[i], float(hull.facets_area[i])
    else:
        i = int(np.argmax(hull.area_faces))
        normal, area = hull.face_normals[i], float(hull.area_faces[i])
    m = mesh.copy()
    m.apply_transform(trimesh.geometry.align_vectors(normal, [0, 0, -1]))
    return m, area, [round(float(x), 4) for x in normal]


def to_bed(mesh):
    m = mesh.copy()
    lo, hi = m.bounds
    m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return m


def half(piece, axis, at, side):
    big = 1e5
    box = trimesh.creation.box(extents=(2 * big,) * 3)
    c = np.zeros(3)
    c[axis] = at + side * big
    box.apply_translation(c)
    return piece.intersection(box, engine="manifold")


def split_to_fit(mesh, bed, margin=5.0, pins=True, pin_d=4.0, pin_depth=6.0, clearance=0.15):
    from shapely.ops import unary_union
    limit = np.array(bed, float) - margin
    work, done, cuts, pin_list, skipped = [mesh.copy()], [], [], [], 0
    while work:
        piece = work.pop(0)
        ratio = piece.extents / limit
        if ratio.max() <= 1:
            done.append(piece)
            continue
        axis = int(np.argmax(ratio))
        at = float(piece.bounds.mean(axis=0)[axis])
        lo, hi = half(piece, axis, at, -1), half(piece, axis, at, +1)
        cuts.append({"axis": "xyz"[axis], "at": round(at, 3)})
        if pins:
            n = np.zeros(3)
            n[axis] = 1.0
            origin = piece.bounds.mean(axis=0).copy()
            origin[axis] = at
            section = piece.section(plane_origin=origin, plane_normal=n)
            placed = False
            if section is not None:
                planar, to_3d = section.to_2D()
                shape = unary_union(planar.polygons_full)
                c2 = shape.centroid
                if shape.contains(c2) and shape.boundary.distance(c2) >= pin_d:
                    center = (to_3d @ np.array([c2.x, c2.y, 0.0, 1.0]))[:3]
                    cyl = trimesh.creation.cylinder(radius=(pin_d + 2 * clearance) / 2, height=2 * pin_depth, sections=32)
                    cyl.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], n))
                    cyl.apply_translation(center)
                    lo, hi = lo.difference(cyl, engine="manifold"), hi.difference(cyl, engine="manifold")
                    pin_list.append({"axis": "xyz"[axis], "center": [round(float(v), 2) for v in center]})
                    placed = True
            if not placed:
                skipped += 1
        work += [lo, hi]
    return done, {"cuts": cuts, "pins": pin_list, "pins_skipped": skipped, "pin_diameter": pin_d, "pin_length": 2 * pin_depth}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("out")
    ap.add_argument("bed", nargs=3, type=float)
    ap.add_argument("--margin", type=float, default=5.0)
    ap.add_argument("--no-pins", action="store_true")
    a = ap.parse_args(argv)
    mesh = trimesh.load(a.stl, force="mesh")
    mesh.merge_vertices()
    if not mesh.is_watertight:
        # CAD exports leave hairline gaps where curved faces meet; welding at 0.01 mm closes them.
        welded = mesh.copy()
        welded.merge_vertices(digits_vertex=2)
        welded.update_faces(welded.nondegenerate_faces())
        welded.remove_unreferenced_vertices()
        if welded.is_watertight:
            mesh = welded
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_normals(mesh)
    flat, base_area, was_normal = lay_flat(mesh)
    flat = to_bed(flat)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("piece_*.stl"):
        old.unlink()
    if flat.is_watertight:
        pieces, split = split_to_fit(flat, a.bed, a.margin, pins=not a.no_pins)
    else:
        pieces, split = [flat], None
    names, sizes = [], []
    for i, piece in enumerate(pieces, 1):
        piece = to_bed(piece)
        name = f"piece_{i:02d}.stl"
        piece.export(out / name)
        names.append(name)
        sizes.append([round(float(x), 1) for x in piece.extents])
    report = {"pieces": names, "sizes": sizes, "base_area_mm2": round(base_area, 1), "base_was_normal": was_normal,
              "split": split, "fits": all(all(s <= b for s, b in zip(size, a.bed)) for size in sizes),
              "watertight": bool(flat.is_watertight), "bed": a.bed}
    (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
