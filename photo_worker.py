"""Measure parts in photos. Runs in the CadQuery Python (needs opencv-python-headless and numpy).

  card <out.pdf>                 write the printable reference card
  measure <photo> <overlay.png>  find the card, correct the camera angle, trace the part; prints JSON

The card has four ArUco markers (DICT_4X4_50, ids 0-3) whose outer corners sit on a 180 x 240 mm
rectangle. Lay the part flat between them and shoot from above.
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

MARKER = 30.0                  # marker size, mm (black border included)
FRAME = (180.0, 240.0)         # outer corners of the four markers, mm
STAGE = (4.0, MARKER + 4.0, FRAME[0] - 4.0, FRAME[1] - MARKER - 4.0)  # where the part goes: x0, y0, x1, y1
SCALE = 5.0                    # px per mm in the corrected image
PAGE = (215.9, 279.4)          # US Letter; the layout also fits A4
ORIGIN = ((PAGE[0] - FRAME[0]) / 2, (PAGE[1] - FRAME[1]) / 2)


def marker_corners():
    """Card-frame corners (mm, y down) of each marker in ArUco order: TL, TR, BR, BL."""
    w, h, m = FRAME[0], FRAME[1], MARKER
    tops = {0: (0, 0), 1: (w - m, 0), 2: (w - m, h - m), 3: (0, h - m)}
    return {i: np.array([(x, y), (x + m, y), (x + m, y + m), (x, y + m)], dtype=np.float32) for i, (x, y) in tops.items()}


def dictionary():
    return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def write_card(out):
    """A one-page PDF drawn with vector squares, so it prints at exact size."""
    pt = 72 / 25.4
    ops = []

    def rect(x, y, w, h):  # mm, page coordinates with y down
        ops.append(f"{x * pt:.3f} {(PAGE[1] - y - h) * pt:.3f} {w * pt:.3f} {h * pt:.3f} re f")

    def text(x, y, size, s):
        s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /F1 {size} Tf {x * pt:.3f} {(PAGE[1] - y) * pt:.3f} Td ({s}) Tj ET")

    cell = MARKER / 6
    for i, corners in marker_corners().items():
        bits = cv2.aruco.generateImageMarker(dictionary(), i, 6)
        x0, y0 = ORIGIN[0] + corners[0][0], ORIGIN[1] + corners[0][1]
        for r in range(6):
            for c in range(6):
                if bits[r, c] < 128:
                    rect(x0 + c * cell, y0 + r * cell, cell + 0.02, cell + 0.02)
    # Light corner ticks mark the stage; they are thin so they don't look like a part.
    sx0, sy0, sx1, sy1 = STAGE
    for x, y in ((sx0, sy0), (sx1, sy0), (sx0, sy1), (sx1, sy1)):
        rect(ORIGIN[0] + x - 3, ORIGIN[1] + y - 0.15, 6, 0.3)
        rect(ORIGIN[0] + x - 0.15, ORIGIN[1] + y - 3, 0.3, 6)
    # 100 mm check bar between the bottom markers.
    bx, by = ORIGIN[0] + (FRAME[0] - 100) / 2, ORIGIN[1] + FRAME[1] - 12
    rect(bx, by, 100, 1.2)
    for k in range(11):
        rect(bx + k * 10 - 0.2, by - (3 if k % 5 else 5), 0.4, 3 if k % 5 else 5)
    text(bx, by + 7, 8, "Check: this bar must measure 100 mm. If not, print at 100% / Actual size.")
    text(ORIGIN[0] + MARKER + 6, ORIGIN[1] + 9, 10, "Form Studio reference card")
    text(ORIGIN[0] + MARKER + 6, ORIGIN[1] + 15, 7, "Lay the part flat in the middle. Shoot straight down,")
    text(ORIGIN[0] + MARKER + 6, ORIGIN[1] + 19.5, 7, "all four squares in view, soft light, no flash.")
    text(ORIGIN[0] + MARKER + 6, ORIGIN[1] + 24, 7, "For a side profile, lay the part on its side.")
    stream = "\n".join(ops).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE[0] * pt:.2f} {PAGE[1] * pt:.2f}] "
            f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>".encode(),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    pdf += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(out).write_bytes(pdf)


def find_card(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    corners, ids, _ = cv2.aruco.ArucoDetector(dictionary(), params).detectMarkers(gray)
    known = marker_corners()
    src, dst, found = [], [], []
    for c, i in zip(corners, [] if ids is None else ids.flatten()):
        if int(i) in known and int(i) not in found:
            found.append(int(i))
            src.extend(c.reshape(4, 2))
            dst.extend(known[int(i)])
    if len(found) < 2:
        return None, found
    h, _ = cv2.findHomography(np.array(src, np.float32), np.array(dst, np.float32), cv2.RANSAC, 3.0)
    return h, found


def trace(warped):
    """Separate the part from the white card inside the stage. Returns (contour, holes) in corrected pixels."""
    x0, y0, x1, y1 = (int(v * SCALE) for v in STAGE)
    stage = warped[y0:y1, x0:x1]
    lab = cv2.cvtColor(stage, cv2.COLOR_BGR2LAB).astype(np.float32)
    border = np.concatenate([lab[:6].reshape(-1, 3), lab[-6:].reshape(-1, 3), lab[:, :6].reshape(-1, 3), lab[:, -6:].reshape(-1, 3)])
    paper = np.median(border, axis=0)
    diff = np.sqrt((((lab - paper) * np.array([0.6, 1.4, 1.4])) ** 2).sum(axis=2))
    diff = cv2.GaussianBlur(np.clip(diff, 0, 255).astype(np.uint8), (5, 5), 0)
    level, mask = cv2.threshold(diff, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if level < 12:  # nothing stands out from the paper
        return None, [], 0
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None, [], level
    outer = [i for i in range(len(contours)) if hierarchy[0][i][3] < 0]
    best = max(outer, key=lambda i: cv2.contourArea(contours[i]))
    if cv2.contourArea(contours[best]) < (10 * SCALE) ** 2:
        return None, [], level
    shift = np.array([[x0, y0]])
    holes = [contours[i] + shift for i in range(len(contours))
             if hierarchy[0][i][3] == best and cv2.contourArea(contours[i]) > (1.5 * SCALE) ** 2]
    return contours[best] + shift, holes, level


def measure(photo, overlay_path):
    image = cv2.imread(str(photo), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Couldn't read the photo")
    h, found = find_card(image)
    if h is None:
        return {"card": False, "markers": found,
                "message": "The reference card wasn't found: all four black squares should be in the photo (at least two must be clear)."}
    size = (int(FRAME[0] * SCALE), int(FRAME[1] * SCALE))
    warped = cv2.warpPerspective(image, np.array([[SCALE, 0, 0], [0, SCALE, 0], [0, 0, 1]]) @ h, size, flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255))
    contour, holes, level = trace(warped)
    result = {"card": True, "markers": found}
    view = warped.copy()
    if contour is None:
        result["message"] = "The card was found, but no part stood out from it. Use soft light and a part that contrasts with white paper."
    else:
        eps = 0.35 * SCALE
        poly = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2).astype(float) / SCALE
        x, y, w, hgt = cv2.boundingRect(contour)
        rect = cv2.minAreaRect(contour)
        long_side, short_side = sorted(rect[1], reverse=True)
        bottom = y + hgt  # card y points down; flip so the outline reads with z up
        points = [(round(px - x / SCALE, 1), round(bottom / SCALE - py, 1)) for px, py in poly]
        hole_list = []
        for hc in holes:
            (cx, cy), r = cv2.minEnclosingCircle(hc)
            area = cv2.contourArea(hc)
            round_ness = area / (np.pi * r * r) if r else 0
            hx, hy, hw, hh = cv2.boundingRect(hc)
            hole_list.append({"center": [round((cx - x) / SCALE, 1), round((bottom - cy) / SCALE, 1)],
                              "diameter": round((2 * r - 1) / SCALE, 1) if round_ness > 0.75 else None,
                              "size": [round(hw / SCALE, 1), round(hh / SCALE, 1)]})
        w, hgt = w - 1, hgt - 1  # boundingRect counts both edge pixels
        result.update({"width": round(w / SCALE, 1), "height": round(hgt / SCALE, 1),
                       "longest": round(long_side / SCALE, 1), "shortest": round(short_side / SCALE, 1),
                       "outline": points, "holes": hole_list, "contrast": round(float(level), 1)})
        # Dark outline with a white halo, plus written sizes: readable without relying on color.
        cv2.drawContours(view, [contour], -1, (255, 255, 255), 7)
        cv2.drawContours(view, [contour], -1, (20, 20, 20), 3)
        for hc in holes:
            cv2.drawContours(view, [hc], -1, (255, 255, 255), 5)
            cv2.drawContours(view, [hc], -1, (20, 20, 20), 2)
        for label, org in ((f"{w / SCALE:.1f} mm wide", (x, min(size[1] - 8, y + hgt + 28))), (f"{hgt / SCALE:.1f} mm tall", (x, max(20, y - 10)))):
            cv2.putText(view, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 5, cv2.LINE_AA)
            cv2.putText(view, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)
        result["message"] = f"Traced the part: {w / SCALE:.1f} × {hgt / SCALE:.1f} mm on the card ({len(points)} outline points, {len(holes)} hole(s))."
    cv2.imwrite(str(overlay_path), view)
    return result


def main(argv):
    if argv[:1] == ["card"] and len(argv) == 2:
        write_card(argv[1])
        print(json.dumps({"ok": True}))
    elif argv[:1] == ["measure"] and len(argv) == 3:
        print(json.dumps(measure(argv[1], argv[2])))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
