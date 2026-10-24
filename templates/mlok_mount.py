# name: M-LOK accessory base
# about: Flat base with two counterbored screw holes on the 40 mm M-LOK slot pitch (32 mm slots, 8 mm apart). Build your accessory on top.
import cadquery as cq
LENGTH = 60.0
WIDTH = 20.0
THICKNESS = 6.0
SLOT_PITCH = 40.0     # M-LOK slot center-to-center
SCREW_D = 5.3         # M5 or #10 clearance
HEAD_D = 9.0          # counterbore for the screw head
HEAD_DEPTH = 3.0
CORNER_R = 3.0
base = cq.Workplane("XY").rect(LENGTH, WIDTH).extrude(THICKNESS).edges("|Z").fillet(CORNER_R)
points = [(-SLOT_PITCH / 2, 0), (SLOT_PITCH / 2, 0)]
base = base.cut(cq.Workplane("XY").pushPoints(points).circle(SCREW_D / 2).extrude(THICKNESS + 2).translate((0, 0, -1)))
base = base.cut(cq.Workplane("XY").pushPoints(points).circle(HEAD_D / 2).extrude(HEAD_DEPTH + 1).translate((0, 0, THICKNESS - HEAD_DEPTH)))
result = base
