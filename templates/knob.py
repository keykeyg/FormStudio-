# name: Knob with nut trap
# about: Round grip knob with finger notches, a hex nut trap and a bolt hole (M6 by default).
import cadquery as cq
import math
DIAMETER = 40.0
HEIGHT = 18.0
NOTCHES = 8
NOTCH_D = 8.0
BOLT_D = 6.6          # M6 clearance
NUT_FLATS = 10.0      # M6 nut across flats
NUT_DEPTH = 5.5
FIT = 0.2
EDGE_R = 2.0
knob = cq.Workplane("XY").circle(DIAMETER / 2).extrude(HEIGHT)
notch_r = DIAMETER / 2 + NOTCH_D * 0.2
pts = [(notch_r * math.cos(2 * math.pi * i / NOTCHES), notch_r * math.sin(2 * math.pi * i / NOTCHES)) for i in range(NOTCHES)]
knob = knob.cut(cq.Workplane("XY").pushPoints(pts).circle(NOTCH_D / 2).extrude(HEIGHT + 2).translate((0, 0, -1)))
try:
    knob = knob.faces(">Z").edges().fillet(EDGE_R)
except Exception:
    pass
knob = knob.cut(cq.Workplane("XY").circle(BOLT_D / 2).extrude(HEIGHT + 2).translate((0, 0, -1)))
nut = cq.Workplane("XY").polygon(6, (NUT_FLATS + FIT) / math.cos(math.pi / 6)).extrude(NUT_DEPTH)
result = knob.cut(nut.translate((0, 0, -0.01)))
