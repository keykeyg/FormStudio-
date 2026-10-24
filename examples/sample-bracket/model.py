import cadquery as cq
WIDTH = 80.0
DEPTH = 40.0
THICKNESS = 5.0
HOLE_DIAMETER = 6.0
HOLE_SPACING = 60.0
CORNER_RADIUS = 3.0
result = cq.Workplane("XY").box(WIDTH, DEPTH, THICKNESS)
result = result.edges("|Z").fillet(CORNER_RADIUS)
result = result.faces(">Z").workplane().pushPoints([(-HOLE_SPACING/2,0),(HOLE_SPACING/2,0)]).hole(HOLE_DIAMETER)
