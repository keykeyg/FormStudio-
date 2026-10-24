# name: Cable clip
# about: Snap-in clip for one cable or tube, with a screw tab.
import cadquery as cq
CABLE_D = 8.0
WALL = 2.0
WIDTH = 10.0
OPENING = 0.75        # opening as a fraction of the cable diameter
TAB_L = 14.0
SCREW_D = 3.6
r_in = CABLE_D / 2
r_out = r_in + WALL
ring = cq.Workplane("XZ").center(0, r_out).circle(r_out).circle(r_in).extrude(WIDTH / 2, both=True)
gap = cq.Workplane("XZ").center(0, r_out * 2).rect(CABLE_D * OPENING, r_out * 1.5).extrude(WIDTH, both=True)
ring = ring.cut(gap)
tab = cq.Workplane("XY").center(r_out + TAB_L / 2 - WALL, 0).rect(TAB_L + WALL, WIDTH).extrude(WALL)
tab = tab.cut(cq.Workplane("XY").center(r_out + TAB_L / 2, 0).circle(SCREW_D / 2).extrude(WALL + 2).translate((0, 0, -1)))
result = ring.union(tab)
