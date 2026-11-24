#!/usr/bin/env python3
"""
count-mesh-triangles.py — what a navigation mesh holds inside a box

Reads a mesh folder (index.json + tiles, see docs/MESH.md) and reports,
for each box, the triangles whose centroid lies inside it: count,
navigable count, depth known / unknown, hazards, marks, and the mesh
area in km². Use it to compare two builds of the same place, or to see
whether a place is covered at all. Reads both tile formats: WRPMESH2
(marks are corners: "mark" counts the triangles with a marked corner)
and the earlier WRPMESH1 (marks were a triangle flag).

Usage:
  count-mesh-triangles.py MESH_DIR --box W,S,E,N [--box ...]
  count-mesh-triangles.py MESH_DIR --point LON,LAT [--radius-deg 0.05]
  count-mesh-triangles.py MESH_A MESH_B --box ...     # side by side

Needs numpy.
"""
import argparse
import json
import math
import os
import struct
import sys

import numpy as np

HEADER = 32
R_EARTH_M = 6_371_008.8


def read_tile(path, n):
    with open(path, "rb") as f:
        head = f.read(HEADER)
        if head[:8] not in (b"WRPMESH1", b"WRPMESH2"):
            raise ValueError(f"{path}: bad magic")
        if struct.unpack("<I", head[8:12])[0] != n:
            raise ValueError(f"{path}: triangle count differs from index.json")
        corners = np.frombuffer(f.read(n * 48), dtype="<f8").reshape(n, 3, 2)
        f.seek(n * 12, 1)                                     # neighbours
        f.seek(n * 4, 1)                                      # mult
        depth = np.frombuffer(f.read(n * 4), dtype="<f4")
        f.seek(n * 4, 1)                                      # clear
        f.seek(n * 4, 1)                                      # hazv
        f.seek(n * 3, 1)                                      # rev
        flags = np.frombuffer(f.read(n), dtype=np.uint8)
        if head[:8] == b"WRPMESH1":
            mark = (flags & 4) > 0
        else:
            f.seek(n * 3 + n * 12 + n * 12, 1)                # sideFlags, sideClear, sideHazv
            corner_flags = np.frombuffer(f.read(n * 3), dtype=np.uint8).reshape(n, 3)
            mark = ((corner_flags & 2) > 0).any(axis=1)       # cornerFlagBits.mark
    return corners, depth, flags, mark


def lon_runs(west, east):
    """The runs of stored longitude (between -180 and 180) a box covers going east from `west` to `east`: one
    run, or two when the box crosses the date line (its west greater than its east, or an end beyond 180)."""
    if east - west >= 360:
        return [(-180.0, 180.0)]
    w = ((west + 180.0) % 360.0) - 180.0
    e = ((east + 180.0) % 360.0) - 180.0
    if e == -180.0 and east > west and w != -180.0:
        e = 180.0                                     # a box that ends on the date line ends at 180
    return [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]


def measure(mesh_dir, boxes):
    with open(os.path.join(mesh_dir, "index.json")) as f:
        idx = json.load(f)
    K = idx["xScale"]
    out = {tuple(b): dict(triangles=0, navigable=0, depth_known=0, hazard=0, mark=0, area_km2=0.0,
                          tiles=0) for b in boxes}
    runs = {tuple(b): lon_runs(b[0], b[2]) for b in boxes}
    for t in idx["tiles"]:
        bb = t["bbox"]
        # a tile's extent is plain numbers (no tile lies across the date line); a box may be two runs
        hits = [b for b in boxes if not (bb[3] < b[1] or bb[1] > b[3])
                and any(not (bb[2] < w or bb[0] > e) for w, e in runs[tuple(b)])]
        if not hits:
            continue
        corners, depth, flags, mark = read_tile(os.path.join(mesh_dir, t["file"]), t["n"])
        cen = corners.mean(axis=1)
        lon = cen[:, 0] / K; lat = cen[:, 1]
        # area in the mesh's own frame (x = lon*K deg, y = lat deg) -> km²
        a = corners[:, 0]; b_ = corners[:, 1]; c = corners[:, 2]
        cr = np.abs((b_[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b_[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
        deg_m = R_EARTH_M * math.pi / 180.0
        area_km2 = cr * deg_m * deg_m / 1e6
        for b in hits:
            in_lon = np.zeros(len(lon), dtype=bool)
            for w, e in runs[tuple(b)]:
                in_lon |= (lon >= w) & (lon <= e)
            m = in_lon & (lat >= b[1]) & (lat <= b[3])
            if not m.any():
                continue
            r = out[tuple(b)]
            r["tiles"] += 1
            r["triangles"] += int(m.sum())
            r["navigable"] += int((flags[m] & 1).sum())
            r["depth_known"] += int((depth[m] != -999).sum())
            r["hazard"] += int(((flags[m] & 2) > 0).sum())
            r["mark"] += int(mark[m].sum())
            r["area_km2"] += float(area_km2[m].sum())
    return idx, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mesh", nargs="+", help="mesh folder(s) holding index.json")
    ap.add_argument("--box", action="append", default=[], metavar="W,S,E,N")
    ap.add_argument("--point", action="append", default=[], metavar="LON,LAT")
    ap.add_argument("--radius-deg", type=float, default=0.05, help="half-width of the box around each --point")
    a = ap.parse_args()
    boxes = [tuple(float(v) for v in s.split(",")) for s in a.box]
    for s in a.point:
        lon, lat = (float(v) for v in s.split(","))
        boxes.append((lon - a.radius_deg, lat - a.radius_deg, lon + a.radius_deg, lat + a.radius_deg))
    if not boxes:
        ap.error("give at least one --box or --point")
    results = []
    for m in a.mesh:
        idx, r = measure(m, boxes)
        results.append((m, idx, r))
        print(f"{m}: {idx['triangles']:,} triangles in {len(idx['tiles'])} tiles, "
              f"box {idx['west']:.4f},{idx['south']:.4f},{idx['east']:.4f},{idx['north']:.4f}, xScale {idx['xScale']:.6f}")
    cols = ("tiles", "triangles", "navigable", "depth_known", "hazard", "mark", "area_km2")
    for b in boxes:
        print(f"\nbox {b[0]:.4f},{b[1]:.4f} .. {b[2]:.4f},{b[3]:.4f}")
        print("  " + "mesh".ljust(40) + "".join(c.rjust(13) for c in cols))
        for m, _, r in results:
            v = r[tuple(b)]
            print("  " + os.path.basename(m.rstrip("/")).ljust(40)
                  + "".join((f"{v[c]:13.2f}" if c == "area_km2" else f"{v[c]:13,}") for c in cols))


if __name__ == "__main__":
    sys.exit(main())
