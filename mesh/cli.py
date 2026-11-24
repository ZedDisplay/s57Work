"""build-mesh.py - build the navigation mesh of a district from its chart source cells.

    build-mesh.py CELLS [MORE_CELLS ...] --name 01CGD [-o data/tiles] [--work DIR] [-j N]
                  [--chart-json FILE] [--archive] [--keep-intermediate]

CELLS is a folder of per-cell GeoJSON exports as s57-to-mbtiles.py writes
them (data/geojson: bandN/LAYER_CELL.geojson, with data/cancelled-cells.json
beside the folder). The first folder holds the district's own cells: the
mesh is built for every 0.25-degree tile those cells reach. Any further
folders hold other districts' cells, read where they reach those tiles
(the most detailed chart wins, whichever district it is filed under).

    <work>/cells.json, <work>/tiles/    the tile stage (mesh/build.py)
    <out>/<name>_mesh/                  the mesh: binary tiles + index.json
    <out>/<name>_mesh.json              sidecar: what it was built from, extent, counts
    <out>/<name>_mesh.tar.zst           with --archive

Exit status: 0 built and every check passed; 1 the tile stage failed;
2 a check of the second stage failed (its message says which and where).
The sidecar and the archive are written only on 0.

Needs the packages in mesh/requirements.txt, and a C++ compiler with CGAL,
GMP and MPFR to build mesh/exact_cut.cpp (done here when the program is
missing or older than its source).
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

from . import MESH_FORMAT_MAGIC, MESH_INDEX_VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
TILE_DEG = 0.25


def build_cutter(log):
    """Compile mesh/exact_cut.cpp to mesh/exact_cut when it is missing or older than its source."""
    src, exe = os.path.join(HERE, "exact_cut.cpp"), os.path.join(HERE, "exact_cut")
    if os.path.exists(exe) and os.path.getmtime(exe) >= os.path.getmtime(src):
        return True
    cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        log("ERROR: no C++ compiler found (set CXX)")
        return False
    cmd = [cxx, "-std=c++17", "-O2"]
    for d in ("/opt/homebrew/include", "/usr/local/include"):
        if os.path.isdir(os.path.join(d, "CGAL")):
            cmd += ["-I", d, "-L", os.path.join(os.path.dirname(d), "lib")]
            break
    cmd += [src, "-lgmp", "-lmpfr", "-o", exe]
    log("compiling the exact cutter: " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("ERROR: the exact cutter did not compile:\n" + r.stderr[-2000:])
        return False
    return True


def project_version():
    try:
        import tomllib
        with open(os.path.join(os.path.dirname(HERE), "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return None


def make_archive(out_dir, folder, log):
    """<out_dir>/<folder>.tar.zst holding <folder>/ as its top folder."""
    archive = os.path.join(out_dir, folder + ".tar.zst")
    if os.path.exists(archive):
        os.remove(archive)
    cmd = ["tar", "--zstd", "-cf", archive, "-C", out_dir, folder]
    log("archive: " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tar failed ({r.returncode}): {r.stderr.strip()}")
    return archive


def cells_record(work, roots):
    """Per cells folder, from the tile stage's cell index: the live cells with charted areas, by band, and the
    cancelled cells left out (the chart build's record beside the folder)."""
    with open(os.path.join(work, "cells.json")) as f:
        cells = json.load(f)                     # [rank, band, name, root, bbox, primary, file suffix]
    out = []
    for k, root in enumerate(roots):
        mine = [c for c in cells if c[3] == root]
        by_band = {}
        for c in mine:
            by_band[c[1]] = by_band.get(c[1], 0) + 1
        cancelled = []
        cj = os.path.join(os.path.dirname(root.rstrip("/")), "cancelled-cells.json")
        if os.path.exists(cj):
            with open(cj) as f:
                cancelled = sorted(c["cell"].upper() for c in json.load(f).get("cancelled", []))
        out.append({"folder": root, "role": "own" if k == 0 else "other district",
                    "cells": len(mine), "cells_by_band": dict(sorted(by_band.items())),
                    "cancelled_cells_left_out": cancelled})
    return out


def tile_stage_record(work):
    """Totals of the tile stage (mesh/build.py), from <work>/tiles.json."""
    with open(os.path.join(work, "tiles.json")) as f:
        t = json.load(f)
    ok = [r for r in t["results"] if r["ok"] and r.get("shapes")]

    def km2(key):
        return round(sum(r[key] for r in ok) / 1e6, 3)

    return {"tiles_reached": len(t["results"]), "tiles_with_chart_shapes": len(ok),
            "tiles_with_water": sum(1 for r in ok if r["water"]), "seconds": round(t["seconds"], 1),
            "water_km2": km2("water_m2"), "water_without_depth_km2": km2("nodepth_m2"),
            "land_km2": km2("land_m2"), "uncharted_km2": km2("nochart_m2"),
            "same_rank_overlap_km2": km2("same_rank_overlap_m2"),
            "sides_on_chart_lines": sum(r["line_sides"] for r in ok),
            "chart_points_as_corners": sum(r["corner_points"] for r in ok)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build-mesh.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cells", nargs="+", help="folder(s) of per-cell GeoJSON; the first is the district's own")
    ap.add_argument("--name", required=True, help="output name, e.g. 01CGD")
    ap.add_argument("-o", "--out", default="data/tiles", help="output directory (default data/tiles)")
    ap.add_argument("--work", help="work directory (default data/mesh/<name>)")
    ap.add_argument("-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--chart-json", metavar="FILE",
                    help="the published chart's metadata JSON; its build_date is recorded as chart_build_date")
    ap.add_argument("--archive", action="store_true", help="also write <name>_mesh.tar.zst (needs tar --zstd)")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="keep <work>/tiles (the cut of every tile) after a successful build")
    a = ap.parse_args(argv)
    for d in a.cells:
        if not os.path.isdir(d):
            ap.error(f"no such folder: {d}")
    chart_meta = None
    if a.chart_json:
        try:
            with open(a.chart_json) as f:
                chart_meta = json.load(f)
        except (OSError, ValueError) as ex:
            ap.error(f"--chart-json {a.chart_json}: {ex}")
    # Absolute, because the second stage runs in its own interpreter from the repository folder; and resolved
    # (symbolic links followed) once, here, so that the folders checked below are the folders written to.
    work = os.path.realpath(a.work or os.path.join("data", "mesh", a.name))
    out = os.path.realpath(a.out)
    def inside(path, folder):
        """Is `path` the folder itself or something under it (by real path)?"""
        path, folder = os.path.realpath(path), os.path.realpath(folder)
        return os.path.commonpath([path, folder]) == folder

    # <work>/tiles is emptied before the build and removed after it, and <out>/<name>_mesh is removed before it:
    # neither the output nor a cells folder may be one of them or lie inside one.
    emptied = os.path.join(work, "tiles")
    replaced = os.path.join(out, f"{a.name}_mesh")
    if inside(out, emptied):
        ap.error(f"the output directory {out} is the work directory's tiles folder or lies inside it "
                 f"({emptied}), which the build empties and removes")
    for d in a.cells:
        for folder in (emptied, replaced):
            if inside(d, folder):
                ap.error(f"the cells folder {d} is {folder} or lies inside it, which the build removes before reading the cells")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    def log(msg=""):
        print(msg, flush=True)

    started = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.time()
    side_path = os.path.join(out, f"{a.name}_mesh.json")
    archive_path = os.path.join(out, f"{a.name}_mesh.tar.zst")
    mesh_dir = os.path.join(out, f"{a.name}_mesh")
    # Nothing of an earlier build of this name may be taken for this one's result.
    for p in (side_path, archive_path):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(mesh_dir):
        shutil.rmtree(mesh_dir)
    if not build_cutter(log):
        return 1
    from . import build
    tiles_dir = os.path.join(work, "tiles")
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    if os.path.exists(os.path.join(work, "cells.json")):
        os.remove(os.path.join(work, "cells.json"))
    roots = [os.path.abspath(d) for d in a.cells]
    try:
        failed = build.run(work, a.workers, roots, log=log)
    except RuntimeError as ex:               # the cells folders are not what the chart build exports
        log(f"ERROR: {ex}")
        return 1
    if failed:
        log("the tile stage failed")
        return 1
    r = subprocess.run([sys.executable, "-m", "mesh.finalize", work, mesh_dir, str(a.workers), a.name],
                       cwd=os.path.dirname(HERE))
    if r.returncode != 0:
        log(f"build-mesh: the second stage stopped (status {r.returncode}) after {time.time() - t0:.0f} s; "
            f"no sidecar written; the tile stage's files are kept in {work}")
        return 2

    with open(os.path.join(mesh_dir, "index.json")) as f:
        idx = json.load(f)
    nbytes = sum(os.path.getsize(os.path.join(mesh_dir, t["file"])) for t in idx["tiles"])
    side = {
        "name": a.name,
        "mesh_dir": f"{a.name}_mesh",
        "format": {"magic": MESH_FORMAT_MAGIC, "index_version": MESH_INDEX_VERSION,
                   "tile_deg": TILE_DEG, "multi_cluster": False},
        "build_date": started,
        "build_seconds": round(time.time() - t0, 1),
        "builder_version": project_version(),
        "workers": a.workers,
        "cells": cells_record(work, roots),
        "chart_build_date": (chart_meta or {}).get("build_date"),
        "chart_metadata": chart_meta,
        "tile_stage": tile_stage_record(work),
        "clusters": [{"dir": ".", "box": [idx["west"], idx["south"], idx["east"], idx["north"]],
                      "x_scale": idx["xScale"], "triangles": idx["triangles"], "mesh_tiles": len(idx["tiles"])}],
        "triangles": idx["triangles"],
        "mesh_tiles": len(idx["tiles"]),
        "bytes": nbytes,
        # The second stage exits non-zero on any failed check (mesh/finalize.py lists them), and then no
        # sidecar is written: a sidecar exists only for a mesh that passed every one.
        "passed": True,
    }
    with open(side_path, "w") as f:
        json.dump(side, f, indent=1)
    log(f"sidecar: {side_path}")
    if a.archive:
        arc = make_archive(out, f"{a.name}_mesh", log)
        log(f"archive: {arc} ({os.path.getsize(arc) / 1e6:.0f} MB)")
    if not a.keep_intermediate:
        shutil.rmtree(tiles_dir, ignore_errors=True)
    log(f"build-mesh: {a.name}: {side['triangles']:,} triangles in {side['mesh_tiles']} tiles, "
        f"{nbytes / 1e6:.0f} MB, {time.time() - t0:.0f} s")
    return 0
