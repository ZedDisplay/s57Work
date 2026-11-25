"""build-mesh.py - build the navigation mesh of a district from its chart source cells.

    build-mesh.py CELLS [MORE_CELLS ...] --name 01CGD [-o data/tiles] [--work DIR] [-j N]
                  [--chart-json FILE] [--archive] [--keep-intermediate]

CELLS is a hand-over folder written by `s57-to-mbtiles.py --by-band
--cells-only` (data/cells): the exported cells and cells.json, the list of
them, which that tool writes last and only when every cell exported
completely. The build reads the list and the files it names, nothing else
in the folder; a folder without the list is refused. The first folder
holds the district's own cells: the mesh is built for every 0.25-degree
tile those cells reach. Any further folders hold other districts' cells,
read where they reach those tiles (the most detailed chart wins, whichever
district it is filed under).

    <work>/cell-index.json, <work>/tiles/    the tile stage (mesh/build.py)
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
    """Per cells folder: what its list says (when it was written, from which inputs, how many cells it names, the
    cancelled cells its export left out), and from the tile stage's cell index the cells of it the build reads
    (those with charted areas), by band."""
    from . import build
    with open(os.path.join(work, build.CELL_INDEX)) as f:
        cells = json.load(f)                     # [rank, band, name, root, bbox, primary, files]
    out = []
    for k, root in enumerate(roots):
        with open(os.path.join(root, build.CELLS_LIST)) as f:
            listed = json.load(f)
        mine = [c for c in cells if c[3] == root]
        by_band = {}
        for c in mine:
            by_band[c[1]] = by_band.get(c[1], 0) + 1
        out.append({"folder": root, "role": "own" if k == 0 else "other district",
                    "list_written": listed.get("written"), "inputs": listed.get("inputs"),
                    "cells_listed": len(listed["cells"]),
                    "cells": len(mine), "cells_by_band": dict(sorted(by_band.items())),
                    "cancelled_cells_left_out": listed.get("cancelled_cells_left_out", [])})
    return out


def resolve_paths(ap, a):
    """(cells folders, work folder, output folder), each resolved once (absolute, symbolic links followed), and
    checked once. The build removes exactly two folders: <work>/tiles (before the tile stage and after a build
    that passed) and <out>/<name>_mesh (before the build). No folder the build is given may be one of those two
    or lie inside one. Everything after this uses the resolved paths only, so what was checked is what is read,
    written and removed."""
    for d in a.cells:
        if not os.path.isdir(d):
            ap.error(f"no such folder: {d}")
    roots = [os.path.realpath(d) for d in a.cells]
    work = os.path.realpath(a.work or os.path.join("data", "mesh", a.name))
    out = os.path.realpath(a.out)
    removed = (os.path.join(work, "tiles"), os.path.join(out, f"{a.name}_mesh"))
    given = [(f"the cells folder {d}", r) for d, r in zip(a.cells, roots)]
    given += [(f"the output directory {a.out}", out), (f"the work directory {a.work or work}", work)]
    for what, path in given:
        for folder in removed:
            if path == folder or path.startswith(folder + os.sep):
                ap.error(f"{what} is {folder} or lies inside it, and the build removes that folder")
    return roots, work, out


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
    ap.add_argument("cells", nargs="+", help="hand-over folder(s) written by s57-to-mbtiles.py --by-band --cells-only "
                                             "(each holds cells.json); the first is the district's own")
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
    chart_meta = None
    if a.chart_json:
        try:
            with open(a.chart_json) as f:
                chart_meta = json.load(f)
        except (OSError, ValueError) as ex:
            ap.error(f"--chart-json {a.chart_json}: {ex}")
    roots, work, out = resolve_paths(ap, a)
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
    if os.path.exists(os.path.join(work, build.CELL_INDEX)):
        os.remove(os.path.join(work, build.CELL_INDEX))
    try:
        failed = build.run(work, a.workers, roots, log=log)
    except RuntimeError as ex:               # a cells folder is not a complete hand-over (mesh/build.py listed_cells)
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
