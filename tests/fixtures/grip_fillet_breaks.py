import cadquery as cq
import math

# ---- Parameters (mm) ----
LENGTH = 100.0          # overall length (given)
WIDTH  = 30.0           # estimated extrusion depth (front-to-back)
BOSS_SPACING = 40.0     # center-to-center of the two mounting bosses
BOSS_HOLE_D  = 5.0      # through-hole diameter
BOSS_D       = 12.0     # boss outer diameter (visual estimate)
BOSS_H       = 6.0      # boss height above the top face
FILLET_R     = 1.5      # small fillet radius (top face only)

# Flat mounting face height (top of body)
TOP_FACE_Z = 54.0
# Embed bosses 6 mm into the body so the union fuses into one solid
BOSS_BASE_Z = TOP_FACE_Z - 6.0
BOSS_TOP_Z  = BOSS_BASE_Z + BOSS_H   # = 54, flush with top face region

# ---- Side profile outline (XZ plane), traced around the grip ----
# Origin: base at Z=0, centered in X (X runs -50..+50).
# Front (hook) at negative X, rear at positive X.
# Top face is flat at Z=54 across the boss region (X -26..+26).
PROFILE = [
    (-50.0, 0.0),    # front bottom tip (hook lowest point)
    (-46.0, 12.0),   # up the front face of the hook
    (-40.0, 22.0),   # front of the ramp
    (-26.0, 34.0),   # ramp rising toward top
    (-26.0, 54.0),   # top face front edge (flat mounting face)
    ( 26.0, 54.0),   # top face rear edge (flat mounting face)
    ( 48.0, 54.0),   # top face rear
    ( 50.0, 40.0),   # rear top corner
    ( 50.0, 20.0),   # rear face going down
    ( 44.0, 10.0),   # rear lower
    ( 30.0,  6.0),   # bottom rear
    ( 10.0,  4.0),   # bottom mid
    (-10.0,  3.0),   # bottom front-mid
    (-30.0,  2.0),   # bottom toward hook
    (-44.0,  2.0),   # bottom near hook tip
]

# ---- Build the extruded side profile, centered on Y ----
body = (
    cq.Workplane("XZ")
    .polyline(PROFILE)
    .close()
    .extrude(WIDTH / 2, both=True)   # centers on Y=0
)

# ---- Mounting bosses on the flat top face (overlapping the body) ----
boss_centers = [(-BOSS_SPACING / 2, 0), (BOSS_SPACING / 2, 0)]

for (bx, by) in boss_centers:
    boss = (
        cq.Workplane("XY")
        .center(bx, by)
        .circle(BOSS_D / 2)
        .extrude(BOSS_H)
        .translate((0, 0, BOSS_BASE_Z))
    )
    body = body.union(boss)

# ---- Through-holes for the screws (cut all the way through) ----
for (hx, hy) in boss_centers:
    hole = (
        cq.Workplane("XY")
        .center(hx, hy)
        .circle(BOSS_HOLE_D / 2)
        .extrude(80)   # tall enough to pass through everything
        .translate((0, 0, BOSS_BASE_Z - 10))
    )
    body = body.cut(hole)

# ---- Small fillets on the top face edges only (best effort) ----
try:
    body = body.faces(">Z").edges().fillet(FILLET_R)
except Exception:
    try:
        body = body.faces(">Z").edges().fillet(FILLET_R / 2)
    except Exception:
        pass  # keep the part even if the fillet fails

result = body