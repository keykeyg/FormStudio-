# name: Wall hook
# about: Screw-on hook with two counterbored screw holes. Prints flat on its side, so the hook is strong.
import cadquery as cq
PLATE_H = 60.0
WIDTH = 18.0
THICKNESS = 5.0
REACH = 30.0          # how far the hook sticks out
HOOK_H = 18.0         # height of the hook's tip
SCREW_D = 4.0
HEAD_D = 8.0
profile = [(0, 0), (THICKNESS, 0), (THICKNESS, PLATE_H - REACH * 0.4), (REACH, PLATE_H - REACH * 0.2),
           (REACH, PLATE_H - REACH * 0.2 + HOOK_H), (REACH - THICKNESS, PLATE_H - REACH * 0.2 + HOOK_H),
           (REACH - THICKNESS, PLATE_H - REACH * 0.2 + THICKNESS * 0.9), (THICKNESS, PLATE_H - REACH * 0.4 + THICKNESS * 1.6), (THICKNESS, PLATE_H), (0, PLATE_H)]
hook = cq.Workplane("XZ").polyline(profile).close().extrude(WIDTH / 2, both=True)
for z in (PLATE_H * 0.2, PLATE_H * 0.55):
    hole = cq.Workplane("YZ").center(0, z).circle(SCREW_D / 2).extrude(THICKNESS + 2).translate((-1, 0, 0))
    hook = hook.cut(hole)
    hook = hook.cut(cq.Workplane("YZ").center(0, z).circle(HEAD_D / 2).extrude((HEAD_D - SCREW_D) / 2).translate((THICKNESS - (HEAD_D - SCREW_D) / 2, 0, 0)))
result = hook
