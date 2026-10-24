# name: Spacer / standoff
# about: Round or hex spacer with a through hole. Set HEX_FLATS to 0 for round.
import cadquery as cq
import math
HEIGHT = 10.0
OUTER_D = 8.0
HEX_FLATS = 0.0       # 0 = round; otherwise hex size across flats
HOLE_D = 3.4          # M3 clearance
if HEX_FLATS > 0:
    body = cq.Workplane("XY").polygon(6, HEX_FLATS / math.cos(math.pi / 6)).extrude(HEIGHT)
else:
    body = cq.Workplane("XY").circle(OUTER_D / 2).extrude(HEIGHT)
result = body.cut(cq.Workplane("XY").circle(HOLE_D / 2).extrude(HEIGHT + 2).translate((0, 0, -1)))
