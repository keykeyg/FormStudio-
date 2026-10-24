# name: Enclosure box + lid
# about: Rounded project box with a press-fit lid printed next to it. Change the inside size and wall.
import cadquery as cq
PARTS = 2             # box and lid are separate parts
INSIDE_L = 80.0
INSIDE_W = 50.0
INSIDE_H = 30.0
WALL = 2.0
FLOOR = 2.0
CORNER_R = 4.0
LID_T = 2.0
LIP_H = 4.0
FIT = 0.2             # gap between the lid's lip and the box wall
L = INSIDE_L + 2 * WALL
W = INSIDE_W + 2 * WALL
box = cq.Workplane("XY").rect(L, W).extrude(INSIDE_H + FLOOR).edges("|Z").fillet(CORNER_R)
box = box.cut(cq.Workplane("XY").rect(INSIDE_L, INSIDE_W).extrude(INSIDE_H + 1).edges("|Z").fillet(max(0.5, CORNER_R - WALL)).translate((0, 0, FLOOR)))
lid = cq.Workplane("XY").rect(L, W).extrude(LID_T).edges("|Z").fillet(CORNER_R)
lip_l, lip_w = INSIDE_L - 2 * FIT, INSIDE_W - 2 * FIT
lip = cq.Workplane("XY").rect(lip_l, lip_w).extrude(LIP_H).edges("|Z").fillet(max(0.5, CORNER_R - WALL - FIT))
lip = lip.cut(cq.Workplane("XY").rect(lip_l - 2 * WALL, lip_w - 2 * WALL).extrude(LIP_H + 1).translate((0, 0, -0.5)))
lid = lid.union(lip.translate((0, 0, LID_T))).translate((0, W + 10, 0))
result = cq.Compound.makeCompound([box.val(), lid.val()])
