# name: Picatinny rail section
# about: Rail with MIL-STD-1913 slot spacing (5.23 mm slots on 10.01 mm centers) and two screw holes. The 45° clamping profile is approximate: test-fit before relying on it.
import cadquery as cq
SLOTS = 6             # number of cross slots
SLOT_PITCH = 10.01
SLOT_W = 5.23
SLOT_DEPTH = 3.0
RAIL_W = 21.2         # across the 45° clamping edges
TOP_W = 15.6          # flat top
BASE_W = 24.0         # mounting base width
BASE_T = 4.0
RAIL_H = 6.0          # dovetail height above the base
SCREW_D = 5.3         # M5 / #10 clearance
LENGTH = SLOTS * SLOT_PITCH
edge = (RAIL_W - TOP_W) / 2
profile = [(-BASE_W / 2, 0), (BASE_W / 2, 0), (BASE_W / 2, BASE_T), (TOP_W / 2, BASE_T),
           (RAIL_W / 2, BASE_T + RAIL_H - edge), (TOP_W / 2, BASE_T + RAIL_H), (-TOP_W / 2, BASE_T + RAIL_H),
           (-RAIL_W / 2, BASE_T + RAIL_H - edge), (-TOP_W / 2, BASE_T)]
rail = cq.Workplane("YZ").polyline(profile).close().extrude(LENGTH / 2, both=True)
slots = (cq.Workplane("XY").pushPoints([((i - (SLOTS - 1) / 2) * SLOT_PITCH, 0) for i in range(SLOTS)])
         .rect(SLOT_W, RAIL_W + 2).extrude(SLOT_DEPTH + 1).translate((0, 0, BASE_T + RAIL_H - SLOT_DEPTH)))
rail = rail.cut(slots)
holes = (cq.Workplane("XY").pushPoints([(-(SLOTS / 2 - 1) * SLOT_PITCH, 0), ((SLOTS / 2 - 1) * SLOT_PITCH, 0)])
         .circle(SCREW_D / 2).extrude(BASE_T + RAIL_H + 2).translate((0, 0, -1)))
result = rail.cut(holes)
