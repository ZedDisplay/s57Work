"""build-mesh.py — build the navigation mesh of one or more chart files.

    build-mesh.py 01CGD_ENCs.mbtiles --land land_polygons.shp [-o data/tiles]

Runs the four stages of the experiment in order, with the same
intermediate files, under the work directory (default data/mesh/<name>/):

    z16_layers/<LAYER>.pkl        stage 1  decoded z16 tiles
    build[/<cluster>]/            stage 3  tile_*.npz, mesh_*.npz, parts.npz, summary.json
    final[/<cluster>]/            stage 4a finalized mesh_*.npz
    <out>/<name>_mesh/            stage 4b binary tiles + index.json (the plugin's meshDir)
    <out>/<name>_mesh.json                 sidecar: provenance, extent, counts, checks
    <out>/<name>_mesh.tar.zst              with --archive

The extent is the rectangle around every 0.25° cell holding a z16 tile
(cells counted from -180°+0.000125°, -90°+0.000125°, so that no tile
seam lies on the sounding grid's 0.00025° lattice; see mesh/extent.py);
a district whose cells form separate longitude groups (more than 4° of
empty columns apart) is built as one mesh per group, each in its own
sub-folder of <name>_mesh/ listed in <name>_mesh/meshes.json.

Exit status: 0 built and every gating requirement passed; 2 built but a
gating requirement failed (the files and the intermediates are left in
place; --allow-fail turns this into 0); 1 the build itself failed.
Gating requirements are all of the experiment's checks except "7 angles
>= 20 deg" and "2 layers loaded", which are reported as warnings, plus
the finalize reverse-edge check, "15 chart coverage loaded" (M_COVR
with CATCOV=1 in the decoded input, also when --reuse-decoded reads an
old z16_layers/) and "no z16 tile failed to decode".
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import MESH_FORMAT_MAGIC, MESH_INDEX_VERSION, decode, extent, finalize, binary

WARN_ONLY = ("2 ", "7 ")          # requirement names that never gate


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def land_date(shp):
    """The data date from the osmdata.openstreetmap.de package README
    beside the shapefile, or None."""
    readme = os.path.join(os.path.dirname(os.path.abspath(shp)), "README.txt")
    if not os.path.exists(readme):
        return None
    m = re.search(r"Date of the data used is (\S+)", open(readme).read())
    return m.group(1) if m else None


def default_name(first_input):
    stem = os.path.splitext(os.path.basename(first_input))[0]
    return stem[:-5] if stem.endswith("_ENCs") else stem


def project_version():
    try:
        import tomllib
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return None


def make_archive(out_dir, folder, log):
    archive = os.path.join(out_dir, folder + ".tar.zst")
    if os.path.exists(archive):
        os.remove(archive)
    cmd = ["tar", "--zstd", "-cf", archive, "-C", out_dir, folder]
    log("archive: " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tar failed ({r.returncode}): {r.stderr.strip()}")
    return archive


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build-mesh.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbtiles", nargs="+", help="chart file(s); several are decoded as separate sources")
    ap.add_argument("--land", help="OSM land_polygons.shp (osmdata.openstreetmap.de split package)")
    ap.add_argument("-o", "--out", default="data/tiles", help="output directory (default data/tiles)")
    ap.add_argument("--name", help="output name; default: first input's stem without _ENCs (01CGD_ENCs -> 01CGD)")
    ap.add_argument("--work", help="work directory (default data/mesh/<name>)")
    ap.add_argument("-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="worker processes for decode and tiles (default cpu count - 1)")
    ap.add_argument("--reuse-decoded", action="store_true",
                    help="skip stage 1 when <work>/z16_layers already holds pickles")
    ap.add_argument("--only-coverage", action="store_true",
                    help="mesh only water inside the input's own coverage (M_COVR CATCOV 1); for bathymetry "
                         "layers, whose data stops at their coverage edge. Districts build without it.")
    ap.add_argument("--snap-discs", action="store_true",
                    help="move disc corners within 2 m of another line onto it (off, as in the kept experiment build)")
    ap.add_argument("--debug-point", action="append", default=[], metavar="LON,LAT",
                    help="dump the faces and layers at this point to build/debug_<i>_<j>.json (repeatable)")
    ap.add_argument("--chart-json", action="append", default=[], metavar="FILE",
                    help="the chart's release .json; its build_date is recorded as chart_build_date")
    ap.add_argument("--archive", action="store_true", help="also write <name>_mesh.tar.zst")
    ap.add_argument("--allow-fail", action="store_true", help="exit 0 even when a gating requirement fails")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="keep z16_layers/, build/ and final/ (default: removed when every gating requirement passes, kept otherwise)")
    ap.add_argument("--box", metavar="W,S,E,N",
                    help="diagnostic: mesh exactly this rectangle (tile grid from its south-west corner, "
                         "as the experiment's D1 box) instead of the rectangle derived from the z16 cells")
    ap.add_argument("--tile-deg", type=float, default=extent.TILE_DEG, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    for p in a.mbtiles:
        if not os.path.exists(p):
            ap.error(f"no such file: {p}")
    if a.land and not os.path.exists(a.land):
        ap.error(f"no such file: {a.land}")
    name = a.name or default_name(a.mbtiles[0])
    work = a.work or os.path.join("data", "mesh", name)
    os.makedirs(work, exist_ok=True)
    os.makedirs(a.out, exist_ok=True)
    logf = open(os.path.join(work, "build.log"), "a")

    def log(msg=""):
        print(msg, flush=True)
        logf.write(msg + "\n"); logf.flush()

    t_start = time.time()
    started = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"build-mesh {started}: {', '.join(a.mbtiles)} -> {a.out}/{name}_mesh  (work {work}, workers {a.workers})")
    if not a.land:
        log("WARNING: no --land: land comes from the charts' LNDARE only")

    # ---- stage 1: decode z16
    pkl = os.path.join(work, "z16_layers")
    if a.reuse_decoded and os.path.isdir(pkl) and any(f.endswith(".pkl") for f in os.listdir(pkl)):
        log(f"stage 1: reusing decoded layers in {pkl}")
        dec = {"tiles": None, "failed": [], "pieces": None, "reused": True}
    else:
        if os.path.isdir(pkl):
            shutil.rmtree(pkl)
        log("stage 1: decoding z16 tiles")
        dec = decode.decode_to_pickles(a.mbtiles, pkl, a.workers, log=log)

    # ---- stage 2: extent
    cells = decode.z16_cells(a.mbtiles, a.tile_deg, lambda lon, lat: extent.cell(lon, lat, a.tile_deg))
    if not cells:
        log("ERROR: the input has no z16 tiles; nothing to mesh")
        return 1
    if a.box:
        cl = [extent.Cluster.from_box([float(v) for v in a.box.split(",")], a.tile_deg)]
        log(f"stage 2: --box override; {len(cells)} cells of {a.tile_deg} deg hold z16 tiles")
    else:
        cl = extent.clusters(cells, a.tile_deg)
        log(f"stage 2: {len(cells)} cells of {a.tile_deg} deg hold z16 tiles -> {len(cl)} cluster(s)")
    for c in cl:
        log(f"  cluster {c.slug}: box {c.box}  {c.nx}x{c.ny} = {c.nx*c.ny} tiles ({len(c.cells)} with z16)  K {c.k:.6f}")

    # ---- stages 3 and 4 per cluster
    from . import build as B                                # imports numpy/shapely/triangle
    mesh_dir = os.path.join(a.out, f"{name}_mesh")
    if os.path.isdir(mesh_dir):
        shutil.rmtree(mesh_dir)
    os.makedirs(mesh_dir)
    multi = len(cl) > 1
    source = [os.path.basename(p) for p in a.mbtiles]
    debug_points = [tuple(float(v) for v in s.split(",")) for s in a.debug_point]
    results = []
    failures = []
    warnings = []
    for c in cl:
        sub = c.slug if multi else ""
        bdir = os.path.join(work, "build", sub) if sub else os.path.join(work, "build")
        fdir = os.path.join(work, "final", sub) if sub else os.path.join(work, "final")
        odir = os.path.join(mesh_dir, sub) if sub else mesh_dir
        for d in (bdir, fdir):
            if os.path.isdir(d):
                shutil.rmtree(d)
        log(f"\nstage 3: build {c.slug} -> {bdir}")
        config = dict(box_=c.box, out=bdir, land=a.land, pkl=pkl, tile=a.tile_deg, snap=a.snap_discs,
                      debug_points=debug_points, only_coverage=a.only_coverage)
        try:
            # Two fresh interpreters: the tile stage, then the global
            # refinement, so the refinement (the memory peak) does not
            # inherit the tile stage's heap. Their output goes to stdout and
            # to the same build.log as this process.
            summ = B.run_fresh(config, a.workers, log_path=os.path.join(work, "build.log"))
        except B.BuildError as ex:
            log(f"ERROR: {ex}")
            return 1
        log(f"\nstage 4a: finalize {c.slug} -> {fdir}")
        fin = finalize.finalize(bdir, fdir, log=log)
        log(f"\nstage 4b: binary tiles {c.slug} -> {odir}")
        idx = binary.write_binary(fdir, odir, c.box, a.tile_deg, source, log=log)
        for rname, passed, detail in summ["requirements"]:
            if not passed:
                (warnings if rname.startswith(WARN_ONLY) else failures).append((c.slug, rname, detail))
        if fin["links_bad_reverse"]:
            failures.append((c.slug, "reverse edges", f"{fin['links_bad_reverse']} links whose reverse edge does not point back"))
        results.append({"cluster": c.slug, "dir": sub or ".", **c.describe(),
                        "triangles": idx["triangles"], "vertices": summ["vertices"],
                        "tiles_built": summ["tiles"], "mesh_tiles": len(idx["tiles"]),
                        "bytes": idx["bytes"], "water_parts": summ["water_parts"],
                        "layers": summ["layers"], "peak_rss_mb": summ["peak_rss_mb"],
                        "timings": {**summ["timings"], "total": summ["t_total"],
                                    "finalize": fin["seconds"]},
                        "requirements": [{"name": n, "pass": p, "detail": d} for n, p, d in summ["requirements"]],
                        "not_covered_examples": summ["not_covered_examples"]})
    if multi:
        with open(os.path.join(mesh_dir, "meshes.json"), "w") as f:
            json.dump({"version": 1, "name": name,
                       "meshes": [{"dir": r["dir"], "box": r["box"], "triangles": r["triangles"]} for r in results]},
                      f, indent=1)
    if dec["failed"]:
        # A tile that does not decode drops its hazards, depth areas and land
        # silently; nothing downstream can detect that, so it gates.
        failures.append(("decode", "tiles failed to decode",
                         f"{len(dec['failed'])}: {dec['failed'][:5]}"))

    # ---- sidecar
    chart_meta = []
    for p in a.chart_json:
        try:
            with open(p) as f:
                chart_meta.append(json.load(f))
        except Exception as ex:
            warnings.append(("chart-json", p, str(ex)))
    build_dates = sorted(m.get("build_date") for m in chart_meta if m.get("build_date"))
    side = {
        "name": name,
        "mesh_dir": f"{name}_mesh",
        "format": {"magic": MESH_FORMAT_MAGIC, "index_version": MESH_INDEX_VERSION,
                   "tile_deg": a.tile_deg, "multi_cluster": multi},
        "build_date": started,
        "build_seconds": round(time.time() - t_start, 1),
        "builder_version": project_version(),
        "workers": a.workers,
        "snap_discs": a.snap_discs,
        "only_coverage": a.only_coverage,
        "box_override": a.box,
        "sources": [{"file": os.path.basename(p), "bytes": os.path.getsize(p), "sha256": sha256(p)}
                    for p in a.mbtiles],
        "chart_build_date": build_dates[-1] if build_dates else None,
        "chart_metadata": chart_meta or None,
        "land_polygons": {"file": os.path.basename(a.land), "date": land_date(a.land)} if a.land else None,
        "z16_tiles_decoded": dec["tiles"],
        "z16_cells": len(cells),
        "clusters": results,
        "triangles": sum(r["triangles"] for r in results),
        "mesh_tiles": sum(r["mesh_tiles"] for r in results),
        "bytes": sum(r["bytes"] for r in results),
        "gating_failures": [{"cluster": c, "name": n, "detail": d} for c, n, d in failures],
        "warnings": [{"cluster": c, "name": n, "detail": d} for c, n, d in warnings],
        "passed": not failures,
    }
    side_path = os.path.join(a.out, f"{name}_mesh.json")
    with open(side_path, "w") as f:
        json.dump(side, f, indent=1)
    log(f"\nsidecar: {side_path}")

    if a.archive:
        arc = make_archive(a.out, f"{name}_mesh", log)
        log(f"archive: {arc} ({os.path.getsize(arc)/1e6:.0f} MB)")

    if not a.keep_intermediate and not failures:
        for d in ("z16_layers", "build", "final"):
            shutil.rmtree(os.path.join(work, d), ignore_errors=True)
    elif failures and not a.keep_intermediate:
        log(f"intermediates kept in {work} for diagnosis (a gating requirement failed)")

    log(f"\n==== {name}: {side['triangles']:,} triangles in {side['mesh_tiles']} tiles, "
        f"{side['bytes']/1e6:.0f} MB, {time.time()-t_start:.0f} s")
    for c, n, d in warnings:
        log(f"WARN  [{c}] {n}: {d}")
    for c, n, d in failures:
        log(f"FAIL  [{c}] {n}: {d}")
    if failures:
        log(f"{len(failures)} gating requirement(s) failed" + (" (--allow-fail)" if a.allow_fail else ""))
        return 0 if a.allow_fail else 2
    log("all gating requirements passed")
    return 0
