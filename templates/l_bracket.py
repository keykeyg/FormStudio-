# name: L-bracket
# about: Right-angle bracket with screw holes in both legs and a rounded inside corner.
import cadquery as cq
LEG_A = 40.0          # flat leg length
LEG_B = 40.0          # upright leg length
WIDTH = 20.0
THICKNESS = 4.0
HOLE_D = 4.5          # M4 clearance hole
CORNER_R = 4.0        # inside corner radius (adds strength)
profile = (cq.Workplane("XZ").polyline([(0, 0), (LEG_A, 0), (LEG_A, THICKNESS), (THICKNESS, THICKNESS), (THICKNESS, LEG_B), (0, LEG_B)])
           .close().extrude(WIDTH / 2, both=True))
gusset = (cq.Workplane("XZ").rect(CORNER_R, CORNER_R, centered=False).extrude(WIDTH / 2, both=True)
          .translate((THICKNESS, 0, THICKNESS))
          .cut(cq.Workplane("XZ").center(THICKNESS + CORNER_R, THICKNESS + CORNER_R).circle(CORNER_R).extrude(WIDTH, both=True)))
body = profile.union(gusset)
flat_hole = cq.Workplane("XY").center((LEG_A + THICKNESS + CORNER_R) / 2, 0).circle(HOLE_D / 2).extrude(THICKNESS * 3).translate((0, 0, -THICKNESS))
up_hole = cq.Workplane("YZ").center(0, (LEG_B + THICKNESS + CORNER_R) / 2).circle(HOLE_D / 2).extrude(THICKNESS * 3).translate((-THICKNESS, 0, 0))
result = body.cut(flat_hole).cut(up_hole).translate((-LEG_A / 2, 0, 0))
