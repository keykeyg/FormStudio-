"""Measure a mesh so it can be rebuilt as CAD, and check a CAD rebuild against it.
Runs in the CadQuery Python (trimesh, numpy, scipy, shapely).

  measure <mesh.stl> <out.json>                 sections, bands, holes and a working baseline CadQuery program
  compare <cad.stl> <mesh.stl> <out.json> [tol] how far the CAD surface is from the mesh, and where

The baseline is a "2.5D" rebuild: the mesh is sliced along its most consistent direction, slices with the same outline
are grouped into bands, and each band becomes an extruded outline with its openings cut through. Round openings become
exact circles. Holes found across the other directions are cut as cylinders. Everything is in the mesh's own coordinates.
"""
import json
import sys
from pathlib import Path

import numpy as np
import trimesh

AXES = "XYZ"
PLANE = {0: ("YZ", (1, 2)), 1: ("XZ", (0, 2)), 2: ("XY", (0, 1))}  # extrusion axis -> CadQuery plane, (u, v) axes


# ---- sections ----
def loops_at(mesh, axis, value):
    """Closed outlines where the plane axis=value cuts the mesh, as (n, 2) arrays in that plane's (u, v) axes."""
    normal = np.zeros(3)
    normal[axis] = 1.0
    origin = np.zeros(3)
    origin[axis] = value
    try:
        section = mesh.section(plane_origin=origin, plane_normal=normal)
    except Exception:
        return []
    if section is None:
        return []
    u, v = PLANE[axis][1]
    out = []
    for line in section.discrete:
        pts = np.asarray(line)[:, (u, v)]
        if len(pts) >= 4 and np.linalg.norm(pts[0] - pts[-1]) < 1e-6:
            pts = pts[:-1]
        if len(pts) >= 3:
            out.append(pts)
    return out


def polygons(loops):
    """Shapely geometry of the region inside an even number of outlines (so openings are holes)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    shapes = []
    for pts in loops:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.area > 1e-6:
            shapes.append(poly)
    region = None
    for poly in sorted(shapes, key=lambda s: -s.area):
        region = poly if region is None else region.symmetric_difference(poly)
    return unary_union(region) if region is not None else None


def iou(a, b):
    if a is None or b is None or a.is_empty or b.is_empty:
        return 1.0 if (a is None or a.is_empty) and (b is None or b.is_empty) else 0.0
    union = a.union(b).area
    return a.intersection(b).area / union if union > 0 else 1.0


def circle_fit(pts):
    """Center, diameter and roundness error (as a fraction of the radius) of a closed outline."""
    x, y = pts[:, 0], pts[:, 1]
    a = np.column_stack([x, y, np.ones_like(x)])
    sol, *_ = np.linalg.lstsq(a, -(x * x + y * y), rcond=None)
    cx, cy = -sol[0] / 2, -sol[1] / 2
    r2 = cx * cx + cy * cy - sol[2]
    if r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    err = float(np.sqrt(np.mean((np.hypot(x - cx, y - cy) - r) ** 2))) / r
    angles = np.sort(np.arctan2(y - cy, x - cx))
    gap = np.max(np.diff(np.concatenate([angles, angles[:1] + 2 * np.pi])))
    if gap > np.pi / 3:  # an arc, not a full circle
        return None
    return float(cx), float(cy), 2 * r, err


def parts_of(region, tol):
    """Outer outlines (with their openings) of a region, simplified to tol, smallest specks dropped."""
    from shapely.geometry import MultiPolygon, Polygon
    if region is None or region.is_empty:
        return []
    geoms = list(region.geoms) if isinstance(region, MultiPolygon) else [region] if isinstance(region, Polygon) else [
        g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]
    total = sum(g.area for g in geoms)
    out = []
    for g in geoms:
        if g.area < max(1.0, 0.002 * total):
            continue
        out.append(g)
    return out


def ring(coords, tol):
    """A simplified, valid outline as rounded points (no repeated closing point)."""
    from shapely.geometry import Polygon
    poly = Polygon(coords)
    for t in (tol, tol / 2, tol / 4, 0):
        simple = poly.simplify(t, preserve_topology=True) if t else poly
        if simple.is_valid and not simple.is_empty and simple.geom_type == "Polygon":
            pts = [(round(x, 2), round(y, 2)) for x, y in list(simple.exterior.coords)[:-1]]
            deduped = [p for i, p in enumerate(pts) if p != pts[i - 1]]
            if len(deduped) >= 3 and Polygon(deduped).is_valid:
                return deduped
    return None


def describe_band(polys, tol):
    """Outlines, openings and round holes of one band's section."""
    outlines, openings, holes, bosses = [], [], [], []
    for poly in polys:
        ext = np.asarray(poly.exterior.coords)[:-1]
        fit = circle_fit(ext) if len(ext) >= 8 else None
        if fit and fit[3] < 0.03:
            bosses.append(tuple(round(v, 2) for v in fit[:3]))
        else:
            pts = ring(ext, tol)
            if pts:
                outlines.append(pts)
        for interior in poly.interiors:
            pts_i = np.asarray(interior.coords)[:-1]
            fit = circle_fit(pts_i) if len(pts_i) >= 8 else None
            if fit and fit[3] < 0.03:
                holes.append(tuple(round(v, 2) for v in fit[:3]))
            else:
                simple = ring(pts_i, tol)
                if simple and abs(_area(simple)) > 0.5:
                    openings.append(simple)
    return outlines, openings, holes, bosses


def _area(pts):
    p = np.asarray(pts)
    return 0.5 * float(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1)))


def bands_along(mesh, axis, n=24, threshold=0.92, limit=8):
    """Group slices along one axis into bands whose outline stays the same; returns (bands, mean IoU)."""
    lo, hi = float(mesh.bounds[0][axis]), float(mesh.bounds[1][axis])
    step = (hi - lo) / n
    pos = [lo + (i + 0.5) * step for i in range(n)]
    regions = [polygons(loops_at(mesh, axis, t)) for t in pos]
    sizes = [r.area if r is not None else 0.0 for r in regions]
    biggest = max(sizes) if sizes else 0
    keep = [i for i in range(n) if sizes[i] > 0.01 * biggest]
    if not keep:
        return [], 0.0, 0
    def group(thr):
        groups = [[keep[0]]]
        for i in keep[1:]:
            if i == groups[-1][-1] + 1 and iou(regions[i], regions[groups[-1][0]]) >= thr:
                groups[-1].append(i)
            else:
                groups.append([i])
        return groups

    strict = len(group(threshold))  # how many distinct outlines this direction really has
    for thr in (threshold, 0.85, 0.75, 0.6, 0.0):
        groups = group(thr)
        if len(groups) <= limit:
            break

    def bisect(i, j, ref_a, ref_b):
        a, b = pos[i], pos[j]
        for _ in range(4):
            mid = (a + b) / 2
            r = polygons(loops_at(mesh, axis, mid))
            if iou(r, ref_a) >= iou(r, ref_b):
                a = mid
            else:
                b = mid
        return (a + b) / 2

    def edge(inside, outside):
        """Where material starts or stops between a slice with material and one without."""
        a, b = inside, outside
        for _ in range(5):
            mid = (a + b) / 2
            r = polygons(loops_at(mesh, axis, mid))
            if r is not None and r.area > 0.01 * biggest:
                a = mid
            else:
                b = mid
        return (a + b) / 2

    bands = []
    for k, g in enumerate(groups):
        # the slice that agrees best with the rest of its band
        best = max(g, key=lambda i: sum(iou(regions[i], regions[j]) for j in g))
        start = lo if k == 0 else bands[-1]["end"]
        if k + 1 < len(groups):
            nxt = groups[k + 1]
            end = bisect(g[-1], nxt[0], regions[best], regions[nxt[0]]) if nxt[0] == g[-1] + 1 else edge(pos[g[-1]], pos[g[-1]] + step)
        else:
            end = hi
        if k > 0 and groups[k - 1][-1] + 1 != g[0]:
            start = edge(pos[g[0]], pos[g[0]] - step)  # a gap with no material between bands
        bands.append({"start": round(start, 2), "end": round(end, 2), "region": regions[best], "at": pos[best]})
    agreement = float(np.mean([iou(regions[i], regions[g[0]]) for g in groups for i in g]))
    return bands, agreement, strict


def cross_holes(mesh, axis, n=16):
    """Round openings that run along an axis: grouped by center and diameter across slices."""
    lo, hi = float(mesh.bounds[0][axis]), float(mesh.bounds[1][axis])
    step = (hi - lo) / n
    found = []
    for i in range(n):
        t = lo + (i + 0.5) * step
        region = polygons(loops_at(mesh, axis, t))
        for poly in parts_of(region, 0):
            for interior in poly.interiors:
                pts = np.asarray(interior.coords)[:-1]
                fit = circle_fit(pts) if len(pts) >= 8 else None
                if fit and fit[3] < 0.03 and fit[2] >= 1.0:
                    found.append((t, fit[0], fit[1], fit[2]))
    holes = []
    for t, cu, cv, d in found:
        for h in holes:
            if abs(h["u"] - cu) < max(0.5, 0.05 * d) and abs(h["v"] - cv) < max(0.5, 0.05 * d) and abs(h["d"] - d) < max(0.3, 0.05 * d):
                h["ts"].append(t)
                h["ds"].append(d)
                break
        else:
            holes.append({"u": cu, "v": cv, "d": d, "ts": [t], "ds": [d]})
    out = []
    for h in holes:
        if len(h["ts"]) < 2 and hi - lo > 4 * step:
            continue
        start = max(lo, min(h["ts"]) - step / 2)
        end = min(hi, max(h["ts"]) + step / 2)
        if min(h["ts"]) - step < lo:
            start = lo - 1.0  # runs out of the part: cut a little past the surface
        if max(h["ts"]) + step > hi:
            end = hi + 1.0
        out.append({"axis": AXES[axis], "u": round(h["u"], 2), "v": round(h["v"], 2), "d": round(float(np.median(h["ds"])), 2),
                    "start": round(start, 2), "end": round(end, 2)})
    return out


# ---- the baseline program ----
def program(axis, bands, holes, tol, size):
    plane, (u, v) = PLANE[axis]
    U, V, A = AXES[u], AXES[v], AXES[axis]
    lines = ["import cadquery as cq", "",
             f"# Rebuilt from measurements of the mesh by Form Studio: {len(bands)} band(s) stacked along {A}.",
             f"# Each band is an outline in ({U}, {V}) filled from {A}0 to {A}1, with its openings and round holes cut through.",
             f"# Overall size of the mesh: X {size[0]:g} x Y {size[1]:g} x Z {size[2]:g} mm.", ""]
    if axis == 1:
        lines += ["def slab(points, a0, a1):",
                  f"    return cq.Workplane(\"XZ\", origin=(0, a1, 0)).polyline(points).close().extrude(a1 - a0)", "",
                  "def disc(cu, cv, d, a0, a1):",
                  f"    return cq.Workplane(\"XZ\", origin=(0, a1, 0)).center(cu, cv).circle(d / 2).extrude(a1 - a0)", ""]
    else:
        origin = "(a0, 0, 0)" if axis == 0 else "(0, 0, a0)"
        lines += ["def slab(points, a0, a1):",
                  f"    return cq.Workplane(\"{plane}\", origin={origin}).polyline(points).close().extrude(a1 - a0)", "",
                  "def disc(cu, cv, d, a0, a1):",
                  f"    return cq.Workplane(\"{plane}\", origin={origin}).center(cu, cv).circle(d / 2).extrude(a1 - a0)", ""]
    lines += ["def band(a0, a1, outlines, openings=(), holes=(), bosses=()):",
              "    body = None",
              "    for pts in outlines:",
              "        piece = slab(pts, a0, a1)",
              "        body = piece if body is None else body.union(piece)",
              "    for cu, cv, d in bosses:",
              "        piece = disc(cu, cv, d, a0, a1)",
              "        body = piece if body is None else body.union(piece)",
              "    for pts in openings:",
              "        body = body.cut(slab(pts, a0 - 0.01, a1 + 0.01))",
              "    for cu, cv, d in holes:",
              "        body = body.cut(disc(cu, cv, d, a0 - 0.01, a1 + 0.01))",
              "    return body", ""]
    names = []
    for i, b in enumerate(bands, 1):
        outlines, openings, holes_b, bosses = describe_band(parts_of(b["region"], tol), tol)
        if not outlines and not bosses:
            continue
        name = f"BAND{i}"
        names.append(name)
        lines.append(f"{name}_{A} = ({b['start']:g}, {b['end']:g})")
        lines.append(f"{name}_OUTLINES = {json.dumps([[list(p) for p in o] for o in outlines], separators=(',', ':'))}")
        if openings:
            lines.append(f"{name}_OPENINGS = {json.dumps([[list(p) for p in o] for o in openings], separators=(',', ':'))}")
        if holes_b:
            lines.append(f"{name}_HOLES = {json.dumps([list(h) for h in holes_b])}  # (center {U}, center {V}, diameter)")
        if bosses:
            lines.append(f"{name}_BOSSES = {json.dumps([list(h) for h in bosses])}  # round pegs: (center {U}, center {V}, diameter)")
        lines.append("")
    if not names:
        raise ValueError("No usable cross-section was found in this mesh")
    lines.append("result = None")
    for name in names:
        args = [f"*{name}_{A}", f"{name}_OUTLINES"]
        for key in ("OPENINGS", "HOLES", "BOSSES"):
            if any(line.startswith(f"{name}_{key} =") for line in lines):
                args.append(f"{key.lower()}={name}_{key}")
        lines.append(f"part = band({', '.join(args)})")
        lines.append("result = part if result is None else result.union(part)")
    cross = [h for h in holes if h["axis"] != A]
    if cross:
        lines += ["", "# Round holes running across the bands: (axis, center, center, diameter, from, to)",
                  f"CROSS_HOLES = {json.dumps([[h['axis'], h['u'], h['v'], h['d'], h['start'], h['end']] for h in cross])}",
                  "for axis, cu, cv, d, a0, a1 in CROSS_HOLES:",
                  "    plane = {\"X\": \"YZ\", \"Y\": \"XZ\", \"Z\": \"XY\"}[axis]",
                  "    origin = {\"X\": (a0, 0, 0), \"Y\": (0, a1, 0), \"Z\": (0, 0, a0)}[axis]",
                  "    result = result.cut(cq.Workplane(plane, origin=origin).center(cu, cv).circle(d / 2).extrude(a1 - a0))"]
    return "\n".join(lines) + "\n"


def measure(path, out):
    mesh = trimesh.load(path, force="mesh")
    mesh.merge_vertices()
    size = [round(float(x), 2) for x in mesh.extents]
    lo, hi = mesh.bounds
    tol = max(0.15, 0.002 * float(max(mesh.extents)))  # outline simplification
    choices = []
    for axis in range(3):
        bands, agreement, strict = bands_along(mesh, axis)
        if bands:
            choices.append((strict, -agreement, float(mesh.extents[axis]), axis, bands, agreement))
    if not choices:
        raise ValueError("The mesh has no closed cross-sections; repair it (Polish) first")
    choices.sort(key=lambda c: (c[0], c[1], c[2]))
    _, _, _, axis, bands, agreement = choices[0]
    holes = []
    for k in range(3):
        holes += cross_holes(mesh, k)
    code = program(axis, bands, holes, tol, size)
    plane, (u, v) = PLANE[axis]
    text = [f"MESH MEASUREMENTS (mm, the mesh's own coordinates: X and Y centered, base at Z=0)",
            f"Size: X {size[0]:g} x Y {size[1]:g} x Z {size[2]:g}. Bounding box X {lo[0]:.2f}..{hi[0]:.2f}, Y {lo[1]:.2f}..{hi[1]:.2f}, Z {lo[2]:.2f}..{hi[2]:.2f}.",
            f"Volume {float(mesh.volume):.0f} mm^3, surface {float(mesh.area):.0f} mm^2." if mesh.is_watertight else "The mesh is not watertight.",
            f"Built along {AXES[axis]}: the outline in ({AXES[u]}, {AXES[v]}) stays the same through {len(bands)} band(s) (slices agree {agreement * 100:.0f}%)."]
    for i, b in enumerate(bands, 1):
        reg = b["region"]
        if reg is None or reg.is_empty:
            continue
        x0, y0, x1, y1 = reg.bounds
        n_open = sum(len(p.interiors) for p in parts_of(reg, tol))
        text.append(f"  band {i}: {AXES[axis]} {b['start']:g}..{b['end']:g}, outline {AXES[u]} {x0:.1f}..{x1:.1f}, {AXES[v]} {y0:.1f}..{y1:.1f}, "
                    f"{len(parts_of(reg, tol))} piece(s), {n_open} opening(s)")
    for h in holes:
        a = AXES.index(h["axis"])
        _, (hu, hv) = PLANE[a]
        text.append(f"Round hole {h['d']:g} mm along {h['axis']} at {AXES[hu]} {h['u']:g}, {AXES[hv]} {h['v']:g}, from {h['axis']} {h['start']:g} to {h['end']:g}")
    result = {"size": size, "bounds": [[round(float(x), 2) for x in lo], [round(float(x), 2) for x in hi]], "axis": AXES[axis],
              "bands": [{"start": b["start"], "end": b["end"]} for b in bands], "holes": holes, "summary": "\n".join(text), "program": code}
    Path(out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("size", "axis", "bands", "holes")}))
    return result


# ---- deviation ----
def compare(cad_path, mesh_path, out, tol=0.5):
    from scipy import ndimage
    from scipy.spatial import cKDTree
    tol = float(tol)
    cad = trimesh.load(cad_path, force="mesh")
    mesh = trimesh.load(mesh_path, force="mesh")

    def dense(m, count):
        pts, face = trimesh.sample.sample_surface(m, count, seed=3)
        return pts, m.face_normals[face]

    def signed(points, target, normals, spacing):
        tree = cKDTree(target)
        dist, idx = tree.query(points)
        along = np.einsum("ij,ij->i", points - target[idx], normals[idx])
        # close to the surface use the point-to-plane distance; farther out the straight distance
        size = np.where(dist < 2 * spacing, np.abs(along), dist)
        return np.sign(along + 1e-12) * size

    area = max(float(mesh.area), 1e-6)
    m_pts, _ = dense(mesh, 60_000)
    c_pts, _ = dense(cad, 60_000)
    m_ref, m_nrm = dense(mesh, 300_000)
    c_ref, c_nrm = dense(cad, 300_000)
    spacing_c = np.sqrt(max(float(cad.area), 1e-6) / 300_000)
    spacing_m = np.sqrt(area / 300_000)
    missing = signed(m_pts, c_ref, c_nrm, spacing_c)  # + : the mesh sticks out of the CAD here (CAD lacks material)
    extra = signed(c_pts, m_ref, m_nrm, spacing_m)    # + : the CAD sticks out of the mesh here (extra material)
    both = np.concatenate([np.abs(missing), np.abs(extra)])
    stats = {"mean": round(float(np.mean(both)), 3), "p95": round(float(np.percentile(both, 95)), 3), "max": round(float(np.max(both)), 2),
             "within": round(float(np.mean(both <= tol)), 4), "tol": tol}
    cell = max(2 * tol, float(max(mesh.extents)) / 40)
    lo = np.minimum(mesh.bounds[0], cad.bounds[0]) - cell

    def regions(points, values, kind, per_point):
        bad = values > tol
        if not bad.any():
            return []
        idx = np.floor((points[bad] - lo) / cell).astype(int)
        grid = np.zeros(idx.max(axis=0) + 1, dtype=bool)
        grid[tuple(idx.T)] = True
        labels, count = ndimage.label(grid, structure=np.ones((3, 3, 3)))
        lab = labels[tuple(idx.T)]
        out = []
        for k in range(1, count + 1):
            sel = lab == k
            pts, vals = points[bad][sel], values[bad][sel]
            area_k = float(sel.sum() * per_point)
            if area_k < max(2.0, 0.001 * area):
                continue
            b0, b1 = pts.min(axis=0), pts.max(axis=0)
            out.append({"kind": kind, "area": round(area_k, 1), "worst": round(float(vals.max()), 2), "typical": round(float(np.median(vals)), 2),
                        "box": [round(float(x), 1) for x in (b0[0], b1[0], b0[1], b1[1], b0[2], b1[2])]})
        return out

    found = regions(m_pts, missing, "missing", area / len(m_pts)) + regions(c_pts, extra, "extra", float(cad.area) / len(c_pts))
    found.sort(key=lambda r: -r["area"] * r["typical"])
    cb, mb = cad.bounds, mesh.bounds
    lines = [f"DEVIATION (CAD vs mesh): {stats['within'] * 100:.0f}% of the surface within ±{tol:g} mm; mean {stats['mean']:g} mm, "
             f"95% within {stats['p95']:g} mm, worst {stats['max']:g} mm."]
    for k, name in enumerate("XYZ"):
        d0, d1 = cb[0][k] - mb[0][k], cb[1][k] - mb[1][k]
        if max(abs(d0), abs(d1)) > tol:
            lines.append(f"{name} extent: CAD {cb[0][k]:.2f}..{cb[1][k]:.2f}, mesh {mb[0][k]:.2f}..{mb[1][k]:.2f}.")
    for r in found[:8]:
        b = r["box"]
        where = f"X {b[0]:g}..{b[1]:g}, Y {b[2]:g}..{b[3]:g}, Z {b[4]:g}..{b[5]:g}"
        if r["kind"] == "missing":
            lines.append(f"- MISSING material at {where}: the mesh has material the CAD lacks (about {r['area']:g} mm^2 of surface, typically {r['typical']:g} mm, up to {r['worst']:g} mm).")
        else:
            lines.append(f"- EXTRA material at {where}: the CAD has material the mesh doesn't (about {r['area']:g} mm^2 of surface, typically {r['typical']:g} mm, up to {r['worst']:g} mm).")
    if not found:
        lines.append("No areas are off by more than the tolerance.")
    result = {**stats, "regions": found[:12], "report": "\n".join(lines)}
    Path(out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("mean", "p95", "max", "within", "tol")}))
    return result


def main(argv):
    if argv and argv[0] == "measure":
        measure(argv[1], argv[2])
    elif argv and argv[0] == "compare":
        compare(argv[1], argv[2], argv[3], *(argv[4:5] or []))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
