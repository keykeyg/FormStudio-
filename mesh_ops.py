"""Mesh operations for Form Studio. Runs in the CadQuery Python (trimesh + manifold3d + numpy).

  boolean  <base.stl> <tools dir> <out>          apply tool_cut.stl / tool_add.stl from the tools dir (CAD edits on meshes)
  scale    <base.stl> <out> <sx> <sy> <sz>       scale about the centre, base stays at Z=0
  stretch  <base.stl> <out> <axis> <at> <by> [band]   lengthen (+) or shorten (-) the part across a plane, inside a band
  polish   <base.stl> <out> [light|medium|strong] [source]   smooth away stair-steps and bumps; same size and place.
           source = TRELLIS.2's raw output: rebuild from its real surface first
  import   <file> <out>                           STL / OBJ / PLY / GLB / 3MF -> parts in out/parts, assembled model.stl
  assemble <out>                                  rebuild out/model.stl from out/parts.json

Every command writes model.stl, metadata.json and (for meshes up to 200k triangles) view-*.png into <out>.
The legacy form `mesh_ops.py <base.stl> <tools dir> <out>` still means boolean.
"""
import json
import math
import os
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


REBUILD_VOXELS = int(os.environ.get("FORM_STUDIO_REBUILD_VOXELS", "1000"))  # along the longest side (tests use fewer)
SMOOTH = {  # windowed-sinc passes and pass band: lower pass band = smoother
    "light": (30, 0.05),
    "medium": (50, 0.02),
    "strong": (80, 0.008),
}
SMOOTH_ON_SOURCE = {"light": None, "medium": (20, 0.1), "strong": (40, 0.04)}  # TRELLIS.2's own surface is already smooth


def rebuild(mesh, voxels=400, sigma=1.0, passes=20, target_faces=200_000, snap_to=None):
    """A closed, smooth surface from any mesh: voxelize and fill, blur the inside/outside field, contour it halfway
    (so the surface falls between voxels instead of on their faces), then a windowed-sinc smoothing that doesn't shrink.
    Gaps under one voxel close. With sigma=0 and few passes this is the plain repair rebuild."""
    import vtk
    from vtk.util import numpy_support
    pitch = max(0.08, float(max(mesh.extents)) / voxels)
    while np.prod(np.ceil(mesh.extents / pitch) + 10) > 6e7:  # keep the grid under about 60 million voxels
        pitch *= 1.15
    grid = mesh.voxelized(pitch).fill()
    pad = 4
    dense = np.pad(grid.matrix.astype(np.float32), pad)
    if sigma > 0:
        from scipy import ndimage
        dense = ndimage.gaussian_filter(dense, sigma)
    image = vtk.vtkImageData()
    image.SetDimensions(*dense.shape)
    image.SetSpacing(pitch, pitch, pitch)
    image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(dense.ravel(order="F"), deep=True, array_type=vtk.VTK_FLOAT))
    surface = vtk.vtkFlyingEdges3D()
    surface.SetInputData(image)
    surface.SetValue(0, 0.5)
    smooth = vtk.vtkWindowedSincPolyDataFilter()
    smooth.SetInputConnection(surface.GetOutputPort())
    smooth.SetNumberOfIterations(passes)
    smooth.SetPassBand(0.05 if sigma > 0 else 0.1)
    smooth.NonManifoldSmoothingOn()
    smooth.NormalizeCoordinatesOn()
    smooth.Update()
    poly = smooth.GetOutput()
    count = poly.GetNumberOfCells()
    if count > target_faces:
        decimate = vtk.vtkQuadricDecimation()
        decimate.SetInputConnection(smooth.GetOutputPort())
        decimate.SetTargetReduction(1 - target_faces / count)
        decimate.VolumePreservationOn()
        decimate.Update()
        poly = decimate.GetOutput()

    def to_mesh(poly):
        verts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData()).astype(float)
        faces = numpy_support.vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
        # voxel (i, j, k) sits at transform @ (i, j, k); the pad shifted it
        m = trimesh.Trimesh(verts + np.asarray(grid.transform)[:3, 3] - pad * pitch, faces, process=True)
        trimesh.repair.fix_normals(m)
        return m

    fixed = to_mesh(poly)
    if not fixed.is_watertight and poly is not smooth.GetOutput():  # decimation can open a seam: keep the full-detail surface
        fixed = to_mesh(smooth.GetOutput())
    if snap_to is not None:
        snap(fixed, snap_to, reach=2.0 * pitch)
    return fixed, pitch


def snap(mesh, source, reach):
    """Move each vertex onto the source surface (the tangent plane of the nearest point on it) when the source is
    within reach and faces the same way. A voxel rebuild then keeps the source's own detail instead of voxel steps;
    only real gaps, where there's no source surface nearby, keep the rebuilt surface."""
    from scipy.spatial import cKDTree
    count = int(min(3_000_000, max(200_000, 40 * len(mesh.vertices))))
    pts, face = trimesh.sample.sample_surface(source, count, seed=1)
    nrm = source.face_normals[face]
    dist, idx = cKDTree(pts).query(mesh.vertices, distance_upper_bound=reach)
    ok = np.isfinite(dist)
    ok[ok] = np.abs(np.einsum("ij,ij->i", mesh.vertex_normals[ok], nrm[idx[ok]])) > 0.6  # source normals may point either way
    v = mesh.vertices.copy()
    off = np.einsum("ij,ij->i", v[ok] - pts[idx[ok]], nrm[idx[ok]])
    v[ok] -= off[:, None] * nrm[idx[ok]]
    mesh.vertices = v
    return float(ok.mean())


def smooth_surface(mesh, passes, band):
    """Windowed-sinc smoothing: removes stair-steps and bumps without shrinking the part. Keeps the triangles' layout."""
    import vtk
    from vtk.util import numpy_support
    poly = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    points.SetData(numpy_support.numpy_to_vtk(np.ascontiguousarray(mesh.vertices, dtype=np.float64), deep=True))
    poly.SetPoints(points)
    cells = vtk.vtkCellArray()
    cells.SetData(numpy_support.numpy_to_vtk(np.arange(0, 3 * len(mesh.faces) + 1, 3, dtype=np.int64), deep=True),
                  numpy_support.numpy_to_vtk(np.ascontiguousarray(mesh.faces.ravel(), dtype=np.int64), deep=True))
    poly.SetPolys(cells)
    smooth = vtk.vtkWindowedSincPolyDataFilter()
    smooth.SetInputData(poly)
    smooth.SetNumberOfIterations(passes)
    smooth.SetPassBand(band)
    smooth.NonManifoldSmoothingOn()
    smooth.NormalizeCoordinatesOn()
    smooth.BoundarySmoothingOff()
    smooth.FeatureEdgeSmoothingOff()
    smooth.Update()
    verts = numpy_support.vtk_to_numpy(smooth.GetOutput().GetPoints().GetData()).astype(float)
    return trimesh.Trimesh(verts, mesh.faces.copy(), process=False)


def fit_to(mesh, lo, hi):
    """Scale and move a mesh so its bounding box is exactly lo..hi."""
    nlo, nhi = mesh.bounds
    mesh.apply_translation(-(nlo + nhi) / 2)
    mesh.apply_transform(np.diag(list((hi - lo) / np.maximum(nhi - nlo, 1e-9)) + [1.0]))
    mesh.apply_translation((lo + hi) / 2)
    return mesh


def source_for(path, base):
    """TRELLIS.2's original output, turned and fitted onto this version; None if it doesn't line up with it."""
    sys.path.insert(0, str(Path(__file__).parent))
    from mesh_worker import repair
    from scipy.spatial import cKDTree
    path = Path(path)
    src = trimesh.load(path, force="mesh")
    if path.suffix.lower() in (".glb", ".gltf"):
        src.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # glTF is Y-up
    src, _ = repair(src)
    lo, hi = base.bounds
    fit_to(src, lo, hi)
    pts, _ = trimesh.sample.sample_surface(src, 400_000, seed=2)
    probe = base.vertices[:: max(1, len(base.vertices) // 50_000)]
    dist, _ = cKDTree(pts).query(probe)
    return src if np.mean(dist < 0.015 * float(max(base.extents))) >= 0.9 else None


def polish(base, out, strength="medium", source=None):
    """Smooth away stair-steps and bumps while keeping the overall size and position. With TRELLIS.2's original
    output as the source, the surface is first rebuilt from it (finely, snapped to its real surface)."""
    strength = str(strength).lower() if str(strength).lower() in SMOOTH else "medium"
    mesh = load(base)
    lo, hi = mesh.bounds.copy()
    src = source_for(source, mesh) if source else None
    result = mesh
    if src is not None:
        rebuilt, _ = rebuild(src, voxels=REBUILD_VOXELS, sigma=0.6, passes=20, snap_to=src)
        if rebuilt.is_watertight and rebuilt.volume > 0:
            result = rebuilt
        else:
            src = None
    setting = SMOOTH_ON_SOURCE[strength] if src is not None else SMOOTH[strength]
    smoothed = setting is not None  # light on a rebuilt surface = TRELLIS.2's own surface
    if smoothed:
        if len(result.faces) < 50_000:  # big flat triangles can't bend smoothly: split them first
            v, f = trimesh.remesh.subdivide_to_size(result.vertices, result.faces, max_edge=float(max(result.extents)) / 120, max_iter=8)
            result = trimesh.Trimesh(v, f, process=True)
        result = smooth_surface(result, *setting)
    if not result.is_watertight or result.volume <= 0:
        raise ValueError("Polishing didn't give a closed solid; try a lighter setting")
    fit_to(result, lo, hi)
    change = abs(result.volume / mesh.volume - 1) * 100 if mesh.is_watertight and mesh.volume > 0 else None
    return finish(result, out, {"polished": {"strength": strength, "source": src is not None, "smoothed": smoothed,
                                             "volume_change_pct": round(float(change), 1) if change is not None else None}})


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
    cmd, args = (argv[0], argv[1:]) if argv and argv[0] in ("boolean", "scale", "stretch", "polish", "import", "assemble") else ("boolean", argv)
    {"boolean": boolean, "scale": scale, "stretch": stretch, "polish": polish, "import": import_model, "assemble": assemble}[cmd](*args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
