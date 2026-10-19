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
