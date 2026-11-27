"""Shared test support: loads the hyphenated scripts as modules, locates
the fixture, and reports which external tools are present so the
end-to-end tests can skip cleanly on a machine without GDAL/tippecanoe."""
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ZIP = ROOT / "tests" / "fixtures" / "mini-district.zip"
# Cells in the fixture (see tests/fixtures/make-fixture.py): a band 4 cell
# with three band 5 cells nested inside it, and one cancelled band 5 cell.
FIXTURE_BAND4 = "US4NY1BY"          # -71.7..-71.4, 40.8..41.1
FIXTURE_BAND5 = ("US5RI1AC", "US5RI1AD", "US5RI1AE")  # 41.025..41.1
FIXTURE_CANCELLED = "US5NJ30M"       # DSID EDTN=0 after its .001 update

_modules = {}


def load_script(filename: str):
    """Import a top-level script (hyphens in the name) as a module."""
    if filename not in _modules:
        spec = importlib.util.spec_from_file_location(
            filename.replace("-", "_").replace(".py", ""), ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _modules[filename] = mod
    return _modules[filename]


def pipeline():
    return load_script("s57-to-mbtiles.py")


def counter():
    return load_script("count-layer-by-zoom.py")


def mesh_catalog():
    return load_script("mesh-catalog.py")


HAVE_GDAL = bool(shutil.which("ogr2ogr") and shutil.which("ogrinfo"))
HAVE_TIPPECANOE = bool(shutil.which("tippecanoe") and shutil.which("tile-join")
                       and shutil.which("tippecanoe-decode"))
HAVE_PMTILES = bool(shutil.which("pmtiles"))
# The mesh build compiles mesh/exact_cut.cpp: a C++ compiler and CGAL's
# headers. Where both are present a compile failure fails the test.
HAVE_CGAL = bool((shutil.which("c++") or shutil.which("g++") or shutil.which("clang++"))
                 and any((Path(d) / "CGAL").is_dir() for d in
                         ("/usr/include", "/usr/local/include", "/opt/homebrew/include")))

def shapes_cell(folder, band, cell, layers):
    """A hand-made cell: write its layers ({layer: [GeoJSON features]}) as the files the mesh build's first
    stage extracts from a chart (one GeoJSON file per layer), and return [band, cell, {layer: file}]."""
    d = Path(folder) / cell
    d.mkdir(parents=True)
    files = {}
    for layer, features in layers.items():
        (d / f"{layer}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        files[layer] = str(d / f"{layer}.geojson")
    return [band, cell, files]


def build_from_shapes(base, name, cells, chart_json=None, archive=False, keep=False):
    """Run the mesh build on hand-made cells (shapes_cell): everything build-mesh.py does after it has read
    the charts, through the same function (mesh.cli.build_mesh), in its own process
    (tests/build_from_shapes.py). Shapes cannot be written as S-57 cells here, so these builds do not go
    through the chart reader; tests/test_mesh_pipeline.py and tests/test_mesh_cells.py do, on real cells.
    Output in base/out, work in base/work. Returns the finished process."""
    base = Path(base)
    spec = base / f"{name}-spec.json"
    spec.write_text(json.dumps({"name": name, "out": str(base / "out"), "work": str(base / "work"), "workers": 2,
                                "chart_json": chart_json, "archive": archive, "keep": keep,
                                "cells": list(cells)}))
    return subprocess.run([sys.executable, "-m", "tests.build_from_shapes", str(spec)],
                          cwd=ROOT, capture_output=True, text=True)


def run_build_mesh(cwd, *args):
    """Run build-mesh.py in cwd."""
    return subprocess.run([sys.executable, str(ROOT / "build-mesh.py"), *args],
                          cwd=str(cwd), capture_output=True, text=True)


MESH_HEADER = 32
# corners f64[6], neighbours i32[3], mult, depth, clear, hazv f32, rev i8[3], flags u8,
# sideFlags u8[3], sideClear f32[3], sideHazv f32[3], cornerFlags u8[3], cornerVal f32[3]
MESH_BYTES_PER_TRIANGLE = 48 + 12 + 4 + 4 + 4 + 4 + 3 + 1 + 3 + 12 + 12 + 3 + 12


def read_mesh(mesh_dir: Path) -> dict:
    """A mesh folder (index.json + WRPMESH2 tiles, docs/MESH.md) as one set
    of arrays over all its triangles, in id order; `lonlat` is the corners
    in degrees as stored (x divided by the index's xScale: between -180 and
    180, also in a mesh across the date line); `tile_of` is each triangle's
    place in the index's tile list. Checks each tile's magic, count, origin
    and exact length on the way. Needs numpy."""
    import struct
    import numpy as np
    idx = json.loads((mesh_dir / "index.json").read_text())
    cols = {k: [] for k in ("corners", "neighbours", "mult", "depth", "clear", "hazv", "rev", "flags",
                            "side_flags", "side_clear", "side_hazv", "corner_flags", "corner_val")}
    first = 0
    for t in idx["tiles"]:
        raw = (mesh_dir / t["file"]).read_bytes()
        n = t["n"]
        assert raw[:8] == b"WRPMESH2", t["file"]
        assert len(raw) == MESH_HEADER + MESH_BYTES_PER_TRIANGLE * n, t["file"]
        count, ox, oy = struct.unpack("<Idd", raw[8:28])
        assert count == n, t["file"]
        # columns count east from the index's west; a tile east of the date line has its origin from -180
        want = idx["west"] + t["i"] * idx["tileDeg"]
        assert abs(ox - (want - 360 if want >= 180 else want)) < 1e-9, t["file"]
        assert abs(oy - (idx["south"] + t["j"] * idx["tileDeg"])) < 1e-9, t["file"]
        assert t["first"] == first, "ids are contiguous per tile in file order"
        first += n
        o = MESH_HEADER
        for key, dtype, per in (("corners", "<f8", 6), ("neighbours", "<i4", 3), ("mult", "<f4", 1),
                                ("depth", "<f4", 1), ("clear", "<f4", 1), ("hazv", "<f4", 1), ("rev", "i1", 3),
                                ("flags", "u1", 1), ("side_flags", "u1", 3), ("side_clear", "<f4", 3),
                                ("side_hazv", "<f4", 3), ("corner_flags", "u1", 3), ("corner_val", "<f4", 3)):
            a = np.frombuffer(raw, dtype, n * per, o)
            o += a.nbytes
            cols[key].append(a.reshape(n, per) if per > 1 else a)
    m = {k: np.concatenate(v) for k, v in cols.items()}
    m["tile_of"] = np.concatenate([np.full(t["n"], k, dtype=np.int64) for k, t in enumerate(idx["tiles"])])
    m["corners"] = m["corners"].reshape(-1, 3, 2)
    m["lonlat"] = m["corners"] / np.array([idx["xScale"], 1.0])
    m["index"] = idx
    return m


def mesh_triangle_at(m: dict, lon: float, lat: float):
    """The id of the mesh triangle holding a point, or None (not water)."""
    import numpy as np
    p = m["lonlat"]
    a, b, c = p[:, 0], p[:, 1], p[:, 2]

    def side(u, v):
        return (v[:, 0] - u[:, 0]) * (lat - u[:, 1]) - (v[:, 1] - u[:, 1]) * (lon - u[:, 0])
    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    hit = np.flatnonzero((d1 >= 0) & (d2 >= 0) & (d3 >= 0))
    return int(hit[0]) if len(hit) else None


def pmtiles_metadata(archive: Path) -> dict:
    """The metadata JSON of a PMTiles archive via `pmtiles show`."""
    out = subprocess.run(["pmtiles", "show", str(archive), "--metadata"],
                         capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}


def decode_all(tileset: Path) -> dict:
    """{(z, x, y): sha256 of the tile's decoded features} for every tile
    plus the tileset's metadata, via a whole-file tippecanoe-decode. Works
    on .mbtiles and .pmtiles alike, so the two can be compared."""
    import hashlib
    out = subprocess.run(["tippecanoe-decode", str(tileset)],
                         capture_output=True, text=True, check=True)
    fc = json.loads(out.stdout)
    tiles = {}
    for t in fc.get("features", []):
        p = t["properties"]
        digest = hashlib.sha256(
            json.dumps(t.get("features", []), sort_keys=True).encode()).hexdigest()
        tiles[(p["zoom"], p["x"], p["y"])] = digest
    return {"tiles": tiles, "metadata": fc.get("properties", {})}


def decode_tile(mbtiles: Path, z: int, x: int, y: int, layer=None) -> dict:
    """{layer: [feature, ...]} for one tile via tippecanoe-decode (XYZ)."""
    cmd = ["tippecanoe-decode"]
    if layer:
        cmd += ["-l", layer]
    cmd += [str(mbtiles), str(z), str(x), str(y)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    fc = json.loads(out.stdout)
    layers = {}
    for f in fc.get("features", []):
        if f.get("type") == "FeatureCollection":
            name = f.get("properties", {}).get("layer", "?")
            layers.setdefault(name, []).extend(f.get("features", []))
    return layers


def metadata(mbtiles: Path) -> dict:
    db = sqlite3.connect(str(mbtiles))
    try:
        return dict(db.execute("SELECT name, value FROM metadata").fetchall())
    finally:
        db.close()


def run_pipeline(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run s57-to-mbtiles.py in cwd (its data/ dir lands there)."""
    return subprocess.run(
        [sys.executable, str(ROOT / "s57-to-mbtiles.py"), *args],
        cwd=str(cwd), capture_output=True, text=True)
