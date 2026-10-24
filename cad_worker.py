"""Restricted CadQuery worker. Kept separate from overnight-ai and its sealed files.

The AST policy reduces accidental access; it is not an operating-system sandbox.
"""
import ast
import builtins
import json
import math
from pathlib import Path
import sys

SAFE_ATTRIBUTES = set('''Workplane Shape Solid Compound Vector Plane Location Sketch
makeBox makeSphere makeCylinder makeCone makeTorus makeCompound
box sphere cylinder cone circle ellipse rect polygon polyline lineTo moveTo move line hLine vLine hLineTo vLineTo threePointArc radiusArc tangentArcPoint spline close
extrude twistExtrude cutBlind cutThruAll hole cboreHole cskHole loft revolve sweep shell fillet chamfer
faces edges vertices solids wires workplane workplaneFromTagged center transformed translate rotate rotateAboutCenter mirror mirrorX mirrorY
union cut intersect split combine clean newObject add pushPoints rarray polarArray tag end val vals toPending
sin cos tan asin acos atan atan2 sqrt pi tau radians degrees floor ceil fabs hypot log exp pow
x y z Length Center BoundingBox Volume Area isValid
slot2D offset2D sagittaArc ellipseArc polarLine polarLineTo regularPolygon sketch placeSketch finalize push reset
each eachpoint copyWorkplane val first last item size fuse scale
xlen ylen zlen xmin xmax ymin ymax zmin zmax Faces Edges Solids Vertices Wires
append extend'''.split())
SAFE_BUILTINS = {k: getattr(builtins, k) for k in ("abs", "min", "max", "round", "int", "float", "len", "list", "tuple", "dict", "enumerate", "zip", "sum", "bool",
                                                    # exception names, so models can guard a fragile fillet with try/except
                                                    "Exception", "ValueError", "RuntimeError", "TypeError", "IndexError", "ZeroDivisionError")}


def bounded_range(*args):
    r = range(*args)
    if len(r) > 1000:
        raise ValueError("Use fewer than 1000 loop iterations")
    return r


def check_source(code):
    if len(code) > 100000:
        raise ValueError("CAD source is too long")
    tree = ast.parse(code)
    forbidden = (ast.While, ast.AsyncFunctionDef, ast.Await, ast.With, ast.AsyncWith, ast.ClassDef, ast.Raise, ast.Global, ast.Nonlocal, ast.Delete, ast.Yield, ast.YieldFrom)
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ValueError(f"Unsupported CAD statement: {type(node).__name__}")
        if isinstance(node, ast.Import):
            if any(a.name not in ("cadquery", "math") for a in node.names):
                raise ValueError("Only cadquery and math may be imported")
        if isinstance(node, ast.ImportFrom):
            raise ValueError("Use import cadquery as cq and import math")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("Private names are not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in SAFE_ATTRIBUTES:
            raise ValueError("Unsupported CAD attribute: " + node.attr)
        if isinstance(node, ast.FunctionDef) and node.decorator_list:
            raise ValueError("Decorators are not supported")
    return tree


def only_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in ("cadquery", "math") or fromlist or level:
        raise ValueError("Import blocked")
    return builtins.__import__(name)


def forgive_common_mistakes(cq):
    """Local models often write translate(x, y, z); CadQuery wants translate((x, y, z))."""
    def wrap(original):
        def translate(self, *args):
            if len(args) == 3 and all(isinstance(a, (int, float)) for a in args):
                args = (tuple(args),)
            return original(self, *args)
        return translate
    cq.Workplane.translate = wrap(cq.Workplane.translate)
    cq.Shape.translate = wrap(cq.Shape.translate)

    # A fillet or chamfer can return broken geometry without raising. Raise instead, so the model's
    # try/except fallback keeps the unfilleted part (and without one, the error says what to change).
    def checked(name, original):
        def edge_op(self, *args, **kwargs):
            out = original(self, *args, **kwargs)
            if any(isinstance(v, cq.Shape) and not v.isValid() for v in out.vals()):
                raise ValueError(f"{name} made broken geometry; use a smaller radius, fewer edges, or skip it")
            return out
        return edge_op
    for name in ("fillet", "chamfer"):
        setattr(cq.Workplane, name, checked(name, getattr(cq.Workplane, name)))


def as_shape(cq, value, name):
    if isinstance(value, cq.Workplane):
        shapes = [s for s in value.vals() if isinstance(s, cq.Shape)]
        if not shapes:
            raise ValueError(f"{name} contains no solid geometry")
        value = cq.Compound.makeCompound(shapes) if len(shapes) > 1 else shapes[0]
    return value


def build_tools(cq, env, destination):
    """Mesh edit mode: export the 'cut' and 'add' solids for a mesh boolean instead of a result."""
    made = {}
    for name in ("cut", "add"):
        if env.get(name) is None:
            continue
        shape = as_shape(cq, env[name], name)
        if not isinstance(shape, cq.Shape) or not shape.isValid() or not shape.Solids() or shape.Volume() <= 0:
            raise ValueError(f"{name} must be valid solid geometry")
        destination.mkdir(parents=True, exist_ok=True)
        cq.exporters.export(shape, str(destination / f"tool_{name}.stl"), tolerance=0.05, angularTolerance=0.15)
        b = shape.BoundingBox()
        made[name] = [round(v, 2) for v in (b.xmin, b.xmax, b.ymin, b.ymax, b.zmin, b.zmax)]
    if not made:
        raise ValueError("Define cut = ... (to remove material) and/or add = ... (to add material); don't assign result")
    (destination / "tools.json").write_text(json.dumps(made), encoding="utf-8")
    print(json.dumps(made))


def build(source, destination, tools=False):
    import cadquery as cq
    forgive_common_mistakes(cq)
    code = source.read_text(encoding="utf-8")
    tree = check_source(code)
    safe = {**SAFE_BUILTINS, "range": bounded_range, "__import__": only_import}
    env = {"__builtins__": safe, "cq": cq, "math": math}
    exec(compile(tree, "model.py", "exec"), env, env)
    if tools:
        return build_tools(cq, env, destination)
    result = as_shape(cq, env.get("result"), "result")
    if not isinstance(result, cq.Shape) or not result.isValid():
        raise ValueError("result must be valid CadQuery solid geometry")
    if not result.Solids() or result.Volume() <= 0:
        raise ValueError("Result is not a solid with positive volume")
    bb = result.BoundingBox()
    dims = [bb.xlen, bb.ylen, bb.zlen]
    if not all(math.isfinite(x) and 0.001 < x <= 5000 for x in dims):
        raise ValueError("Bounding dimensions must be between 0.001 and 5000 mm")
    destination.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(result, str(destination / "model.step"))
    cq.exporters.export(result, str(destination / "model.stl"), tolerance=0.15, angularTolerance=0.2)
    if (destination / "model.step").stat().st_size < 100 or (destination / "model.stl").stat().st_size < 100:
        raise ValueError("CAD export was empty")
    boxes = []
    for s in result.Solids()[:10]:
        b = s.BoundingBox()
        boxes.append([round(v, 2) for v in (b.xmin, b.xmax, b.ymin, b.ymax, b.zmin, b.zmax)])
    meta = {"dimensions": [round(x, 3) for x in dims], "volume": round(result.Volume(), 3), "area": round(result.Area(), 3),
            "solids": len(result.Solids()), "valid": True, "solid_boxes": boxes}
    # Pure stdlib preview renderer; no Pillow or extra CAD packages needed on Windows.
    sys.path.insert(0, str(Path(__file__).parent))
    from preview_render import render_views
    render_views(destination / "model.stl", destination)
    (destination / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    print(json.dumps(meta))


def describe(exc, source):
    """Error text for the model: the failing line of model.py, not just the message."""
    import traceback
    text = f"{type(exc).__name__}: {exc}"
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "model.py"]
    if frames:
        line = frames[-1].lineno
        try:
            code = source.read_text(encoding="utf-8").splitlines()[line - 1].strip()
        except (OSError, IndexError):
            code = ""
        text = f"model.py line {line}: {code}\n{text}"
    elif isinstance(exc, SyntaxError) and exc.lineno:
        text = f"model.py line {exc.lineno}: {(exc.text or '').strip()}\n{text}"
    return text


if __name__ == "__main__":
    try:
        build(Path(sys.argv[1]), Path(sys.argv[2]), tools="--tools" in sys.argv[3:])
    except Exception as exc:
        print(describe(exc, Path(sys.argv[1])), file=sys.stderr)
        raise SystemExit(1)
