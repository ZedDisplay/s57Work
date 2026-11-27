"""build-mesh.py - build the navigation mesh of a district from its charts.

    build-mesh.py CHARTS [MORE_CHARTS ...] --name 01CGD
                  [-o data/tiles] [--work DIR] [-j N] [--chart-json FILE] [--archive] [--keep-intermediate]

CHARTS is the district's charts: NOAA's zip of S-57 cells (01CGD_ENCs.zip),
or a folder of them; several when the district's charts come in several
files. The mesh is built from those charts and from nothing else, for every
0.25-degree tile their cells reach; where two of them chart the same spot
the most detailed wins. The build reads the charts itself (mesh/cells.py,
with GDAL's ogrinfo and ogr2ogr): one copy of each cell, the latest issued;
cancelled cells left out.

Every run works in a new folder of its own, <work>/run-XXXXXXXX: the
unpacked zips, the extracted layers, the cut of every tile. Nothing of an
earlier run is read. It is removed after a build that passed (kept with
--keep-intermediate) and kept after one that did not.

    <out>/<name>_mesh/                  the mesh: binary tiles + index.json
    <out>/<name>_mesh.json              sidecar: what it was built from, extent, counts
    <out>/<name>_mesh.tar.zst           with --archive

Exit status: 0 built and every check passed; 1 the charts could not be
read, the tile stage failed or the archive could not be written; 2 a
check of the second stage failed (its message says which and where). The
sidecar is written last and only on 0.

Needs GDAL (ogrinfo, ogr2ogr; 3.7 or later), the packages in
mesh/requirements.txt, and a C++ compiler with CGAL, GMP and MPFR to build
mesh/exact_cut.cpp (done here when the program is missing or older than
its source).
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def outputs(out, name):
    """(mesh folder, sidecar, archive) of a build: what it writes into the output directory, and all it removes
    there before it starts."""
    return (os.path.join(out, f"{name}_mesh"), os.path.join(out, f"{name}_mesh.json"),
            os.path.join(out, f"{name}_mesh.tar.zst"))


def resolve_paths(ap, a):
    """(chart inputs, work folder, output folder), each resolved once (absolute, symbolic links followed) and
    checked once. Everything after this uses the resolved paths only, so what was checked is what
    is read, written and removed.

    The build removes one thing it did not make in this run: the earlier result of the same name in the output
    directory (outputs). Nothing it is given may be that or lie inside it, and a charts folder may not hold the
    mesh folder (charts inside it would be gone before they are read). Its working files go into a new folder
    of this run inside the work folder, which nothing given can lie in; a charts folder may not hold the work
    folder (the charts the run unpacks there would be found again as that folder's own cells)."""
    given = list(a.charts)
    for p in given:
        if not os.path.exists(p):
            ap.error(f"no such file or folder: {p}")
    inputs = [os.path.realpath(p) for p in given]
    work = os.path.realpath(a.work or os.path.join("data", "mesh", a.name))
    out = os.path.realpath(a.out)
    mesh_dir, side, archive = outputs(out, a.name)

    def inside(path, folder):
        return path == folder or path.startswith(folder + os.sep)

    named = [(f"the charts {p}", r) for p, r in zip(given, inputs)] + [(f"the work directory {a.work or work}", work)]
    for what, path in named:
        if inside(path, mesh_dir):
            ap.error(f"{what} is {mesh_dir} or lies inside it, and the build removes that folder")
        if path in (side, archive):
            ap.error(f"{what} is {path}, a file the build replaces")
    for p, r in zip(given, inputs):
        if os.path.isdir(r) and inside(mesh_dir, r) and mesh_dir != r:
            ap.error(f"the charts folder {p} holds {mesh_dir}, and the build removes that folder")
        if os.path.isdir(r) and inside(work, r):
            ap.error(f"the charts folder {p} holds the work directory {work}, where the build unpacks charts: "
                     f"they would be read as that folder's own")
    return inputs, work, out


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


def build_mesh(name, out, work, workers, open_charts, chart_meta=None, archive=False, keep=False, log=print):
    """One build, into the output directory `out`, working in a new folder of its own inside `work` (both
    resolved: resolve_paths). open_charts(run folder, log) gives the cells: an object with cells(), the
    records mesh/build.py cell_index takes, and record(index) for the sidecar; main gives mesh/cells.py Charts,
    which reads them from the charts. Returns the exit status."""
    started = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.time()
    mesh_dir, side_path, archive_path = outputs(out, name)
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    # Nothing of an earlier build of this name may be taken for this one's result.
    for p in (side_path, archive_path):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(mesh_dir):
        shutil.rmtree(mesh_dir)
    if not build_cutter(log):
        return 1
    run_dir = tempfile.mkdtemp(prefix="run-", dir=work)
    status = 1
    try:
        status = stages(name, out, run_dir, workers, open_charts, chart_meta, archive, started, t0, log)
        return status
    finally:
        if status == 0 and not keep:
            shutil.rmtree(run_dir)
        else:
            log(f"build-mesh: the run's files are kept in {run_dir}")


def stages(name, out, run_dir, workers, open_charts, chart_meta, archive, started, t0, log):
    """The stages of one build in its run folder: the charts, the tiles, the second stage, the sidecar."""
    from . import build
    mesh_dir, side_path, archive_path = outputs(out, name)
    try:
        charts = open_charts(run_dir, log)
        cells = build.cell_index(charts.cells(), workers)
        if not cells:
            raise RuntimeError("the charts hold no live cell with charted areas: there is no tile to build")
        charts_record = charts.record(cells)
        failed = build.run(run_dir, workers, cells, log=log)
    except RuntimeError as ex:               # the charts cannot be read as given (mesh/cells.py ChartsError), or a
        log(f"ERROR: {ex}")                  # cell is not one this build reads (mesh/build.py cell_index, run)
        return 1
    if failed:
        log("the tile stage failed")
        return 1
    r = subprocess.run([sys.executable, "-m", "mesh.finalize", run_dir, mesh_dir, str(workers), name],
                       cwd=os.path.dirname(HERE))
    if r.returncode != 0:
        log(f"build-mesh: the second stage stopped (status {r.returncode}) after {time.time() - t0:.0f} s; "
            f"no sidecar written")
        return 2

    with open(os.path.join(mesh_dir, "index.json")) as f:
        idx = json.load(f)
    nbytes = sum(os.path.getsize(os.path.join(mesh_dir, t["file"])) for t in idx["tiles"])
    side = {
        "name": name,
        "mesh_dir": f"{name}_mesh",
        "format": {"magic": MESH_FORMAT_MAGIC, "index_version": MESH_INDEX_VERSION,
                   "tile_deg": TILE_DEG, "multi_cluster": False},
        "build_date": started,
        "build_seconds": round(time.time() - t0, 1),
        "builder_version": project_version(),
        "workers": workers,
        "charts": charts_record,
        "chart_build_date": (chart_meta or {}).get("build_date"),
        "chart_metadata": chart_meta,
        "tile_stage": tile_stage_record(run_dir),
        "clusters": [{"dir": ".", "box": [idx["west"], idx["south"], idx["east"], idx["north"]],
                      "x_scale": idx["xScale"], "triangles": idx["triangles"], "mesh_tiles": len(idx["tiles"])}],
        "triangles": idx["triangles"],
        "mesh_tiles": len(idx["tiles"]),
        "bytes": nbytes,
        # The second stage exits non-zero on any failed check (mesh/finalize.py lists them), and then no
        # sidecar is written: a sidecar exists only for a mesh that passed every one.
        "passed": True,
    }
    if archive:
        try:
            arc = make_archive(out, f"{name}_mesh", log)
        except RuntimeError as ex:
            log(f"ERROR: {ex}; no sidecar written")
            return 1
        log(f"archive: {arc} ({os.path.getsize(arc) / 1e6:.0f} MB)")
    # The sidecar last: it says the build is whole, so it is there only when everything else is.
    with open(side_path, "w") as f:
        json.dump(side, f, indent=1)
    log(f"sidecar: {side_path}")
    log(f"build-mesh: {name}: {side['triangles']:,} triangles in {side['mesh_tiles']} tiles, "
        f"{nbytes / 1e6:.0f} MB, {time.time() - t0:.0f} s")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build-mesh.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("charts", nargs="+", help="the district's charts: NOAA zip(s) of S-57 cells, or folder(s) of them")
    ap.add_argument("--name", required=True, help="output name, e.g. 01CGD")
    ap.add_argument("-o", "--out", default="data/tiles", help="output directory (default data/tiles)")
    ap.add_argument("--work", help="work directory (default data/mesh/<name>); each run makes its own folder in it")
    ap.add_argument("-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--chart-json", metavar="FILE",
                    help="the published chart's metadata JSON; its build_date is recorded as chart_build_date")
    ap.add_argument("--archive", action="store_true", help="also write <name>_mesh.tar.zst (needs tar --zstd)")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="keep the run's folder (the unpacked charts, the extracted layers, the cut of every "
                         "tile) after a successful build")
    a = ap.parse_args(argv)
    chart_meta = None
    if a.chart_json:
        try:
            with open(a.chart_json) as f:
                chart_meta = json.load(f)
        except (OSError, ValueError) as ex:
            ap.error(f"--chart-json {a.chart_json}: {ex}")
    inputs, work, out = resolve_paths(ap, a)

    def log(msg=""):
        print(msg, flush=True)

    def open_charts(run_dir, log_):
        from . import cells
        return cells.Charts(inputs, run_dir, a.workers, log_)

    return build_mesh(a.name, out, work, a.workers, open_charts, chart_meta=chart_meta, archive=a.archive,
                      keep=a.keep_intermediate, log=log)
