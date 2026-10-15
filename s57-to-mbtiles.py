#!/usr/bin/env python3 -u
"""
s57-to-mbtiles.py — Convert S-57 ENC charts to vector MBTiles

Takes one or more ZIP files (or directories) of S-57 ENC files (.000) and
produces a single merged vector MBTiles file.

─────────────────────────────────────────────────────────────────────────────
MODES
─────────────────────────────────────────────────────────────────────────────

SINGLE SOURCE:
  %(prog)s NY_ENCs.zip
  %(prog)s NY_ENCs.zip -o ny-charts.mbtiles --minzoom 9 --maxzoom 16

TWO-SOURCE MERGE (coarse + detail):
  %(prog)s region03.zip RI_detail.zip --split 12 -o ri-merged.mbtiles

MULTI-SOURCE (explicit zoom ranges):
  %(prog)s --sources region03.zip:9-11 RI_detail.zip:12-16 -o merged.mbtiles

BY-BAND (recommended for multi-state regions):
  %(prog)s CT_ENCs.zip RI_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ny.mbtiles

SKIP GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12

─────────────────────────────────────────────────────────────────────────────
PIPELINE (per band or source)
  1. Extract ZIPs → data/enc/
  1b. Drop cancelled cells (S-57 DSID EDTN=0 after updates) → data/cancelled-cells.json
  2. ogr2ogr (native or container) → data/geojson/
  2b. (by-band) Clip legacy cells under reschemed cells of the same band
      → data/geojson/<band>.resolved/  (reschemed cell wins)
  3. Consolidate per-layer GeoJSON → data/merged/
  3b. (by-band) Erase finer charts' footprints from a band's extended
      zooms → data/merged/<band>.minus-<finer>/  (finer chart wins)
  4. tippecanoe (one per band and zoom group) → data/tiles/
  5. tile-join → final .mbtiles

All artifacts stored in ./data/, nothing deleted between runs.
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GDAL_IMAGE = "ghcr.io/osgeo/gdal:alpine-small-latest"
SKIP_LAYERS = {"DSID", "C_AGGR", "C_ASSO", "Generic"}
DATA_DIR = Path("data")

BAND_ZOOM: Dict[int, Tuple[int, int, str, str]] = {
    1: (7,  8,  "overview",  "~1:3,500,000"),
    2: (9,  10, "general",   "~1:700,000"),
    3: (11, 12, "coastal",   "~1:90,000"),
    4: (13, 14, "approach",  "~1:22,000"),
    5: (15, 16, "harbour",   "~1:8,000"),
    6: (17, 18, "berthing",  "~1:3,000"),
}

# How many zoom levels past its native ceiling each band renders (capped
# at the global maxzoom). See the comment at the effective_max computation
# in process_by_band for the rationale and cost history.
BAND_ZOOM_EXTENSION = 2

# How many zoom levels below its native floor each band also renders
# (capped at the global minzoom). There it ranks like an extension, by
# its band (priority_at): the native band for the zoom still wins, but
# in a hole the finer chart beats a coarser band's extension, because a
# fine chart drawn small only gets denser while a coarse chart drawn
# large is wrong by its generalisation (band 1 at z10 is a 1:3.5M
# coastline). NOAA's rescheming leaves bands missing under finer ones —
# 05CGD's band 2 grid has no cell for 81.6-76.8 W x 33.6-38.4 N once
# legacy US2EC02M was cancelled, so Cape Fear to New Bern had band 3
# only and z10 was blank; 178 z14 tiles across the districts had band 5
# but no band 3 or 4 (2026-10-15 hole scan of the R2 files). Upward
# extension alone cannot bridge a missing band from above. A band whose
# native floor is above the global maxzoom (band 6) is still skipped.
BAND_ZOOM_DOWNWARD = 2

# Zoom presence of point layers inside a source's zoom range.
#
# Every layer renders from the bottom zoom of its source (a band, or a
# gap fill). Until 2026-09-07 the aids-to-navigation, hazard and SOUNDG
# layers were held back one zoom from a band's native bottom (a +1
# offset added in 0.6.0 as a density control), and tippecanoe's default
# point drop rate (2.5 per zoom below a run's top zoom) thinned every
# point layer at the bottom zoom of every run. Once the finer-wins erase
# (Stage 3b) removed the coarser band under finer coverage, the odd
# zooms had nothing left: band 3's extension is erased under band 4 at
# z13 while band 4's own soundings began at z14, so the published
# district files carried SOUNDG only at even zooms, and lights, buoys,
# beacons, wrecks, obstructions and rocks vanished at z11/13/15 the same
# way. Nobody re-measured the odd zooms after the erase landed.
#
# Now: in by-band mode tippecanoe never drops points (--drop-rate=1);
# zoom presence is decided here, per feature, through the
# `tippecanoe.minzoom` extension stamped during consolidation and
# re-applied after the erase (tippecanoe silently ignores a per-layer
# minzoom in the -L spec, verified v2.78.0). Only SOUNDG is dense enough
# to need gating (ZoomRule, soundg_rule):
#   * bands 1-2 (overview, general): soundings only at the band's top
#     zoom, z8 and z10, unchanged;
#   * band 3 and finer, and gap fills: soundings from the source's
#     bottom zoom, never below SOUNDG_MIN_ZOOM, and at that bottom zoom
#     only one in SOUNDG_THIN_RATE of them (the rate tippecanoe applied
#     until now), so a z13 tile carries about 1.6x the soundings of a
#     z14 tile (four z14 tiles' worth, thinned 2.5x) rather than four
#     times as many. Which soundings survive at the thinned zoom is a
#     hash of the sounding's identity (LNAM and position), so the pick
#     is identical in the pristine and the erased copy of a layer and
#     stable from run to run.
# Plain single-source mode has no band context: no stamps, and
# tippecanoe's default drop rate remains the only thinning there.
SOUNDG_MIN_ZOOM = BAND_ZOOM[2][1]   # z10: never below the general band's top
SOUNDG_THIN_RATE = 2.5
SOUNDG_OVERVIEW_BANDS = (1, 2)


@dataclass(frozen=True)
class ZoomRule:
    """Per-feature minzoom for one layer of one source. Features get
    `minzoom`; with thin_rate set, only one in thin_rate of them does and
    the rest get minzoom + 1."""
    minzoom: int
    thin_rate: Optional[float] = None

    def as_json(self) -> list:
        """[minzoom, thin_rate]: the form stored in the merged-dir
        source marker so a rule change invalidates the merged layer."""
        return [self.minzoom, self.thin_rate]


def soundg_rule(band: Optional[int], bottom_zoom: int) -> ZoomRule:
    """SOUNDG rule for a source whose data is of usage band `band` (None
    when unknown) rendering from bottom_zoom (a band's NATIVE bottom, a
    gap fill's configured bottom — config-static, so cached merges stay
    valid across CLI zoom arguments)."""
    if band in SOUNDG_OVERVIEW_BANDS:
        return ZoomRule(BAND_ZOOM[band][1])
    return ZoomRule(max(bottom_zoom, SOUNDG_MIN_ZOOM), SOUNDG_THIN_RATE)


def layer_zoom_rules(band: Optional[int],
                     bottom_zoom: int) -> Dict[str, ZoomRule]:
    """Layer name -> ZoomRule for a by-band source. Layers not listed
    render from the source's bottom zoom untouched."""
    return {"SOUNDG": soundg_rule(band, bottom_zoom)}


def feature_minzoom(feat: dict, rule: ZoomRule) -> int:
    """The minzoom rule assigns this feature. A thinned rule keeps one in
    thin_rate features at rule.minzoom by hashing the feature's identity
    — its S-57 LNAM plus its 2-D position, since soundings split from one
    MultiPoint share the LNAM — and puts the others one zoom up."""
    if rule.thin_rate is None:
        return rule.minzoom
    props = feat.get("properties") or {}
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    # First vertex, 2-D, rounded so the GDAL round trip of the erase
    # (which re-serialises coordinates) hashes the same.
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    pos = ""
    if isinstance(coords, list) and len(coords) >= 2:
        pos = f"{round(coords[0], 6)},{round(coords[1], 6)}"
    key = f"{props.get('LNAM', '')}|{pos}"
    bucket = zlib.crc32(key.encode("utf-8")) % 10000
    return rule.minzoom if bucket * rule.thin_rate < 10000 else rule.minzoom + 1


def stamp_feature(feat: dict, rule: Optional[ZoomRule]) -> None:
    """Apply rule to feat in place as the tippecanoe.minzoom extension."""
    if rule is None or not isinstance(feat, dict):
        return
    ext = feat.get("tippecanoe")
    ext = dict(ext) if isinstance(ext, dict) else {}
    ext["minzoom"] = feature_minzoom(feat, rule)
    feat["tippecanoe"] = ext

# Gap-fill config lives in enc-sources.yaml under the `gap_fills:` key.
# See the comment block in that file for full background on what gap-fill
# is, why it's needed (NOAA legacy ENC discontinuities, addressed by the
# in-progress ENC Rescheming Project), and how to add new gaps.
GAP_FILL_CONFIG_FILE = "enc-sources.yaml"


@dataclass
class GapFillGroup:
    """One group of cells to render at a custom zoom range as a gap fill.
    Loaded from the `gap_fills:` list in enc-sources.yaml."""
    name: str
    zoom_range: Tuple[int, int]
    cells: List[str]
    description: str = ""


def load_gap_fill_config(
    config_path: Optional[Path] = None,
) -> List[GapFillGroup]:
    """Load the list of gap-fill groups from enc-sources.yaml.

    Returns an empty list (gap-fill becomes a no-op) if the file is
    missing, has no `gap_fills:` section, or pyyaml is unavailable.

    Accepts both the current list-of-groups schema (multiple named
    groups, each with its own zoom_range) and the legacy flat schema
    (a single zoom_range + cells dict), to keep older configs working.
    """
    if config_path is None:
        candidates = [
            Path.cwd() / GAP_FILL_CONFIG_FILE,
            Path(__file__).resolve().parent / GAP_FILL_CONFIG_FILE,
        ]
        config_path = next((p for p in candidates if p.exists()), None)
    if config_path is None or not config_path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        print("WARNING: pyyaml not installed; skipping gap-fill config",
              file=sys.stderr)
        return []
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    section = data.get("gap_fills")
    if not section:
        return []

    groups: List[GapFillGroup] = []
    if isinstance(section, list):
        # Current schema: list of groups
        for i, g in enumerate(section):
            cells = list(g.get("cells") or [])
            zr = g.get("zoom_range") or [9, 10]
            if not cells:
                continue
            groups.append(GapFillGroup(
                name=g.get("name") or f"gapfill{i}",
                zoom_range=(int(zr[0]), int(zr[1])),
                cells=cells,
                description=g.get("description", ""),
            ))
    elif isinstance(section, dict):
        # Legacy schema: single group inline
        cells = list(section.get("cells") or [])
        zr = section.get("zoom_range") or [9, 10]
        if cells:
            groups.append(GapFillGroup(
                name="gapfill",
                zoom_range=(int(zr[0]), int(zr[1])),
                cells=cells,
            ))
    return groups


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Source:
    path: Optional[Path]
    minzoom: int
    maxzoom: int
    label: str = ""
    geojson_dir: Optional[Path] = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="s57-to-mbtiles — Convert S-57 ENC charts (.000) to vector MBTiles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Single source:
  %(prog)s region03.zip
  %(prog)s region03.zip -o ne.mbtiles --minzoom 9 --maxzoom 16

Two-source split:
  %(prog)s region03.zip RI_detail.zip --split 12 -o ri.mbtiles

Multi-source explicit ranges:
  %(prog)s --sources region03.zip:9-11 RI.zip:12-16 -o merged.mbtiles

By-band (recommended for multi-state):
  %(prog)s CT_ENCs.zip RI_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ny.mbtiles

Skip GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12
        """,
    )
    parser.add_argument("inputs", nargs="*",
                        help="ZIP file(s) or director(ies) containing .000 ENC files")
    parser.add_argument("--by-band", action="store_true",
                        help="Auto-group inputs by NOAA usage band.")
    parser.add_argument("--sources", nargs="+", metavar="FILE:MIN-MAX",
                        help="Explicit sources with zoom ranges.")
    parser.add_argument("--split", type=int, metavar="ZOOM",
                        help="Zoom split for two-input mode.")
    parser.add_argument("-o", "--output", help="Output .mbtiles filename")
    parser.add_argument("--output-dir",
                        help="Production directory to copy final .mbtiles to.")
    parser.add_argument("--minzoom", type=int, default=9)
    parser.add_argument("--maxzoom", type=int, default=16)
    parser.add_argument("--geojson-dir",
                        help="Skip GDAL, use existing GeoJSON directory")
    parser.add_argument("-j", "--jobs", type=int,
                        default=max(1, (os.cpu_count() or 2) // 2),
                        help="Parallel workers (default: half CPU count)")
    parser.add_argument("--catalog", metavar="XML",
                        help="NOAA ENC product catalog to use instead of "
                             "downloading ENCProdCat_19115.xml (by-band)")
    parser.add_argument("--no-replacements", action="store_true",
                        help="Do not fetch the reschemed cells that replace "
                             "cancelled ones from NOAA's catalog (by-band)")
    parser.add_argument("--pmtiles", action="store_true",
                        help="Also write a PMTiles archive of the final "
                             "tileset beside it (same tiles, layers and "
                             "metadata; needs go-pmtiles' `pmtiles` on PATH)")
    return parser


def setup_data_dir() -> Path:
    for sub in ("zips", "enc", "geojson", "merged", "tiles"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def resolve_output_name(args) -> str:
    if args.output:
        return args.output
    if args.inputs:
        stem = Path(args.inputs[0]).stem
        suffix = "-merged" if len(args.inputs) > 1 else ""
        return f"{stem}{suffix}.mbtiles"
    return "enc-chart.mbtiles"


def parse_source_spec(spec: str, default_min: int, default_max: int) -> Source:
    m = re.match(r'^(.+):(\d+)-(\d+)$', spec)
    if m:
        p = Path(m.group(1)).resolve()
        return Source(p, int(m.group(2)), int(m.group(3)), label=p.name)
    p = Path(spec).resolve()
    return Source(p, default_min, default_max, label=p.name)


def build_sources(args, parser) -> List[Source]:
    if args.sources:
        return [parse_source_spec(s, args.minzoom, args.maxzoom) for s in args.sources]
    if args.geojson_dir:
        gj = Path(args.geojson_dir).resolve()
        if not gj.is_dir():
            print(f"ERROR: {gj} is not a directory", file=sys.stderr)
            sys.exit(1)
        return [Source(None, args.minzoom, args.maxzoom, label="geojson", geojson_dir=gj)]
    if len(args.inputs) == 1:
        p = Path(args.inputs[0]).resolve()
        return [Source(p, args.minzoom, args.maxzoom, label=p.name)]
    if len(args.inputs) == 2 and args.split:
        return [
            Source(Path(args.inputs[0]).resolve(),
                   args.minzoom, args.split - 1, label=Path(args.inputs[0]).name),
            Source(Path(args.inputs[1]).resolve(),
                   args.split, args.maxzoom, label=Path(args.inputs[1]).name),
        ]
    if len(args.inputs) == 2:
        parser.error("Two inputs require --split or --by-band.")
    if len(args.inputs) > 2:
        parser.error("More than two inputs require --by-band or --sources.")
    parser.error("Provide at least one input, or use --sources / --geojson-dir")


def validate_sources(sources: List[Source]):
    for s in sources:
        if s.path and not s.path.exists():
            print(f"ERROR: {s.path} not found", file=sys.stderr)
            sys.exit(1)
        if s.minzoom > s.maxzoom:
            print(f"ERROR: minzoom {s.minzoom} > maxzoom {s.maxzoom}", file=sys.stderr)
            sys.exit(1)
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            if a.minzoom <= b.maxzoom and b.minzoom <= a.maxzoom:
                print(f"WARNING: {a.label} overlaps {b.label}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def find_container_runtime() -> Optional[str]:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def check_deps(need_gdal: bool = True,
               need_pmtiles: bool = False) -> Tuple[bool, Optional[str]]:
    errors = []
    if not shutil.which("tippecanoe"):
        errors.append("tippecanoe not found.")
    if not shutil.which("tile-join"):
        errors.append("tile-join not found.")
    if need_pmtiles and not shutil.which("pmtiles"):
        errors.append("pmtiles not found (go-pmtiles); needed for --pmtiles.")
    native_gdal = bool(shutil.which("ogr2ogr") and shutil.which("ogrinfo"))
    runtime = find_container_runtime()
    if need_gdal and not native_gdal and not runtime:
        errors.append("No GDAL found. Install ogr2ogr or podman/docker.")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if native_gdal:
        print("Using native GDAL")
    elif runtime:
        print(f"Using containerized GDAL via {runtime}")
    return native_gdal, runtime


def pull_image(runtime: str, image: str):
    check_cmd = "image exists" if runtime == "podman" else "image inspect"
    result = subprocess.run(
        [runtime] + check_cmd.split() + [image], capture_output=True
    )
    if result.returncode != 0:
        print(f"Pulling {image}...")
        subprocess.run([runtime, "pull", image], check=True)


# ---------------------------------------------------------------------------
# Freshness helpers (mtime-based skip for incremental rebuilds)
# ---------------------------------------------------------------------------

def output_is_fresh(output: Path, inputs: List[Path]) -> bool:
    """True if `output` exists, is non-empty, and is newer than every input."""
    if not output.exists() or output.stat().st_size <= 100:
        return False
    output_mtime = output.stat().st_mtime
    for p in inputs:
        if not p.exists():
            continue
        if p.stat().st_mtime > output_mtime:
            return False
    return True


def _mbtiles_zoom_range(path: Path) -> Optional[Tuple[int, int]]:
    """(minzoom, maxzoom) from an mbtiles' metadata, or None if unreadable.
    Used so a zoom-range change invalidates otherwise-fresh outputs."""
    try:
        db = sqlite3.connect(path)
        meta = dict(db.execute(
            "SELECT name, value FROM metadata "
            "WHERE name IN ('minzoom', 'maxzoom')").fetchall())
        db.close()
        return int(meta["minzoom"]), int(meta["maxzoom"])
    except Exception:
        return None


# tippecanoe's default --drop-rate; what a run without an explicit rate
# was built with (recorded in the mbtiles metadata, see _patch_metadata).
TIPPECANOE_DEFAULT_DROP_RATE = 2.5


def _mbtiles_drop_rate(path: Path) -> Optional[float]:
    """The drop rate an mbtiles was rendered with, from the `drop_rate`
    metadata key _patch_metadata writes; None if absent or unreadable, so
    a file built before the key existed (or by a different tippecanoe
    invocation) is never reused as fresh."""
    try:
        db = sqlite3.connect(path)
        row = db.execute("SELECT value FROM metadata "
                         "WHERE name = 'drop_rate'").fetchone()
        db.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def _cell_marker(geojson_dir: Path, cell: str) -> Path:
    """Completion marker written after the LAST layer of a cell is
    exported, holding the cell version (cell_version) the export was made
    from. Two things it guarantees: an export interrupted mid-cell (killed
    run, lost CI runner) has no marker and is redone (2026-09-05: 39 cells
    missing most layers, SOUNDG included, after a killed run was resumed);
    and freshness is decided by the cell's own edition/update numbers, not
    by file times, which unzip, copy and re-download all rewrite."""
    return geojson_dir / f".{cell}.exported"


def cell_outputs_fresh(enc_path: Path, geojson_dir: Path,
                       multi_file: bool, version: str) -> bool:
    """True if the cell was exported from exactly this version (marker
    content == version) and its outputs are still there."""
    cell_stem = enc_path.stem
    marker = _cell_marker(geojson_dir, cell_stem)
    try:
        if version == "unknown" or marker.read_text().strip() != version:
            return False
    except OSError:
        return False
    pattern = f"*_{cell_stem}.geojson" if multi_file else "*.geojson"
    return any(True for _ in geojson_dir.glob(pattern))


EXPORT_ERROR_LOG = ".export-errors.log"


# ---------------------------------------------------------------------------
# Stage 1: Input staging
# ---------------------------------------------------------------------------

def stage_input(input_path: Path, enc_dir: Path, zips_dir: Path):
    enc_dir.mkdir(parents=True, exist_ok=True)
    if input_path.is_file() and zipfile.is_zipfile(input_path):
        zip_dest = zips_dir / input_path.name
        if not zip_dest.exists():
            shutil.copy2(input_path, zip_dest)
            print(f"Archived {input_path.name} -> {zip_dest}")
        print(f"Extracting {input_path.name}...")
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(enc_dir)
    elif input_path.is_dir():
        shutil.copytree(input_path, enc_dir, dirs_exist_ok=True)
    else:
        print(f"ERROR: {input_path} is not a ZIP or directory", file=sys.stderr)
        sys.exit(1)


def find_enc_files(directory: Path) -> List[Path]:
    return sorted(directory.rglob("*.000"))


def enc_band(enc_file: Path) -> Optional[int]:
    m = re.match(r'^US(\d)', enc_file.stem, re.IGNORECASE)
    return int(m.group(1)) if m else None


ENC_CELL_RE = re.compile(r'^US\d[A-Z0-9]{5}$', re.IGNORECASE)


def layer_name_from_stem(stem: str) -> str:
    """S-57 layer name from a per-cell GeoJSON stem.

    Multi-cell exports are named LAYER_CELL (e.g. DEPARE_US5MA1SK); a
    single-cell export is just LAYER. Layer names themselves may contain
    underscores (M_COVR, M_QUAL, M_NSYS, TS_FEB), so only a trailing NOAA
    cell name is stripped — never everything after the first underscore,
    which folded every M_* meta layer into one layer called "M"."""
    head, sep, tail = stem.rpartition("_")
    if sep and ENC_CELL_RE.match(tail):
        return head
    return stem


def cell_name_from_stem(stem: str) -> Optional[str]:
    """The NOAA cell name a per-cell GeoJSON stem ends with (LAYER_CELL),
    or None for single-cell exports that carry no suffix."""
    head, sep, tail = stem.rpartition("_")
    if sep and ENC_CELL_RE.match(tail):
        return tail.upper()
    return None


def _remove_orphan_layers(merged_dir: Path, wanted: set):
    """Delete merged LAYER.geojson files no current layer produces (e.g.
    the old "M.geojson" after the layer-name fix), so tippecanoe is not
    fed a stale layer alongside the correct ones."""
    for f in merged_dir.glob("*.geojson"):
        if f.stem not in wanted:
            f.unlink()


def group_by_band(enc_files: List[Path]) -> Dict[int, List[Path]]:
    groups: Dict[int, List[Path]] = {}
    for f in enc_files:
        band = enc_band(f)
        groups.setdefault(band if band is not None else 0, []).append(f)
    return groups


# ---------------------------------------------------------------------------
# Cancelled cells
# ---------------------------------------------------------------------------
# S-57 Appendix B.1 §5.7: "In order to delete a data set, an update cell
# file is created, containing only the Data Set General Information record
# with the DSID field. The Edition Number [EDTN] subfield must be set to 0.
# This message is only used to cancel a base cell file." NOAA's district
# zips (checked 2026-09-05, all eight active districts) still ship 190
# cells that carry such a cancellation update — legacy cells withdrawn as
# their reschemed replacements were released. GDAL applies the update on
# open and reports DSID_EDTN = 0, but keeps every feature, so without this
# check the pipeline renders withdrawn charts on top of their replacements
# (docs/SAME-BAND-OVERLAP.md). An ECDIS removes a cancelled cell; so do we.

def _cell_files(enc: Path) -> List[Path]:
    """The base .000 and every .NNN update file of a cell."""
    return sorted(enc.parent.glob(f"{enc.stem}.[0-9][0-9][0-9]"))


# DSID fields the pipeline reads per cell: edition/update/issue date for
# cancellation and freshness (DSID record) and the compilation scale
# (DSPM record, exposed by GDAL on the same DSID layer) for the bundle's
# `scale` metadata.
DSID_FIELDS = {"EDTN": "DSID_EDTN", "UPDN": "DSID_UPDN", "ISDT": "DSID_ISDT",
               "CSCL": "DSPM_CSCL"}


def parse_dsid(text: str) -> Dict[str, Optional[str]]:
    """DSID_FIELDS values out of `ogrinfo -al` output of the DSID layer;
    None for a field that is absent."""
    out: Dict[str, Optional[str]] = {}
    for key, field in DSID_FIELDS.items():
        m = re.search(rf"{field} \(\w+\) = (\S+)", text)
        out[key] = m.group(1) if m else None
    return out


def read_cell_dsid(enc: Path, gdal: "GdalRunner") -> Dict[str, Optional[str]]:
    """EDTN/UPDN/ISDT/CSCL from the DSID record with updates applied."""
    res = subprocess.run(gdal.cmd(["ogrinfo", "-ro", "-q", "-al", enc, "DSID"]),
                         capture_output=True, text=True)
    return parse_dsid(res.stdout)


def read_cell_versions(enc_files: List[Path], data_dir: Path,
                       gdal: "GdalRunner", max_workers: int,
                       why: str = "") -> Dict[Path, Dict[str, Optional[str]]]:
    """DSID EDTN/UPDN/ISDT of every cell, with updates applied. Cached in
    data/enc/.cell-editions.json keyed by cell name and the newest mtime
    among the cell's files (shutil.copy2 preserves mtimes, so the band
    copies prepare_band makes hit the staging entries), so only new or
    updated cells cost an ogrinfo call."""
    cache_path = data_dir / "enc" / ".cell-editions.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}

    def stamp(enc: Path) -> int:
        return max(f.stat().st_mtime_ns for f in _cell_files(enc) + [enc])

    def key(enc: Path) -> str:
        """Cache key: the cell name, shared by every staged copy."""
        return enc.stem.upper()

    # A cache entry is reused only for the same file state and only if it
    # carries every field read today (entries written before CSCL was
    # added lack it).
    todo = [e for e in enc_files
            if cache.get(key(e), {}).get("stamp") != stamp(e)
            or any(f not in cache.get(key(e), {}) for f in DSID_FIELDS)]
    if todo:
        print(f"Reading DSID of {len(todo)}/{len(enc_files)} cell(s)"
              f"{' for ' + why if why else ''}...")

        def one(e: Path):
            """(cache key, DSID fields + stamp) for one cell."""
            return key(e), dict(read_cell_dsid(e, gdal), stamp=stamp(e))

        with ThreadPoolExecutor(max_workers=max(1, max_workers) * 2) as pool:
            for k, val in pool.map(one, todo):
                cache[k] = val
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True))
    return {e: cache.get(key(e), {}) for e in enc_files}


def cell_version(info: Dict[str, Optional[str]]) -> str:
    """The cell's published state, 'EDTN.UPDN' (e.g. '62.0'): the S-57
    edition and update numbers after ER updates are applied. It changes
    whenever NOAA changes the cell, and only then — the export freshness
    key. 'unknown' when the DSID could not be read."""
    if not info.get("EDTN"):
        return "unknown"
    return f"{info['EDTN']}.{info.get('UPDN') or 0}"


# Nominal compilation scale per usage band, the fallback for a bundle's
# `scale` when no cell's DSPM CSCL could be read (NOAA's band definitions;
# the same figures BAND_ZOOM quotes).
BAND_NOMINAL_SCALE: Dict[int, int] = {
    1: 3_500_000, 2: 700_000, 3: 90_000, 4: 22_000, 5: 8_000, 6: 3_000}


def bundle_scale(enc_files: List[Path], data_dir: Path, gdal: "GdalRunner",
                 max_workers: int) -> Optional[int]:
    """The `scale` metadata of a bundle built from enc_files: the most
    detailed compilation scale among its cells (smallest DSPM CSCL), or
    the finest band's nominal scale when no CSCL is readable, or None
    for an empty inventory.

    A bundle merges many cells across bands, so it has no single true
    scale; the consumers of this value (Signal K chart plugins, which
    default to 250000 when the row is missing, and Freeboard-SK, which
    only uses it to stack charts most-detailed-on-top) need the scale of
    the most detailed data the bundle holds. Reads nothing new: the DSID
    fields are already cached per cell for the cancellation check."""
    if not enc_files:
        return None
    versions = read_cell_versions(enc_files, data_dir, gdal, max_workers,
                                  "compilation scale")
    scales = []
    for e in enc_files:
        try:
            cscl = int(versions[e].get("CSCL") or 0)
        except (TypeError, ValueError):
            cscl = 0
        if cscl > 0:
            scales.append(cscl)
    if scales:
        return min(scales)
    bands = [b for b in (enc_band(e) for e in enc_files) if b in BAND_NOMINAL_SCALE]
    return BAND_NOMINAL_SCALE[max(bands)] if bands else None


def drop_cancelled_cells(enc_files: List[Path], data_dir: Path,
                         gdal: "GdalRunner", max_workers: int,
                         record_path: Path) -> List[Path]:
    """Return enc_files without cancelled cells (DSID EDTN = 0 after
    updates). Writes record_path listing the cancelled cells."""
    versions = read_cell_versions(enc_files, data_dir, gdal, max_workers,
                                  "cancellation status")
    kept: List[Path] = []
    cancelled = []
    for e in enc_files:
        info = versions[e]
        if info.get("EDTN") == "0":
            cancelled.append({"cell": e.stem, "band": enc_band(e),
                              "cancel_update": info.get("UPDN"),
                              "cancelled_on": info.get("ISDT"),
                              "path": str(e)})
        else:
            kept.append(e)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(
        {"rule": "S-57 App. B.1 5.7: DSID EDTN=0 after updates = cancelled",
         "cancelled": sorted(cancelled, key=lambda c: c["cell"])},
        indent=1))
    if cancelled:
        names = " ".join(c["cell"] for c in sorted(cancelled, key=lambda c: c["cell"]))
        print(f"Excluding {len(cancelled)} cancelled cell(s) (S-57 EDTN=0): {names}")
    return kept


def _remove_orphan_cells(geojson_dir: Path, wanted_cells: set):
    """Delete per-cell exports of cells no longer in the inventory (cancelled
    since the last run, or gone from NOAA's zip) so they cannot reach
    consolidation."""
    for f in geojson_dir.glob("*_US*.geojson"):
        cell = cell_name_from_stem(f.stem)
        if cell and cell not in wanted_cells:
            f.unlink()
    for m in geojson_dir.glob(".US*.exported"):
        if m.name[1:-9].upper() not in wanted_cells:
            m.unlink()


# ---------------------------------------------------------------------------
# Stage 1b: replacement cells from NOAA's product catalog
# ---------------------------------------------------------------------------
#
# NOAA's district zips hold the cells NOAA files under that district. The
# rescheming program replaces one legacy cell that spanned several
# districts with gridded cells that are each filed under ONE district, so
# a district zip can lose coverage of its own waters the day NOAA cancels
# the legacy cell: 01CGD's band 2 chart west of 72°W (New York, Long
# Island Sound, Connecticut) was US2EC04M, cancelled 2026-07-24; its
# successor there, US2ATLPC, is filed under district 5 and ships only in
# 05CGD_ENCs.zip. Dropping cancelled cells (correct: S-57 says a
# cancelled cell must not be displayed) therefore emptied z9-10 over New
# York in the 2026-09-05 build.
#
# This stage restores coverage with CURRENT data rather than the withdrawn
# chart: for every cancelled cell it looks up, in NOAA's ENC product
# catalog (per-cell extent polygons, "coast guard district" keyword and a
# per-cell download link), the live reschemed cells of the same band whose
# extent overlaps the cancelled cell AND overlaps the district's own
# charted waters (the extents of the input's live band >= 3 cells — the
# same notion of "district region" trim_low_bands_to_region uses), and
# downloads the ones missing from the input. They then go through the
# normal pipeline (band grouping, Stage 2b, erase) like any other cell.
# Legacy-named cells (USbXXnnM) are never fetched: a neighbour district's
# legacy cell is exactly the same-band overlap Stage 2b exists to avoid.
# Extents are catalog polygons' bounding boxes (reschemed cells are grid
# rectangles, so exact for what is fetched) and the cancelled cell's
# M_COVR extent from ogrinfo.

CATALOG_URL = "https://charts.noaa.gov/ENCs/ENCProdCat_19115.xml"
CELL_ZIP_URL = "https://charts.noaa.gov/ENCs/{cell}.zip"
CATALOG_MAX_AGE_S = 24 * 3600
LEGACY_CELL_RE = re.compile(r"^US\d[A-Z]{2}\d{2}M$", re.IGNORECASE)

BBox = Tuple[float, float, float, float]  # w, s, e, n


@dataclass
class CatalogCell:
    name: str
    band: int
    district: str
    bbox: BBox
    url: str


def _bbox_intersects(a: BBox, b: BBox) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _download(url: str, dest: Path, attempts: int = 3) -> bool:
    """curl when available (it uses the system trust store; Python builds
    without certificates fail TLS to charts.noaa.gov), else urllib."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    curl = shutil.which("curl")
    for i in range(attempts):
        try:
            if curl:
                subprocess.run([curl, "-fsSL", "--retry", "3", "--retry-delay", "5",
                                "-o", str(tmp), url], check=True,
                               capture_output=True, text=True)
            else:
                with urllib.request.urlopen(url, timeout=120) as r, \
                        open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f)
            tmp.replace(dest)
            return True
        except Exception as e:  # noqa: BLE001 - any network error retries
            detail = getattr(e, "stderr", None) or str(e)
            print(f"  download failed ({i + 1}/{attempts}) {url}: "
                  f"{str(detail).strip()[:200]}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    tmp.unlink(missing_ok=True)
    return False


def fetch_catalog(data_dir: Path, override: Optional[Path]) -> Optional[Path]:
    """NOAA's ENC product catalog, cached in data_dir for CATALOG_MAX_AGE_S.
    A stale cached copy is still used when the download fails."""
    if override:
        return override
    dest = data_dir / "ENCProdCat_19115.xml"
    fresh = dest.exists() and time.time() - dest.stat().st_mtime < CATALOG_MAX_AGE_S
    if fresh:
        return dest
    print("Downloading NOAA ENC product catalog...")
    if _download(CATALOG_URL, dest):
        return dest
    if dest.exists():
        print("WARNING: using stale cached catalog", file=sys.stderr)
        return dest
    return None


def parse_catalog(path: Path) -> Dict[str, CatalogCell]:
    """Live cells with an extent polygon. Cancelled cells stay listed in
    the catalog but carry no polygon, so they drop out here."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    cells: Dict[str, CatalogCell] = {}
    for m in re.finditer(r"<title>\s*<gco:CharacterString>(US\d[A-Z0-9]{5})"
                         r"</gco:CharacterString>", text):
        name = m.group(1).upper()
        end = text.find("</MD_DataIdentification>", m.start())
        blk = text[m.start():end if end > 0 else m.start() + 20000]
        pts = [tuple(map(float, p.split()))
               for p in re.findall(r"<gml:pos>([^<]*)</gml:pos>", blk)]
        if not pts:
            continue
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        d = re.search(r"coast guard district: (\d+)", blk)
        cells[name] = CatalogCell(
            name, int(name[2]), d.group(1) if d else "?",
            (min(lons), min(lats), max(lons), max(lats)),
            CELL_ZIP_URL.format(cell=name))
    return cells


def read_cell_extent(enc: Path, gdal: "GdalRunner") -> Optional[BBox]:
    """Extent of the cell's M_COVR layer (all coverage records)."""
    res = subprocess.run(gdal.cmd(["ogrinfo", "-ro", "-so", enc, "M_COVR"]),
                         capture_output=True, text=True)
    m = re.search(r"Extent: \(([-\d.]+), ([-\d.]+)\) - \(([-\d.]+), ([-\d.]+)\)",
                  res.stdout)
    return tuple(float(v) for v in m.groups()) if m else None  # type: ignore


def is_reschemed_name(name: str) -> bool:
    band = enc_band(Path(name))
    if band in RESCHEMED_REGION_CODES:
        return classify_cell(name) == "reschemed"
    return not LEGACY_CELL_RE.match(name)


def fetch_replacement_cells(cancelled: List[dict], live: List[Path],
                            data_dir: Path, gdal: "GdalRunner",
                            catalog_path: Optional[Path],
                            record_path: Path) -> List[Path]:
    """Download the live reschemed cells that replace `cancelled` inside
    this district's waters and are missing from `live`; return their .000
    paths. Writes record_path describing what was fetched and why."""
    record = {"rule": "catalog same-band reschemed cells overlapping a "
                      "cancelled cell and the input's band>=3 extents",
              "fetched": []}
    if not cancelled:
        record_path.write_text(json.dumps(record, indent=1))
        return []
    catalog_file = fetch_catalog(data_dir, catalog_path)
    if not catalog_file:
        print("WARNING: no NOAA catalog available; cancelled cells are not "
              "replaced", file=sys.stderr)
        return []
    catalog = parse_catalog(catalog_file)
    have = {e.stem.upper() for e in live}
    region = [catalog[n].bbox for n in have if n in catalog and catalog[n].band >= 3]
    if not region:
        print("WARNING: no band>=3 cell of the input is in the catalog; "
              "cannot define the district region, cancelled cells are not "
              "replaced", file=sys.stderr)
        return []

    wanted: Dict[str, dict] = {}
    # cancelled cell -> live same-band cells overlapping it (in the input
    # or fetched below). Gap-fill groups use this to follow a cancelled
    # cell to its successors (_prepare_gap_fill_group).
    successors: Dict[str, List[str]] = {}
    for c in cancelled:
        extent = read_cell_extent(Path(c["path"]), gdal)
        if not extent:
            continue
        for cat in catalog.values():
            if (cat.band != c["band"] or cat.name in have
                    or not is_reschemed_name(cat.name)
                    or not _bbox_intersects(cat.bbox, extent)
                    or not any(_bbox_intersects(cat.bbox, r) for r in region)):
                continue
            w = wanted.setdefault(cat.name, {
                "cell": cat.name, "band": cat.band, "district": cat.district,
                "url": cat.url, "replaces": []})
            w["replaces"].append(c["cell"])
        successors[c["cell"]] = sorted(
            n for n in have if n in catalog and catalog[n].band == c["band"]
            and is_reschemed_name(n) and _bbox_intersects(catalog[n].bbox, extent))
    record["successors"] = successors

    fetched: List[Path] = []
    zips_dir = data_dir / "zips"
    enc_root = data_dir / "enc" / "replacements"
    for name in sorted(wanted):
        w = wanted[name]
        zip_path = zips_dir / f"{name}.zip"
        stale = (not zip_path.exists()
                 or time.time() - zip_path.stat().st_mtime > CATALOG_MAX_AGE_S)
        if stale and not _download(w["url"], zip_path):
            print(f"WARNING: could not download replacement cell {name}",
                  file=sys.stderr)
            continue
        dest = enc_root / name
        if dest.exists():
            shutil.rmtree(dest)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        base = next(iter(sorted(dest.rglob(f"{name}.000"))), None)
        if base is None:
            print(f"WARNING: {zip_path.name} holds no {name}.000", file=sys.stderr)
            continue
        fetched.append(base)
        w["path"] = str(base)
        record["fetched"].append(w)
        for c in w["replaces"]:
            successors[c].append(name)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=1))
    if fetched:
        print(f"Fetched {len(fetched)} replacement cell(s) from NOAA's catalog "
              f"for cancelled cells: "
              + " ".join(f"{w['cell']}(d{w['district']}<-{'+'.join(w['replaces'])})"
                         for w in record["fetched"]))
    else:
        print("No replacement cells needed from NOAA's catalog")
    return fetched


# ---------------------------------------------------------------------------
# Stage 2: GDAL export
# ---------------------------------------------------------------------------

def export_to_geojson(
    enc_dir: Path,
    geojson_dir: Path,
    enc_files: List[Path],
    label: str = "",
    native_gdal: bool = True,
    runtime: Optional[str] = None,
    max_workers: int = 1,
    data_dir: Optional[Path] = None,
) -> List[Path]:
    """Stage 2: export every layer of every cell in enc_files to GeoJSON
    in geojson_dir, natively or in the GDAL container. Cells whose
    completion marker holds their current EDTN.UPDN are skipped. Returns
    the non-empty GeoJSON files present afterwards; per-layer failures
    are logged to EXPORT_ERROR_LOG and summarised on stderr."""
    tag = f"[{label}] " if label else ""
    multi_file = len(enc_files) > 1
    data_dir = data_dir or DATA_DIR
    gdal = GdalRunner(native_gdal, runtime, data_dir)

    # Per-cell freshness: re-export a cell only when its edition/update
    # numbers differ from those its existing export was made from.
    infos = read_cell_versions(enc_files, data_dir, gdal, max_workers,
                               "export freshness")
    versions = {f: cell_version(infos[f]) for f in enc_files}
    cells_to_process = [
        f for f in enc_files
        if not cell_outputs_fresh(f, geojson_dir, multi_file, versions[f])
    ]

    if not cells_to_process:
        existing = [f for f in geojson_dir.glob("*.geojson")
                    if f.stat().st_size > 100]
        print(f"{tag}GeoJSON fresh ({len(existing)} layers), skipping GDAL")
        return existing

    print(f"{tag}GDAL: converting {len(cells_to_process)}/{len(enc_files)} "
          f"cell(s) to GeoJSON...")

    error_log = geojson_dir / EXPORT_ERROR_LOG
    error_log.write_text("")
    if native_gdal:
        _export_native(enc_dir, geojson_dir, cells_to_process, multi_file,
                       tag, max_workers, versions)
    else:
        _export_container(runtime, enc_dir, geojson_dir, cells_to_process,
                          multi_file, tag, label, versions)
    _report_export_errors(tag, error_log)

    valid = []
    for f in list(geojson_dir.glob("*.geojson")):
        if f.stat().st_size > 100:
            valid.append(f)
        else:
            f.unlink()

    print(f"{tag}Generated {len(valid)} GeoJSON layers")
    return valid


def _report_export_errors(tag: str, error_log: Path):
    """ogr2ogr/ogrinfo failures are per layer and used to vanish; a layer
    that failed to export is a silent hole in the chart. Every failure is
    appended to EXPORT_ERROR_LOG in the GeoJSON dir; this prints the count
    and the first few."""
    try:
        lines = [ln for ln in error_log.read_text().splitlines() if ln.strip()]
    except OSError:
        return
    if not lines:
        return
    print(f"{tag}WARNING: {len(lines)} GDAL export failure(s), see "
          f"{error_log}", file=sys.stderr)
    for ln in lines[:5]:
        print(f"{tag}  {ln[:200]}", file=sys.stderr)


def _log_export_error(error_log: Path, lock, text: str):
    """Append one line to the export error log under the thread lock."""
    with lock:
        with open(error_log, "a") as fh:
            fh.write(text.rstrip() + "\n")


def _export_native(enc_dir, geojson_dir, cells_to_process, multi_file,
                   tag, max_workers, versions: Dict[Path, str]):
    """Native ogr2ogr export of cells_to_process, max_workers cells at a
    time. A cell's completion marker (holding versions[cell]) is written
    only when every non-skipped layer exported."""
    import threading
    total = len(cells_to_process)
    done = [0]
    error_log = geojson_dir / EXPORT_ERROR_LOG
    lock = threading.Lock()

    def process_enc(enc: Path):
        """Export one cell: list its layers, drop its old outputs, run
        ogr2ogr per layer, mark the cell complete if nothing failed."""
        name = enc.stem
        result = subprocess.run(
            ["ogrinfo", "-so", str(enc)], capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stderr or "").strip().splitlines()
            _log_export_error(error_log, lock,
                              f"{name}: ogrinfo failed: {err[-1] if err else '?'}")
            return

        layers = []
        for line in result.stdout.splitlines():
            m = re.match(r'^\d+:\s+(\S+)', line)
            if m:
                layers.append(m.group(1))

        # Drop every existing output of this cell, not just the layers
        # about to be rewritten: an orphan from a layer the cell no
        # longer has (object class removed by an ER update, or an older
        # export) keeps an old mtime, fails cell_outputs_fresh forever,
        # and forces a re-export — and a cascade of stale merged layers
        # and tilesets — on every run.
        pattern = f"*_{name}.geojson" if multi_file else "*.geojson"
        for old in geojson_dir.glob(pattern):
            old.unlink()
        _cell_marker(geojson_dir, name).unlink(missing_ok=True)

        failed = 0
        for layer in layers:
            if layer in SKIP_LAYERS:
                continue
            outname = f"{layer}_{name}" if multi_file else layer
            outpath = geojson_dir / f"{outname}.geojson"
            cmd = ["ogr2ogr", "-f", "GeoJSON", "-oo", "LIST_AS_STRING=YES"]
            if layer == "SOUNDG":
                cmd.extend(["-oo", "SPLIT_MULTIPOINT=YES",
                            "-oo", "ADD_SOUNDG_DEPTH=YES"])
            cmd.extend([str(outpath), str(enc), layer])
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not outpath.exists():
                failed += 1
                err = (r.stderr or "").strip().splitlines()
                _log_export_error(error_log, lock, f"{name} {layer}: "
                                  f"{err[-1] if err else 'no output'}")
        # The completion marker means "every layer of this version is
        # here". A cell with a failed layer gets none, so the next run
        # re-exports it instead of treating the hole as fresh.
        if failed:
            print(f"{tag}[{name}] {failed} layer(s) failed; cell left "
                  "unmarked for retry", file=sys.stderr)
        else:
            _cell_marker(geojson_dir, name).write_text(versions[enc] + "\n")

        done[0] += 1
        print(f"{tag}[{done[0]}/{total}] {name}")

    if max_workers <= 1:
        for enc in cells_to_process:
            process_enc(enc)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_enc, enc): enc
                       for enc in cells_to_process}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"WARNING: {futures[future].name}: {exc}",
                          file=sys.stderr)

    print(f"{tag}Export complete")


def _export_container(runtime, enc_dir, geojson_dir, cells_to_process,
                      multi_file, tag, label, versions: Dict[Path, str]):
    """Same export as _export_native, run as one shell script inside the
    GDAL container with enc_dir and geojson_dir bind-mounted. The script
    touches a marker per fully exported cell; the version is stamped in
    afterwards."""
    skip_case = "|".join(SKIP_LAYERS)
    name_template = "${layer}_${name}" if multi_file else "${layer}"
    # Same orphan cleanup as _export_native (see comment there).
    rm_pattern = ('/output/*_"$name".geojson' if multi_file
                  else '/output/*.geojson')

    # Pass explicit relative paths so the container processes only the
    # cells the freshness check decided are stale.
    rel_paths = [str(c.relative_to(enc_dir)) for c in cells_to_process]
    cells_arg = " ".join(rel_paths)

    script = f"""
set -e
cells="{cells_arg}"
count=$(echo "$cells" | wc -w)
i=0
for rel in $cells; do
  enc="/input/$rel"
  i=$((i + 1))
  name=$(basename "$enc" .000)
  echo "[$i/$count] $name"
  rm -f {rm_pattern} "/output/.$name.exported"
  if ! ogrinfo -so "$enc" >/tmp/layers 2>/tmp/err; then
    echo "$name: ogrinfo failed: $(tail -n1 /tmp/err)" >> "/output/{EXPORT_ERROR_LOG}"
    continue
  fi
  layers=$(grep -E '^[0-9]+:' /tmp/layers | awk -F': ' '{{print $2}}' | awk '{{print $1}}')
  failed=0
  for layer in $layers; do
    case "$layer" in {skip_case}) continue ;; esac
    outname="{name_template}"
    rm -f "/output/$outname.geojson"
    if [ "$layer" = "SOUNDG" ]; then
      ogr2ogr -f GeoJSON -oo SPLIT_MULTIPOINT=YES -oo ADD_SOUNDG_DEPTH=YES \
        -oo LIST_AS_STRING=YES \
        "/output/$outname.geojson" "$enc" "$layer" 2>/tmp/err || {{ echo "$name $layer: $(tail -n1 /tmp/err)" >> "/output/{EXPORT_ERROR_LOG}"; failed=1; }}
    else
      ogr2ogr -f GeoJSON -oo LIST_AS_STRING=YES \
        "/output/$outname.geojson" "$enc" "$layer" 2>/tmp/err || {{ echo "$name $layer: $(tail -n1 /tmp/err)" >> "/output/{EXPORT_ERROR_LOG}"; failed=1; }}
    fi
  done
  # Marker only when every layer exported (see _export_native).
  if [ "$failed" = 0 ]; then
    touch "/output/.$name.exported"
  else
    echo "$name: layer failure(s); cell left unmarked for retry" >&2
  fi
done
echo "Export complete"
"""

    result = subprocess.run(
        [runtime, "run", "--rm",
         "-v", f"{enc_dir}:/input:ro,Z",
         "-v", f"{geojson_dir}:/output:Z",
         GDAL_IMAGE, "sh", "-c", script],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"ERROR: GDAL export failed ({label})", file=sys.stderr)
        sys.exit(1)
    # The container touches a marker per cell whose every layer exported;
    # stamp the version into every marker it left behind.
    for enc in cells_to_process:
        marker = _cell_marker(geojson_dir, enc.stem)
        if marker.exists():
            marker.write_text(versions[enc] + "\n")


# ---------------------------------------------------------------------------
# Stage 3: GeoJSON consolidation
# ---------------------------------------------------------------------------

def merge_geojson_layer(layer_name: str, source_files: List[Path],
                        output_path: Path,
                        rule: Optional[ZoomRule] = None):
    """Merge multiple GeoJSON files into one valid FeatureCollection.
    Uses streaming writes to keep memory low.

    When rule is set, each feature gets the tippecanoe feature extension
    {"minzoom": N} from feature_minzoom, which tippecanoe honors natively
    — unlike per-layer "minzoom" in the -L JSON spec, which it silently
    ignores."""
    with open(output_path, "w") as out:
        out.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for source_file in source_files:
            try:
                with open(source_file) as inp:
                    fc = json.load(inp)
            except (json.JSONDecodeError, OSError):
                continue
            for feat in fc.get("features", []):
                stamp_feature(feat, rule)
                if not first:
                    out.write(",\n")
                json.dump(feat, out)
                first = False
        out.write("\n]}\n")


def _stamp_marker_stale(merged_dir: Path,
                        layer_rules: Optional[Dict[str, ZoomRule]]) -> bool:
    """Freshness guard for per-feature minzoom stamps baked into merged
    GeoJSON: mtime checks can't see stamp-config changes, so the applied
    config is recorded in a marker file. Returns True (treat all merged
    files as stale) when the config differs from what's recorded, and
    updates the marker."""
    merged_dir.mkdir(parents=True, exist_ok=True)
    marker = merged_dir / ".layer-minzoom.json"
    current = json.dumps({k: r.as_json() for k, r in
                          (layer_rules or {}).items()}, sort_keys=True)
    try:
        if marker.exists() and marker.read_text() == current:
            return False
    except OSError:
        pass
    marker.write_text(current)
    return True


SOURCE_MARKER = ".layer-sources.json"


def _source_identity(src: Path, geojson_dir: Path) -> str:
    """Stable name for a per-cell source file: relative to the geojson
    root so the plain export ("band3/X.geojson") and Stage 2b's clipped
    copy ("band3.resolved/X.geojson") are told apart."""
    try:
        return str(src.relative_to(geojson_dir.parent))
    except ValueError:
        return str(src)


def _read_source_marker(merged_dir: Path) -> Dict[str, List[str]]:
    """The merged dir's record of what each layer was built from
    (see _write_source_marker); empty when missing or unreadable."""
    try:
        data = json.loads((merged_dir / SOURCE_MARKER).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_source_marker(merged_dir: Path, sources: Dict[str, List[str]]):
    (merged_dir / SOURCE_MARKER).write_text(
        json.dumps(sources, indent=1, sort_keys=True))


def consolidate_geojson(geojson_dir: Path, merged_dir: Path,
                        max_workers: int = 1,
                        layer_rules: Optional[Dict[str, ZoomRule]] = None,
                        overrides: Optional[Dict[str, Path]] = None
                        ) -> List[Path]:
    """Group geojson files by layer name and merge into one file per layer.
    Returns list of merged file paths. layer_rules maps layer name → the
    ZoomRule stamped per feature (see layer_zoom_rules).
    overrides maps a per-cell filename to a replacement file (Stage 2b's
    clipped copy of a legacy cell); a replacement with no features is
    skipped, so a fully erased cell contributes nothing."""
    merged_dir.mkdir(parents=True, exist_ok=True)

    geojson_files = [f for f in sorted(geojson_dir.glob("*.geojson"))
                     if f.stat().st_size > 100]
    if not geojson_files:
        return []

    # Group by layer name. Alongside the files, record each layer's source
    # identity: which file was chosen per cell, including overrides that
    # were skipped for having no features. mtimes can't see an override
    # being added, removed or emptied (the plain export is older than the
    # merged file), so the identity is persisted and compared instead.
    layer_groups: Dict[str, List[Path]] = {}
    layer_sources: Dict[str, List[str]] = {}
    for f in geojson_files:
        src = (overrides or {}).get(f.name, f)
        layer_name = layer_name_from_stem(f.stem)
        ident = _source_identity(src, geojson_dir)
        if src is not f and not _geojson_has_features(src):
            layer_sources.setdefault(layer_name, []).append(ident + "#empty")
            continue
        layer_groups.setdefault(layer_name, []).append(src)
        layer_sources.setdefault(layer_name, []).append(ident)
    _remove_orphan_layers(merged_dir, set(layer_groups))

    # Per-layer freshness pre-pass (all stale if the stamp config changed)
    force_stale = _stamp_marker_stale(merged_dir, layer_rules)
    recorded = _read_source_marker(merged_dir)
    fresh: List[Path] = []
    stale: List[Tuple[str, List[Path], Path]] = []
    for layer_name, files in layer_groups.items():
        out_path = merged_dir / f"{layer_name}.geojson"
        if (not force_stale
                and recorded.get(layer_name) == layer_sources[layer_name]
                and output_is_fresh(out_path, files)):
            fresh.append(out_path)
        else:
            stale.append((layer_name, files, out_path))

    if not stale:
        print(f"  All {len(layer_groups)} merged layers fresh, skipping")
        _write_source_marker(merged_dir, layer_sources)
        return sorted(fresh)

    print(f"  Consolidating {len(stale)}/{len(layer_groups)} layers "
          f"(others fresh)...")

    def merge_one(item):
        """Merge one layer's per-cell files into out_path with its rule."""
        layer_name, files, out_path = item
        rule = (layer_rules or {}).get(layer_name)
        if len(files) == 1 and rule is None:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path, rule=rule)
        return out_path

    if max_workers <= 1:
        results = [merge_one(item) for item in stale]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(merge_one, item): item[0]
                       for item in stale}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"WARNING: merge {futures[future]}: {exc}",
                          file=sys.stderr)
                else:
                    results.append(future.result())

    # Record the identity of every layer that is now up to date; a layer
    # whose merge failed is left out so it is retried next run.
    done = {p.stem for p in fresh + results}
    _write_source_marker(merged_dir, {k: v for k, v in layer_sources.items()
                                      if k in done})
    print(f"  Consolidated {len(results)} layers (+ {len(fresh)} fresh)")
    return sorted(fresh + results)


# ---------------------------------------------------------------------------
# Stage 4: tippecanoe (one invocation per band, full zoom range)
# ---------------------------------------------------------------------------

def _geojson_has_features(path: Path) -> bool:
    """False for a FeatureCollection whose features array is empty — the
    padded file erase_layer writes when a whole layer lies inside finer
    coverage. tippecanoe exits non-zero when every input is empty."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return not re.search(rb'"features"\s*:\s*\[\s*\]', head)


def run_tippecanoe_for_source(
    merged_dir: Path,
    tile_dir: Path,
    stem: str,
    minzoom: int,
    maxzoom: int,
    max_workers: int = 1,  # unused; tippecanoe handles its own threading
    drop_rate: Optional[float] = None,
) -> Optional[Path]:
    """Run a single tippecanoe over [minzoom, maxzoom] using merged GeoJSON.
    Returns the produced .mbtiles path, or None if there's nothing to
    build. drop_rate is tippecanoe's point drop rate per zoom below the
    run's top zoom; by-band mode passes 1 (no dropping — zoom presence is
    stamped per feature, see soundg_rule), plain mode leaves tippecanoe's
    default of 2.5 as its only thinning."""
    merged_files = [f for f in sorted(merged_dir.glob("*.geojson"))
                    if f.stat().st_size > 100 and _geojson_has_features(f)]
    if not merged_files:
        print(f"  [{stem}] no features to render (all erased or empty), "
              "skipping")
        return None

    final = tile_dir / f"{stem}.mbtiles"
    effective_drop_rate = (TIPPECANOE_DEFAULT_DROP_RATE if drop_rate is None
                           else float(drop_rate))

    # Fresh only if newer than its inputs AND built for the same zoom range
    # AND with the same drop rate: a by-band run (rate 1) and a plain run
    # (tippecanoe's default) over the same stem must not reuse each other.
    if (output_is_fresh(final, merged_files)
            and _mbtiles_zoom_range(final) == (minzoom, maxzoom)
            and _mbtiles_drop_rate(final) == effective_drop_rate):
        print(f"  [{stem}] z{minzoom}-{maxzoom}: fresh "
              f"({final.stat().st_size / 1048576:.1f} MB), skipping")
        return final

    # Build per-layer JSON layer specs. A per-layer "minzoom" here would
    # be silently ignored by tippecanoe (verified v2.78.0) — zoom gating
    # for soundings is instead stamped per-feature during consolidation
    # via the `tippecanoe.minzoom` extension (see soundg_rule).
    layer_args = []
    for f in merged_files:
        spec = {"file": str(f), "layer": f.stem}
        layer_args.extend(["-L", json.dumps(spec)])

    print(f"tippecanoe [{stem}]: {len(merged_files)} layers, "
          f"z{minzoom}-{maxzoom}")

    tmp = (tile_dir / f".tmp-{stem}").resolve()
    tmp.mkdir(exist_ok=True)

    cmd = [
        "tippecanoe",
        "-o", str(final),
        "-Z", str(minzoom), "-z", str(maxzoom),
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--no-simplification",
        "--no-tiny-polygon-reduction",
        "--detect-shared-borders",
        "--buffer=80",
        "--force",
        "--temporary-directory", str(tmp),
        *(["--drop-rate", str(drop_rate)] if drop_rate is not None else []),
        *layer_args,
    ]
    result = subprocess.run(cmd)
    shutil.rmtree(tmp, ignore_errors=True)

    if result.returncode != 0 or not final.exists() or final.stat().st_size == 0:
        if final.exists():
            final.unlink()
        raise RuntimeError(f"tippecanoe failed for {stem}")

    _patch_metadata(final, stem, effective_drop_rate)
    print(f"  [{stem}] done ({final.stat().st_size / 1048576:.1f} MB)")
    return final


# ---------------------------------------------------------------------------
# Stage 3b: finer-wins erase (by-band mode)
# ---------------------------------------------------------------------------
# tile-join does NOT let a later input win where tilesets overlap. When two
# inputs carry the same tile and the same layer, their features are
# concatenated into one layer — its README: "If they define the same
# layers or the same tiles, the layers or tiles are merged" (verified
# against v2.78.0). So every overlap between one band's extended zooms and
# the next band's native zooms shipped BOTH charts' features in the same
# tiles, with a draw order that is not controllable and flips between
# tiles. Opaque depth-area fills then showed the coarse chart on top in
# some tiles and the fine chart in their neighbours (the Sept 2026
# tile-shaped-patches regression around Block Island).
#
# What an ECDIS does instead: a coarser chart is shown only where no finer
# chart has coverage. This stage reproduces that. Every render source (a
# band, or a gap-fill group) has a priority. Before a source is tiled at
# zooms where higher-priority sources also render, the union of those
# sources' chart footprints — the M_COVR objects with CATCOV=1 that every
# S-57 cell carries — is erased from its features. The erased copy lives
# in its own merged dir and is tiled separately; the pristine merged dir
# and its tippecanoe output still serve the source's native zooms.
#
# Mechanics: the footprint union and its complement are computed once per
# (source, erasing set) with GDAL's SQLite dialect (ST_Union/ST_Difference
# need SpatiaLite or GEOS in the GDAL build; Ubuntu's gdal-bin has it).
# Each merged layer is then streamed through `ogr2ogr -clipsrc
# <complement>` as GeoJSONSeq, so a layer of any size is clipped without
# being loaded into memory. Attribute names pass GeoJSON→GeoJSON unchanged;
# the SQLite route would launder them to lower case, which is why it is
# used only for the mask. Points inside a finer footprint are dropped,
# lines and polygons are cut at the footprint edge.

WORLD_WKT = "POLYGON((-180 -90,180 -90,180 90,-180 90,-180 -90))"

@dataclass
class RenderSource:
    """One consolidated GeoJSON set that renders over zoom_range.

    priority is the band of the data: the band number for a band, and
    cellband - 0.1 for a gap fill (so a fill of band 4 cells loses to the
    band 4 run of the same cells and is never tiled twice, but beats band
    3 and below). native_zoom is the band's own NOAA zoom range, None for
    fills. At any zoom inside its native range a band outranks everything
    (see priority_at): the chart NOAA compiled for that scale is shown
    wherever it has coverage, fills and extensions only where it does not."""
    priority: float
    label: str
    merged_dir: Path
    footprint_files: List[Path]
    zoom_range: Tuple[int, int]
    layer_rules: Dict[str, ZoomRule]
    native_zoom: Optional[Tuple[int, int]] = None


def priority_at(src: RenderSource, z: int) -> float:
    """Effective priority of a source at zoom z: the native band for that
    zoom wins outright; otherwise finer data wins, above the native range
    (BAND_ZOOM_EXTENSION) and below it (BAND_ZOOM_DOWNWARD) alike."""
    if src.native_zoom and src.native_zoom[0] <= z <= src.native_zoom[1]:
        return src.priority + 10
    return src.priority


@dataclass
class RenderRun:
    """One tippecanoe invocation: a source over a zoom sub-range, with
    the footprints of `erased_by` removed from its features first."""
    source: RenderSource
    zoom_range: Tuple[int, int]
    erased_by: List[RenderSource]

    @property
    def stem(self) -> str:
        s = self.source.label
        if self.zoom_range != self.source.zoom_range:
            s += f"_z{self.zoom_range[0]}-{self.zoom_range[1]}"
        if self.erased_by:
            s += ".minus-" + "+".join(e.label for e in self.erased_by)
        return s


class GdalRunner:
    """Builds ogr2ogr command lines for native or containerized GDAL. In
    container mode data_dir is mounted at /data and Path arguments under
    it are rewritten, so every file GDAL touches must live under it."""

    def __init__(self, native: bool, runtime: Optional[str], data_dir: Path):
        self.native = native
        self.runtime = runtime
        self.data_dir = data_dir.resolve()

    def cmd(self, args: list) -> List[str]:
        if self.native:
            return [str(a) for a in args]
        out = []
        for a in args:
            if isinstance(a, Path):
                rel = a.resolve().relative_to(self.data_dir)
                out.append(f"/data/{rel.as_posix()}")
            else:
                out.append(str(a))
        return [self.runtime, "run", "--rm",
                "-v", f"{self.data_dir}:/data:Z", GDAL_IMAGE] + out


def plan_render_runs(sources: List[RenderSource]) -> List[RenderRun]:
    """Split each source's zoom range into runs of consecutive zooms that
    share the same set of higher-priority sources rendering there
    (priority_at: native band first, then finer data). A run with no such
    sources is tiled as-is; the others get the erase.
    Ordered coarse → fine for tile-join (order no longer decides what
    wins, but it keeps the input list readable)."""
    runs: List[RenderRun] = []
    for src in sources:
        groups: List[list] = []  # [zmin, zmax, erasers]
        for z in range(src.zoom_range[0], src.zoom_range[1] + 1):
            erasers = [o for o in sources
                       if priority_at(o, z) > priority_at(src, z)
                       and o.zoom_range[0] <= z <= o.zoom_range[1]]
            key = [o.label for o in erasers]
            if groups and [o.label for o in groups[-1][2]] == key:
                groups[-1][1] = z
            else:
                groups.append([z, z, erasers])
        for zmin, zmax, erasers in groups:
            runs.append(RenderRun(src, (zmin, zmax), erasers))
    runs.sort(key=lambda r: (r.source.priority, r.zoom_range[0]))
    return runs


def load_footprints(files: List[Path]) -> List[dict]:
    """Geometry-only features for every M_COVR object with CATCOV=1
    (area covered by the cell's data) in the given GeoJSON files."""
    feats = []
    for f in files:
        try:
            fc = json.load(open(f))
        except (json.JSONDecodeError, OSError):
            continue
        for ft in fc.get("features", []):
            if (ft.get("properties", {}).get("CATCOV") == 1
                    and ft.get("geometry")):
                feats.append({"type": "Feature", "properties": {},
                              "geometry": ft["geometry"]})
    return feats


def build_clip_complement(footprint_files: List[Path], labels: str,
                          mask_dir: Path, gdal: GdalRunner) -> Path:
    """Write mask_dir/clip.geojson: one polygon feature covering the world
    minus the union of the CATCOV=1 footprints in footprint_files. The
    file is only replaced when its content changes, so mtime-based
    freshness of the clipped layers keeps working across runs. The mask
    files live in their own subdirectory because tippecanoe is fed every
    *.geojson in a merged dir — a world-sized clip polygon must never
    become a layer. labels names the erasers for messages."""
    mask_dir.mkdir(parents=True, exist_ok=True)
    feats: List[dict] = load_footprints(footprint_files)
    if not feats:
        raise RuntimeError(
            f"no M_COVR (CATCOV=1) footprints found for {labels}; "
            "cannot erase finer coverage")

    mask = mask_dir / "footprints.geojson"
    with open(mask, "w") as out:
        json.dump({"type": "FeatureCollection", "features": feats}, out)

    tmp = mask_dir / "clip.tmp.geojson"
    if tmp.exists():
        tmp.unlink()
    sql = (f"SELECT ST_Difference(ST_GeomFromText('{WORLD_WKT}'), "
           f"ST_Union(geometry)) AS geometry FROM {mask.stem}")
    result = subprocess.run(
        gdal.cmd(["ogr2ogr", "-f", "GeoJSON", tmp, mask,
                  "-dialect", "SQLITE", "-sql", sql]),
        capture_output=True, text=True)
    ok = result.returncode == 0 and tmp.exists()
    if ok:
        try:
            geom = json.load(open(tmp))["features"][0]["geometry"]
            ok = bool(geom and geom.get("coordinates"))
        except (json.JSONDecodeError, OSError, KeyError, IndexError):
            ok = False
    if not ok:
        raise RuntimeError(
            "GDAL could not compute the footprint complement "
            f"(ST_Union/ST_Difference) for {labels}. This needs a GDAL "
            "build with SpatiaLite or GEOS spatial SQL functions.\n"
            f"{result.stderr.strip()}")

    clip = mask_dir / "clip.geojson"
    if clip.exists() and clip.read_bytes() == tmp.read_bytes():
        tmp.unlink()
    else:
        tmp.replace(clip)
    return clip


# S-57 PRIM attribute (present on every feature GDAL exports) → the
# GeoJSON geometry types that carry the same dimension.
_PRIM_TYPES: Dict[int, Tuple[str, ...]] = {
    1: ("Point", "MultiPoint"),
    2: ("LineString", "MultiLineString"),
    3: ("Polygon", "MultiPolygon"),
}
_GEOM_DIM = {"Point": 0, "MultiPoint": 0, "LineString": 1,
             "MultiLineString": 1, "Polygon": 2, "MultiPolygon": 2}


def _keep_own_dimension(feat: dict) -> bool:
    """Drop the lower-dimension debris a clip leaves behind and return
    whether anything of the feature's own dimension survives.

    Where a polygon's edge coincides with the erase boundary (a cell
    clipped by its own footprint, or two cells sharing an edge), GEOS
    returns the shared edge as a LineString and GDAL passes it through;
    a line touching the boundary likewise yields Points. The feature's
    S-57 PRIM (1 point, 2 line, 3 area) says what it really is, so parts
    of any other dimension are stripped, unwrapping GeometryCollections.
    Without PRIM, only the highest-dimension parts of a collection are
    kept."""
    geom = feat.get("geometry")
    if not geom:
        return False
    prim = feat.get("properties", {}).get("PRIM")
    parts = (geom["geometries"] if geom["type"] == "GeometryCollection"
             else [geom])
    parts = [g for g in parts if g.get("type") in _GEOM_DIM]
    if not parts:
        return False
    if prim in _PRIM_TYPES:
        want = _PRIM_TYPES[prim]
    else:
        top = max(_GEOM_DIM[g["type"]] for g in parts)
        want = tuple(t for t, d in _GEOM_DIM.items() if d == top)
    parts = [g for g in parts if g["type"] in want]
    if not parts:
        return False
    if len(parts) == 1:
        feat["geometry"] = parts[0]
    else:
        # Several same-dimension parts: fold into the Multi type.
        multi = want[1]
        coords = []
        for g in parts:
            if g["type"].startswith("Multi"):
                coords.extend(g["coordinates"])
            else:
                coords.append(g["coordinates"])
        feat["geometry"] = {"type": multi, "coordinates": coords}
    return True


def erase_layer(src_file: Path, clip: Path, out_path: Path,
                rule: Optional[ZoomRule], gdal: GdalRunner,
                note: Optional[str] = None) -> int:
    """Stream src_file through `ogr2ogr -clipsrc clip` and write the
    survivors as a FeatureCollection to out_path, re-applying the
    per-feature tippecanoe minzoom stamp that the round trip drops (the
    same rule as consolidation, so a thinned layer keeps the same pick)
    and discarding clip debris of the wrong dimension
    (_keep_own_dimension). Returns the number of features kept."""
    tmp = out_path.with_suffix(".tmp")
    err_path = out_path.with_suffix(".stderr")
    cmd = gdal.cmd(["ogr2ogr", "-f", "GeoJSONSeq", "/vsistdout/",
                    src_file, "-clipsrc", clip])
    n = 0
    with open(err_path, "w") as err, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err) as proc, \
            open(tmp, "w") as out:
        out.write('{"type":"FeatureCollection","features":[\n')
        for raw in proc.stdout:
            line = raw.strip().lstrip(b"\x1e")
            if not line:
                continue
            feat = json.loads(line)
            if not _keep_own_dimension(feat):
                continue
            stamp_feature(feat, rule)
            if n:
                out.write(",\n")
            json.dump(feat, out)
            n += 1
        out.write("\n]}\n")
    rc = proc.returncode
    if rc != 0:
        msg = err_path.read_text().strip()
        tmp.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
        raise RuntimeError(f"ogr2ogr -clipsrc failed on {src_file.name}: "
                           f"{msg}")
    err_path.unlink(missing_ok=True)
    if n == 0:
        # Valid, feature-less GeoJSON padded past the 100-byte threshold
        # output_is_fresh uses to tell a real output from a truncated one,
        # so a fully-erased layer is not re-clipped on every run.
        with open(tmp, "w") as out:
            json.dump({"type": "FeatureCollection",
                       "note": note or (f"every {src_file.stem} feature lies "
                                        "inside finer-chart coverage; "
                                        "nothing to render"),
                       "features": []}, out)
    tmp.replace(out_path)
    return n


def erase_for_run(run: RenderRun, data_dir: Path, gdal: GdalRunner,
                  max_workers: int) -> Path:
    """Produce (or refresh) the erased merged dir for a run and return
    it. Layers are re-clipped only when the source layer or the clip
    polygon changed; outputs whose source layer vanished are removed."""
    src = run.source
    erase_dir = data_dir / "merged" / run.stem
    clip = build_clip_complement(
        [f for e in run.erased_by for f in e.footprint_files],
        ", ".join(e.label for e in run.erased_by), erase_dir / "mask", gdal)

    merged_files = [f for f in sorted(src.merged_dir.glob("*.geojson"))
                    if f.stat().st_size > 100]
    wanted = {f.name for f in merged_files}
    for stale_out in erase_dir.glob("*.geojson"):
        if stale_out.name not in wanted:
            stale_out.unlink()

    todo = [f for f in merged_files
            if not output_is_fresh(erase_dir / f.name, [f, clip])]
    fresh = len(merged_files) - len(todo)
    labels = "+".join(e.label for e in run.erased_by)
    if not todo:
        print(f"  [{src.label}] z{run.zoom_range[0]}-{run.zoom_range[1]}: "
              f"all {fresh} erased layers fresh (minus {labels})")
        return erase_dir
    print(f"  [{src.label}] z{run.zoom_range[0]}-{run.zoom_range[1]}: "
          f"erasing {labels} footprints from {len(todo)} layer(s) "
          f"({fresh} fresh)...")

    def one(f: Path) -> Tuple[str, int]:
        """Erase one merged layer; (layer name, features kept)."""
        rule = src.layer_rules.get(f.stem)
        return f.stem, erase_layer(f, clip, erase_dir / f.name, rule, gdal)

    kept: Dict[str, int] = {}
    if max_workers <= 1:
        for f in todo:
            name, n = one(f)
            kept[name] = n
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(one, f): f for f in todo}
            for fut in as_completed(futures):
                exc = fut.exception()
                if exc:
                    raise RuntimeError(
                        f"erase failed for {futures[fut].name}: {exc}")
                name, n = fut.result()
                kept[name] = n
    empty = sorted(k for k, v in kept.items() if v == 0)
    print(f"  [{src.label}] erased {len(kept)} layer(s); "
          f"{len(empty)} fully inside finer coverage"
          + (f" ({', '.join(empty[:6])}{'…' if len(empty) > 6 else ''})"
             if empty else ""))
    return erase_dir


def _patch_metadata(mbtiles_path: Path, name: str,
                    drop_rate: Optional[float] = None,
                    scale: Optional[int] = None):
    """Stamp type/name/description and, when given, the `drop_rate` the
    file was rendered with (read back by _mbtiles_drop_rate for the
    freshness check in run_tippecanoe_for_source) and the bundle's
    `scale` (bundle_scale; Signal K chart plugins read this row and
    default to 250000 without it)."""
    db = sqlite3.connect(str(mbtiles_path))
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS name ON metadata (name)")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('type', 'S-57')")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('name', ?)", (name,))
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('description', ?)", (name,))
    if drop_rate is not None:
        db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                   "VALUES ('drop_rate', ?)", (repr(float(drop_rate)),))
    if scale is not None:
        db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                   "VALUES ('scale', ?)", (str(int(scale)),))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# District-region clipping (by-band mode)
# ---------------------------------------------------------------------------
# BAND_ZOOM_EXTENSION renders low bands into deeper zooms, but band 1/2
# "sailing" cells can span entire ocean basins (US1PO02M covers the whole
# North Pacific), which planted planet-wide z9/z10 tiles and -180..180
# bounds metadata in the Pacific district files (Aug 2026 regression).
# The district's regional extent is defined as the union of its own
# band>=3 (coastal/approach/harbour) chart footprints — never band 1/2 —
# rasterized as a tile mask at REGION_MASK_ZOOM and dilated by one tile
# for margin at the edges. After the per-band tippecanoe runs, each band
# 1/2 file is copied to a derived *.region.mbtiles, tiles outside the
# mask are deleted from the copy and its bounds metadata recomputed from
# the survivors, and tile-join consumes the copy — so the final file's
# tile pyramid AND declared bounds are regional while the cached
# tippecanoe outputs stay pristine for resume. This
# naturally keeps multi-part regions (14CGD = Hawaii + Guam + Samoa)
# because the mask is a tile set, not a single bbox.

REGION_MASK_ZOOM = 11


def _tile_lonlat_bounds(z: int, x: int, tms_y: int
                        ) -> Tuple[float, float, float, float]:
    """(w, s, e, n) lon/lat bounds of an mbtiles tile (TMS row order)."""
    import math
    n = 1 << z
    y = n - 1 - tms_y  # TMS -> XYZ row
    w = x / n * 360.0 - 180.0
    e = (x + 1) / n * 360.0 - 180.0

    def lat(yy: int) -> float:
        t = math.pi - 2.0 * math.pi * yy / n
        return math.degrees(math.atan(math.sinh(t)))

    return w, lat(y + 1), e, lat(y)


def _tile_table(db: sqlite3.Connection) -> str:
    """Name of the table holding tile addresses: tippecanoe's normalized
    `map` table when present, else the plain `tiles` table."""
    has_map = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='map'"
    ).fetchone()
    return "map" if has_map else "tiles"


def _tile_coords(db: sqlite3.Connection) -> List[Tuple[int, int, int]]:
    """All (zoom, column, row) in an mbtiles, via map table or tiles."""
    return list(db.execute(
        f"SELECT zoom_level, tile_column, tile_row FROM {_tile_table(db)}"))


def _region_mask(detail_tiles: List[Path]) -> set:
    """Union of the given (band>=3) tilesets' coverage as (x, y) pairs at
    REGION_MASK_ZOOM, dilated by one tile. TMS rows throughout — ancestor
    math (right-shift) is row-order-agnostic.

    Only each file's bottom-zoom rows are read: coverage at the bottom
    zoom already spans the file's footprint, and streaming just that level
    avoids materializing the millions of z16 rows a district's band 4/5
    files carry."""
    mask: set = set()
    for path in detail_tiles:
        db = sqlite3.connect(str(path))
        table = _tile_table(db)
        zmin = db.execute(f"SELECT MIN(zoom_level) FROM {table}").fetchone()[0]
        if zmin is None or zmin < REGION_MASK_ZOOM:
            db.close()
            continue
        shift = zmin - REGION_MASK_ZOOM
        for x, y in db.execute(
                f"SELECT DISTINCT tile_column, tile_row FROM {table} "
                "WHERE zoom_level = ?", (zmin,)):
            mask.add((x >> shift, y >> shift))
        db.close()
    # Dilate by one tile (~20 km at z11) so band 1/2 coverage doesn't get
    # shaved right at the region's edge.
    n = 1 << REGION_MASK_ZOOM
    dilated = set()
    for x, y in mask:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                dilated.add(((x + dx) % n, min(max(y + dy, 0), n - 1)))
    return dilated


def _recompute_bounds(db: sqlite3.Connection) -> Optional[str]:
    """Bounds string 'w,s,e,n' from the tiles actually present, using the
    deepest zoom level (tightest tile granularity)."""
    coords = _tile_coords(db)
    if not coords:
        return None
    zmax = max(c[0] for c in coords)
    deep = [c for c in coords if c[0] == zmax]
    w = min(_tile_lonlat_bounds(z, x, y)[0] for z, x, y in deep)
    s = min(_tile_lonlat_bounds(z, x, y)[1] for z, x, y in deep)
    e = max(_tile_lonlat_bounds(z, x, y)[2] for z, x, y in deep)
    n = max(_tile_lonlat_bounds(z, x, y)[3] for z, x, y in deep)
    return f"{w},{s},{e},{n}"


def trim_low_bands_to_region(tiles: List[Tuple[float, Path]]
                             ) -> List[Tuple[float, Path]]:
    """Return the (priority, mbtiles) list to feed tile-join, with entries
    below band 3 (bands 1/2 and gap fills) replaced by copies clipped to
    the district region.

    The tippecanoe outputs are never modified: they are the cached build
    artifacts that the resume logic in run_tippecanoe_for_source relies
    on, and a clip that deleted rows in place could never be undone when
    a later run's band>=3 inputs widen the region. Clipping is cheap (low
    band files are small), so it is treated like the other cheap stages —
    always recomputed, into a separate *.region.mbtiles derived file. A
    low entry left with no tiles inside the region is dropped from the
    returned list. Band >= 3 entries pass through untouched. When there is
    nothing to clip against, the input list is returned unchanged."""
    low = [(p, path) for p, path in tiles if p < 3]
    detail = [path for p, path in tiles if p >= 3]
    if not low:
        return list(tiles)
    if not detail:
        print("WARNING: no band>=3 charts to define the district region; "
              "skipping overview-band clipping", file=sys.stderr)
        return list(tiles)

    mask = _region_mask(detail)
    if not mask:
        print("WARNING: empty district-region mask; skipping clipping",
              file=sys.stderr)
        return list(tiles)

    # Ancestor masks for zooms coarser than the mask zoom.
    anc: Dict[int, set] = {}
    for d in range(1, REGION_MASK_ZOOM + 1):
        anc[d] = {(x >> d, y >> d) for x, y in mask}

    result: List[Tuple[float, Path]] = []
    for prio, src in tiles:
        if prio >= 3:
            result.append((prio, src))
            continue
        dst = src.with_name(f"{src.stem}.region{src.suffix}")
        shutil.copy2(src, dst)
        db = sqlite3.connect(str(dst))
        table = _tile_table(db)
        doomed = []
        for z, x, y in _tile_coords(db):
            if z >= REGION_MASK_ZOOM:
                s = z - REGION_MASK_ZOOM
                keep = (x >> s, y >> s) in mask
            else:
                keep = (x, y) in anc[REGION_MASK_ZOOM - z]
            if not keep:
                doomed.append((z, x, y))
        db.executemany(
            f"DELETE FROM {table} WHERE zoom_level=? AND tile_column=? "
            "AND tile_row=?", doomed)
        if table == "map":
            db.execute("DELETE FROM images WHERE tile_id NOT IN "
                       "(SELECT DISTINCT tile_id FROM map)")
        bounds = _recompute_bounds(db)
        if bounds:
            db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                       "VALUES ('bounds', ?)", (bounds,))
            w, s_, e, n = (float(v) for v in bounds.split(","))
            db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                       "VALUES ('center', ?)",
                       (f"{(w + e) / 2},{(s_ + n) / 2},{REGION_MASK_ZOOM}",))
        else:
            # Nothing survived: drop the stale (planet-wide) bounds rather
            # than leave them describing tiles that no longer exist.
            db.execute("DELETE FROM metadata WHERE name IN "
                       "('bounds', 'center')")
        db.commit()
        db.execute("VACUUM")
        db.close()
        if bounds:
            print(f"  [{src.stem}] clipped {len(doomed)} out-of-region "
                  f"tile(s) -> {dst.name}, bounds -> {bounds}")
            result.append((prio, dst))
        else:
            print(f"  [{src.stem}] clipped all {len(doomed)} tile(s) as "
                  f"out-of-region; excluding empty tileset from merge")
    return result


# ---------------------------------------------------------------------------
# Stage 5: tile-join merge
# ---------------------------------------------------------------------------

def merge_mbtiles(tile_files: List[Path], output_path: Path, final_name: str,
                  scale: Optional[int] = None):
    print(f"\nMerging {len(tile_files)} tile set(s) with tile-join...")
    cmd = [
        "tile-join",
        "--no-tile-size-limit",
        "--force",
        "-o", str(output_path),
        *[str(f) for f in tile_files],
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("ERROR: tile-join failed", file=sys.stderr)
        sys.exit(1)
    _patch_metadata(output_path, final_name, scale=scale)
    print(f"Merged -> {output_path} ({output_path.stat().st_size / 1048576:.1f} MB)")


def write_pmtiles(mbtiles_path: Path) -> Path:
    """Write a PMTiles archive beside the final MBTiles with go-pmtiles'
    `pmtiles convert`. The MBTiles stays the master: the gzipped MVT
    blobs are copied unchanged (deduplicated and clustered), and the
    metadata table — bounds, zooms, `vector_layers` and the `type=S-57`
    stamp from _patch_metadata — becomes the archive's JSON metadata.
    tile-join can also emit PMTiles, but it resets `type` to `overlay`
    and an archive's JSON metadata cannot be patched in place, so the
    conversion runs after the stamp. Regenerated every run, like the
    final MBTiles it mirrors."""
    out = mbtiles_path.with_suffix(".pmtiles")
    tmp = out.with_name(out.name + ".tmp")
    cmd = ["pmtiles", "convert", str(mbtiles_path), str(tmp), "--force",
           "--tmpdir", str(mbtiles_path.parent)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0 and tmp.exists()
    if ok:
        with open(tmp, "rb") as f:
            ok = f.read(7) == b"PMTiles"
    if not ok:
        tmp.unlink(missing_ok=True)
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        print("ERROR: pmtiles convert failed"
              + (f": {tail[-1]}" if tail else ""), file=sys.stderr)
        sys.exit(1)
    os.replace(tmp, out)
    print(f"PMTiles -> {out} ({out.stat().st_size / 1048576:.1f} MB)")
    return out


# ---------------------------------------------------------------------------
# Gap fill: render high-band detail cells at lower zooms to cover
#           documented NOAA legacy-ENC coverage holes.
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# NOAA's legacy ENCs were compiled cell-by-cell from paper-chart sources
# at fixed scales over many years. The result is genuine discontinuities
# in the lower-band overview cells: certain coastal stretches have band
# 3+ detail cells but no band 2 (z=9-10) cell at all, or have band 2 and
# band 3 catalog cells whose actual polygons stop short of the coast,
# leaving holes at z=9-12.
#
# NOAA acknowledges this and is rebuilding the ENC catalog as a continuous
# gridded dataset under the multi-year ENC Rescheming Project, working
# from band 5 outward (band 5 first, then 4, 3, 2, 1). Until band 2 is
# fully reschemed, this script's "gap fill" pass synthesizes the missing
# overview/general/coastal coverage by rendering the band 3-5 detail
# cells at lower zooms.
#
# How it works
# ------------
# Each `gap_fills:` group in enc-sources.yaml lists a set of higher-band
# cell IDs and a zoom_range. prepare_gap_fill_sources():
#   1. Globs the existing per-band GeoJSON output (data/geojson/band*/)
#      for files matching each cell ID. Cells not present in this build's
#      input are silently skipped.
#   2. Consolidates the matching per-cell GeoJSON into a per-group merged
#      directory, grouped by layer name.
#   3. Returns the group as a RenderSource with priority cellband - 0.1.
#
# process_by_band then treats fills like any other source. At every zoom
# the band NOAA compiled for that zoom outranks everything, and among the
# rest finer data wins (priority_at); every higher-ranked source's chart
# footprints are erased from the fill before tippecanoe (Stage 3b). So a
# fill shows only where the natural pipeline has no chart for that zoom —
# the effect the old design wrongly expected from tile-join's input
# order — and a fill of band N cells is fully erased wherever band N
# itself renders, so the same cells are never tiled twice. Fills of the
# same cell band do not erase each other; keep such groups disjoint.
#
# References (full context in the gap_fills: comment block of
# enc-sources.yaml):
#   • Rescheming program:
#       https://nauticalcharts.noaa.gov/charts/rescheming-and-improving-electronic-navigational-charts.html
#   • Cell creation status map:
#       https://nauticalcharts.noaa.gov/updates/follow-the-status-of-electronic-navigational-chart-improvements-with-noaas-new-map-viewer/

def prepare_gap_fill_sources(
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
) -> List[RenderSource]:
    """Consolidate every configured gap-fill group that has cells in this
    build and return them as render sources (empty list if none)."""
    groups = load_gap_fill_config()
    if not groups:
        return []

    geojson_root = data_dir / "geojson"
    if not geojson_root.is_dir():
        return []

    sources: List[RenderSource] = []
    for group in groups:
        src = _prepare_gap_fill_group(
            group, data_dir, geojson_root, minzoom, maxzoom)
        if src is not None:
            sources.append(src)
    return sources


def _prepare_gap_fill_group(
    group: GapFillGroup,
    data_dir: Path,
    geojson_root: Path,
    minzoom: int,
    maxzoom: int,
) -> Optional[RenderSource]:
    """Consolidate one gap-fill group's cells into a render source."""
    effective_min = max(group.zoom_range[0], minzoom)
    effective_max = min(group.zoom_range[1], maxzoom)
    if effective_min > effective_max:
        return None

    # Cells live in whichever band's geojson dir matches their ID prefix.
    # We just glob across all band* dirs to find each requested cell —
    # cells absent from this build's ENC input produce no matches and
    # are silently skipped.
    # A configured cell NOAA has since cancelled is followed to its live
    # successors (recorded by fetch_replacement_cells): on 2026-09-05
    # east_maine_offshore_band3 pointed at cancelled US3EC11M and silently
    # rendered nothing, blanking the Gulf of Maine at z15-16.
    successors: Dict[str, List[str]] = {}
    try:
        successors = json.loads(
            (data_dir / "replacement-cells.json").read_text())["successors"]
    except (OSError, ValueError, KeyError):
        pass
    wanted_cells: List[str] = []
    for cell_id in group.cells:
        if cell_id in successors:
            print(f"   [{group.name}] {cell_id} is cancelled; using "
                  f"successors {' '.join(successors[cell_id]) or '(none)'}")
            wanted_cells.extend(successors[cell_id])
        else:
            wanted_cells.append(cell_id)

    cell_files: Dict[str, List[Path]] = {}
    for cell_id in dict.fromkeys(wanted_cells):
        matches: List[Path] = []
        for band_dir in sorted(geojson_root.glob("band*")):
            # bandN only: bandN.resolved holds Stage 2b's clipped copies
            if band_dir.is_dir() and re.fullmatch(r"band\d+", band_dir.name):
                matches.extend(band_dir.glob(f"*_{cell_id}.geojson"))
        if matches:
            cell_files[cell_id] = matches

    if not cell_files:
        return None

    present = sorted(cell_files)
    print(f"\n-- Gap fill [{group.name}]: {len(present)} cell(s) at "
          f"z{effective_min}-{effective_max} --")
    print(f"   Cells: {', '.join(present)}")

    # Group per-cell GeoJSON files by layer name. The naming convention
    # comes from prepare_band's GDAL export step: "LAYER_CELLSTEM.geojson"
    # (layer names may themselves contain underscores, see
    # layer_name_from_stem).
    layer_groups: Dict[str, List[Path]] = {}
    for files in cell_files.values():
        for f in files:
            if f.stat().st_size <= 100:
                continue
            layer_groups.setdefault(layer_name_from_stem(f.stem), []).append(f)

    if not layer_groups:
        return None

    safe_name = re.sub(r'[^\w\-.]', '_', group.name)
    merged_dir = data_dir / "merged" / f"gapfill-{safe_name}"
    merged_dir.mkdir(parents=True, exist_ok=True)
    _remove_orphan_layers(merged_dir, set(layer_groups))

    # Priority just below the band of the group's finest cells: the fill
    # loses to that band's own run (same cells, never tiled twice) and to
    # the native band of each zoom, and beats every coarser band.
    cell_band = max((enc_band(Path(c)) or 3) for c in present)

    # Sounding zoom rule from the group's configured bottom zoom
    # (config-static, so cached merges stay valid).
    layer_rules = layer_zoom_rules(cell_band, group.zoom_range[0])
    force_stale = _stamp_marker_stale(merged_dir, layer_rules)

    for layer_name, files in layer_groups.items():
        out_path = merged_dir / f"{layer_name}.geojson"
        if not force_stale and output_is_fresh(out_path, files):
            continue
        rule = layer_rules.get(layer_name)
        if len(files) == 1 and rule is None:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path, rule=rule)

    # Footprints straight from the per-cell files: layer_groups keys are
    # the pre-underscore stem, which folds every M_* layer into "M".
    footprints = sorted(f for files in cell_files.values() for f in files
                        if f.name.startswith("M_COVR_"))
    return RenderSource(
        priority=cell_band - 0.1,
        label=f"gapfill-{safe_name}",
        merged_dir=merged_dir,
        footprint_files=footprints,
        zoom_range=(effective_min, effective_max),
        layer_rules=layer_rules)


# ---------------------------------------------------------------------------
# Stage 2b: same-band overlap resolution (by-band mode)
# ---------------------------------------------------------------------------
# NOAA's rescheming ships legacy and reschemed cells of the SAME usage band
# that overlap with M_COVR CATCOV=1 coverage in both and carry the same
# objects (same LNAM) — an IHO S-57 App. B.1 §2.2 violation ("in the area
# of overlap only one cell may contain data"). Consolidation concatenates
# every cell of a band, so the overlap became exact duplicate features in
# the tiles (Chicago z10: 51; Block Island z10: 27). Stage 3b cannot see
# it: both cells have the same band priority. Full evidence, standards and
# the rejected alternatives (issue date, compilation scale — both wrong on
# the data) are in docs/SAME-BAND-OVERLAP.md.
#
# Rule: within a band the reschemed cell wins. Every layer of a legacy
# cell — including its own M_COVR, so its CATCOV=1 polygon loses the
# overlap exactly as §2.2 requires of the producer — is clipped by the
# complement of the union of the overlapping reschemed cells' CATCOV=1
# footprints before consolidation. Cells are classified by name using
# NOAA's ENC Design Handbook (June 2024): legacy cells look like US2EC03M;
# reschemed cells carry an Annex A region code in characters 4-6. Only
# band 1 (GLB) and band 2 (ARC ANT ATL GRL GOM PAC) codes are unambiguous;
# bands 3-6 reuse state codes that legacy harbour cells also used, so
# overlaps there are detected and WARNED but never erased. Every overlap
# is recorded in data/merged/bandN/.same-band-overlaps.json for reporting
# to NOAA; once NOAA cuts back the legacy cells the rule has nothing to do.

RESCHEMED_REGION_CODES: Dict[int, set] = {
    1: {"GLB"},
    2: {"ARC", "ANT", "ATL", "GRL", "GOM", "PAC"},
}
# Real legacy/reschemed overlaps are whole-cell scale (tenths of deg² and
# up); the edge slivers legitimately shared by adjacent cells measure
# ~2e-6 deg². 1e-4 deg² is about 1 km² at mid latitudes.
SAME_BAND_OVERLAP_MIN_AREA = 1e-4
SAME_BAND_RULE = "reschemed-wins-v2"


def classify_cell(name: str) -> str:
    """'reschemed' when the name carries a Design Handbook Annex A region
    code for band 1 or 2 (GLB; ARC ANT ATL GRL GOM PAC), else 'other'.
    Only used for LIVE cells that overlap: cancelled cells are dropped at
    inventory (drop_cancelled_cells), which covered 444 of the 448 overlap
    pairs measured on 2026-09-05. The remaining live-vs-live case is the
    legacy US2EC03M under reschemed US2ATL* cells."""
    name = name.upper()
    band = enc_band(Path(name))
    if band in RESCHEMED_REGION_CODES and name[3:6] in RESCHEMED_REGION_CODES[band]:
        return "reschemed"
    return "other"


@dataclass
class OverlapPair:
    a: str
    b: str
    area_deg2: float  # NaN when the exact test could not run


def load_cell_footprints(band_geojson_dir: Path) -> Dict[str, List[dict]]:
    """cell name → its M_COVR CATCOV=1 features, from the per-cell exports.
    Single-cell exports carry no cell suffix and are skipped."""
    out: Dict[str, List[dict]] = {}
    for f in sorted(band_geojson_dir.glob("M_COVR_*.geojson")):
        cell = cell_name_from_stem(f.stem)
        if not cell:
            continue
        feats = load_footprints([f])
        if feats:
            out[cell] = feats
    return out


def _geom_bbox(feats: List[dict]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for k in c:
                walk(k)

    for ft in feats:
        walk(ft["geometry"]["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_candidates(bboxes: Dict[str, Tuple[float, float, float, float]],
                     eps: float = 1e-6) -> List[Tuple[str, str]]:
    """Cell pairs whose bounding boxes overlap by more than eps on both
    axes (exact grid-edge sharing is excluded)."""
    names = sorted(bboxes)
    pairs = []
    for i, a in enumerate(names):
        ax0, ay0, ax1, ay1 = bboxes[a]
        for b in names[i + 1:]:
            bx0, by0, bx1, by1 = bboxes[b]
            if (min(ax1, bx1) - max(ax0, bx0) > eps
                    and min(ay1, by1) - max(ay0, by0) > eps):
                pairs.append((a, b))
    return pairs


def find_same_band_overlaps(footprints: Dict[str, List[dict]],
                            candidates: List[Tuple[str, str]],
                            scratch_dir: Path, gdal: GdalRunner
                            ) -> Tuple[List[OverlapPair], bool]:
    """Exact CATCOV=1 overlap area for each candidate pair, via one GDAL
    SQLite-dialect self-join streamed as CSV. Returns (pairs, exact). If
    GDAL fails (e.g. an invalid legacy polygon), every candidate is
    returned with area NaN and exact=False: erasing by a footprint that
    does not really overlap removes nothing, so over-approximating is
    safe, but the record says the areas are unknown."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    cells = sorted({c for pair in candidates for c in pair})
    src = scratch_dir / "candidates.geojson"
    feats = [{"type": "Feature", "properties": {"cell": c},
              "geometry": ft["geometry"]}
             for c in cells for ft in footprints[c]]
    with open(src, "w") as out:
        json.dump({"type": "FeatureCollection", "features": feats}, out)
    sql = ("SELECT a.cell AS ca, b.cell AS cb, "
           "SUM(ST_Area(ST_Intersection(a.geometry, b.geometry))) AS area "
           "FROM candidates a, candidates b "
           "WHERE a.cell < b.cell AND ST_Intersects(a.geometry, b.geometry) "
           "GROUP BY a.cell, b.cell")
    res = subprocess.run(
        gdal.cmd(["ogr2ogr", "-f", "CSV", "/vsistdout/", src,
                  "-dialect", "SQLITE", "-sql", sql]),
        capture_output=True, text=True)
    if res.returncode != 0:
        print("WARNING: exact same-band overlap test failed; treating every "
              "bounding-box candidate as overlapping.\n"
              f"{res.stderr.strip()}", file=sys.stderr)
        return [OverlapPair(a, b, float("nan")) for a, b in candidates], False
    wanted = set(candidates)
    pairs: List[OverlapPair] = []
    for line in res.stdout.splitlines()[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            continue
        a, b = parts[0].upper(), parts[1].upper()
        try:
            area = float(parts[2]) if parts[2] else 0.0
        except ValueError:
            area = float("nan")
        key = (a, b) if (a, b) in wanted else ((b, a) if (b, a) in wanted else None)
        if key:
            pairs.append(OverlapPair(key[0], key[1], area))
    return pairs, True


def plan_same_band_resolution(pairs: List[OverlapPair]):
    """Split overlap pairs into resolved (legacy → sorted reschemed
    erasers), unresolved [(pair, reason)] and slivers [pair]."""
    resolved: Dict[str, List[str]] = {}
    unresolved: List[Tuple[OverlapPair, str]] = []
    slivers: List[OverlapPair] = []
    for p in pairs:
        area = p.area_deg2
        if area == area and area < SAME_BAND_OVERLAP_MIN_AREA:  # NaN-safe
            slivers.append(p)
            continue
        ka, kb = classify_cell(p.a), classify_cell(p.b)
        if {ka, kb} == {"other", "reschemed"}:
            legacy, resch = (p.a, p.b) if ka == "other" else (p.b, p.a)
            resolved.setdefault(legacy, []).append(resch)
        else:
            unresolved.append((p, f"{ka} x {kb}"))
    for k in resolved:
        resolved[k].sort()
    return resolved, unresolved, slivers


def _count_features(path: Path) -> int:
    try:
        with open(path) as f:
            return len(json.load(f).get("features", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0


def _write_if_changed(path: Path, text: str):
    try:
        if path.exists() and path.read_text() == text:
            return
    except OSError:
        pass
    path.write_text(text)


def _cleanup_resolved(resolved_dir: Path, keep: set):
    """Drop clipped outputs and mask dirs of cells that are no longer
    resolved (their overlap went away, or NOAA cut back the legacy cell)."""
    if not resolved_dir.is_dir():
        return
    for f in resolved_dir.iterdir():
        if f.is_file() and (f.suffix in (".geojson", ".tmp", ".stderr")):
            if cell_name_from_stem(f.stem) not in keep:
                f.unlink()
    mask_root = resolved_dir / "mask"
    if mask_root.is_dir():
        for d in mask_root.iterdir():
            if d.is_dir() and d.name.upper() not in keep:
                shutil.rmtree(d, ignore_errors=True)


def resolve_same_band_overlaps(band: int, label: str,
                               band_geojson_dir: Path, resolved_dir: Path,
                               record_path: Path, gdal: GdalRunner,
                               max_workers: int) -> Dict[str, Path]:
    """Stage 2b entry point. Returns the consolidation overrides map
    (per-cell filename → clipped copy) for every resolved legacy cell;
    empty when the band has no resolvable overlap."""
    record_path.parent.mkdir(parents=True, exist_ok=True)
    footprints = load_cell_footprints(band_geojson_dir)
    inputs = {}
    for c in footprints:
        st = (band_geojson_dir / f"M_COVR_{c}.geojson").stat()
        inputs[c] = [st.st_mtime_ns, st.st_size]
    base = {"rule": SAME_BAND_RULE, "band": band, "inputs": inputs}

    if len(footprints) < 2:
        _cleanup_resolved(resolved_dir, set())
        _write_if_changed(record_path, json.dumps(
            dict(base, exact=True, pairs=[], resolved=[], unresolved=[],
                 slivers=[]), indent=1, sort_keys=True))
        return {}

    # Detection is cached in the record: it only reruns when a cell's
    # M_COVR export changed (every layer of a cell is rewritten together).
    pairs: Optional[List[OverlapPair]] = None
    exact = True
    try:
        cached = json.loads(record_path.read_text())
        if (cached.get("rule") == SAME_BAND_RULE
                and cached.get("inputs") == inputs and "pairs" in cached):
            pairs = [OverlapPair(**p) for p in cached["pairs"]]
            exact = bool(cached.get("exact", True))
    except (OSError, ValueError, TypeError):
        pairs = None
    if pairs is None:
        bboxes = {c: _geom_bbox(g) for c, g in footprints.items()}
        cands = _bbox_candidates(bboxes)
        if cands:
            pairs, exact = find_same_band_overlaps(
                footprints, cands, resolved_dir / "mask", gdal)
        else:
            pairs, exact = [], True

    resolved, unresolved, slivers = plan_same_band_resolution(pairs)
    if resolved or unresolved:
        print(f"\n-- Band {band}: same-band overlap check (reschemed wins) --")
    for p, reason in unresolved:
        area = "unknown" if p.area_deg2 != p.area_deg2 else f"{p.area_deg2:.4f} deg²"
        print(f"WARNING: [{label}] same-band overlap not resolved ({reason}): "
              f"{p.a} x {p.b}, {area}", file=sys.stderr)

    _cleanup_resolved(resolved_dir, set(resolved))
    overrides: Dict[str, Path] = {}
    report = []
    area_of = {(p.a, p.b): p.area_deg2 for p in pairs}
    area_of.update({(p.b, p.a): p.area_deg2 for p in pairs})
    for legacy, erasers in sorted(resolved.items()):
        resolved_dir.mkdir(parents=True, exist_ok=True)
        tag = "+".join(erasers)
        clip = build_clip_complement(
            [band_geojson_dir / f"M_COVR_{r}.geojson" for r in erasers],
            tag, resolved_dir / "mask" / legacy, gdal)
        srcs = [f for f in sorted(band_geojson_dir.glob(f"*_{legacy}.geojson"))
                if f.stat().st_size > 100]
        todo = [f for f in srcs
                if not output_is_fresh(resolved_dir / f.name, [f, clip])]
        fresh = len(srcs) - len(todo)
        if todo:
            print(f"  [{label}] {legacy} (legacy) under {tag}: clipping "
                  f"{len(todo)} layer(s) ({fresh} fresh)...")
        else:
            print(f"  [{label}] {legacy} (legacy) under {tag}: all {fresh} "
                  "clipped layers fresh")
        note = (f"every {legacy} feature of this layer lies inside reschemed "
                f"coverage ({tag}); removed under S-57 App. B.1 2.2")

        def one(f: Path) -> int:
            return erase_layer(f, clip, resolved_dir / f.name, None, gdal,
                               note=note)

        if max_workers <= 1 or len(todo) <= 1:
            for f in todo:
                one(f)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(one, f): f for f in todo}
                for fut in as_completed(futures):
                    exc = fut.exception()
                    if exc:
                        raise RuntimeError(
                            f"same-band clip failed for {futures[fut].name}: {exc}")

        before = {f.stem: _count_features(f) for f in srcs}
        after = {f.stem: _count_features(resolved_dir / f.name) for f in srcs}
        removed_by_layer = {}
        emptied = []
        for stem in before:
            layer = layer_name_from_stem(stem)
            gone = before[stem] - after[stem]
            if gone > 0:
                removed_by_layer[layer] = gone
            if after[stem] == 0:
                emptied.append(layer)
        removed = sum(removed_by_layer.values())
        total = sum(before.values())
        print(f"  [{label}] {legacy}: removed {removed:,} of {total:,} features; "
              f"{len(emptied)} layer(s) emptied"
              + (f" ({', '.join(sorted(emptied)[:6])}"
                 f"{'…' if len(emptied) > 6 else ''})" if emptied else ""))
        for f in srcs:
            overrides[f.name] = resolved_dir / f.name
        report.append({
            "legacy": legacy, "reschemed": erasers,
            "overlap_deg2": {r: area_of.get((legacy, r)) for r in erasers},
            "layers_clipped": len(srcs), "features_before": total,
            "features_removed": removed, "layers_emptied": sorted(emptied),
            "removed_by_layer": dict(sorted(removed_by_layer.items())),
        })

    def _pair(p: OverlapPair):
        return {"cells": [p.a, p.b],
                "overlap_deg2": None if p.area_deg2 != p.area_deg2 else p.area_deg2}

    record = dict(base, exact=exact,
                  pairs=[{"a": p.a, "b": p.b, "area_deg2": p.area_deg2}
                         for p in pairs],
                  resolved=report,
                  unresolved=[dict(_pair(p), reason=r) for p, r in unresolved],
                  slivers=[_pair(p) for p in slivers])
    _write_if_changed(record_path, json.dumps(record, indent=1, sort_keys=True))
    return overrides



# ---------------------------------------------------------------------------
# Band pipeline (stages 2-3 for one band)
# ---------------------------------------------------------------------------

def prepare_band(
    band: int,
    enc_files: List[Path],
    data_dir: Path,
    effective_min: int,
    effective_max: int,
    desc: str,
    scale: str,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
) -> Optional[RenderSource]:
    """Run stages 2-3 (GDAL export, consolidate) for a single band and
    return it as a render source; tiling happens in process_by_band once
    every band's footprints are known."""
    label = f"band{band}-{desc}"
    print(f"\n-- Band {band}: {desc} ({scale})  z{effective_min}-{effective_max} --")

    enc_base = data_dir / "enc"
    geojson_base = data_dir / "geojson"
    merged_base = data_dir / "merged"

    band_enc_dir = enc_base / f"band{band}"
    band_geojson_dir = geojson_base / f"band{band}"
    band_merged_dir = merged_base / f"band{band}"

    band_enc_dir.mkdir(parents=True, exist_ok=True)
    band_geojson_dir.mkdir(parents=True, exist_ok=True)

    # Copy each cell's .000 base AND any .001..NNN ER update files.
    # Without the update files, ogr2ogr applies only the base — incremental
    # NOAA chart updates would be silently dropped.
    band_cells: List[Path] = []
    for enc_file in enc_files:
        cell_stem = enc_file.stem
        for src in enc_file.parent.glob(f"{cell_stem}.*"):
            dest = band_enc_dir / src.name
            if (not dest.exists()
                    or src.stat().st_mtime > dest.stat().st_mtime):
                shutil.copy2(src, dest)
        band_cells.append(band_enc_dir / enc_file.name)

    # Cells cancelled or removed since the last run must not linger.
    _remove_orphan_cells(band_geojson_dir, {c.stem.upper() for c in band_cells})

    # Stage 2: GDAL export
    export_to_geojson(
        band_enc_dir, band_geojson_dir, band_cells, label=label,
        native_gdal=native_gdal, runtime=runtime, max_workers=max_workers,
        data_dir=data_dir)

    # Stage 2b: clip legacy cells under reschemed cells of the same band
    # (see the Stage 2b section). Returns per-cell file overrides for
    # consolidation; empty when the band has no resolvable overlap.
    gdal = GdalRunner(native_gdal, runtime, data_dir)
    overrides = resolve_same_band_overlaps(
        band, label, band_geojson_dir,
        band_geojson_dir.with_name(f"band{band}.resolved"),
        band_merged_dir / ".same-band-overlaps.json", gdal, max_workers)

    # Stage 3: Consolidate. Soundings get a per-feature minzoom stamp
    # from the band's NATIVE minzoom (not the CLI-effective one) so
    # cached merged files stay valid across zoom-argument changes.
    layer_rules = layer_zoom_rules(band, BAND_ZOOM[band][0])
    merged = consolidate_geojson(band_geojson_dir, band_merged_dir,
                                 max_workers=max_workers,
                                 layer_rules=layer_rules,
                                 overrides=overrides)
    if not merged:
        print(f"WARNING: [{label}] nothing to render", file=sys.stderr)
        return None

    # Footprints: M_COVR per cell (single-cell exports drop the suffix);
    # a clipped legacy cell contributes its trimmed M_COVR.
    footprints = [overrides.get(f.name, f)
                  for f in sorted(band_geojson_dir.glob("M_COVR*.geojson"))]
    return RenderSource(
        priority=float(band),
        label=label,
        merged_dir=band_merged_dir,
        footprint_files=footprints,
        zoom_range=(effective_min, effective_max),
        layer_rules=layer_rules,
        native_zoom=BAND_ZOOM[band][:2])


# ---------------------------------------------------------------------------
# By-band orchestration (parallel across bands)
# ---------------------------------------------------------------------------

def process_by_band(
    inputs: List[Path],
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
    replacements: bool = True,
    catalog_path: Optional[Path] = None,
) -> Tuple[List[Path], Optional[int]]:
    """The by-band pipeline: stage inputs, drop cancelled cells and fetch
    their replacements, export per band, resolve same-band overlaps,
    consolidate, erase finer coverage, tile each render run, clip bands
    1-2 to the district region. Returns the tilesets for tile-join and
    the bundle's scale (bundle_scale; None when unknown)."""
    print("\n-- By-band mode ---------------------------------------------------")

    # Stage 1: stage all inputs
    staging_dir = data_dir / "enc" / "all"
    staging_dir.mkdir(parents=True, exist_ok=True)
    zips_dir = data_dir / "zips"
    for i, p in enumerate(inputs):
        dest = staging_dir / f"input{i}"
        if dest.exists() and list(dest.rglob("*.000")):
            print(f"Input {i} already staged, skipping")
        else:
            stage_input(p, dest, zips_dir)

    all_enc = find_enc_files(staging_dir)
    print(f"Total ENC files found: {len(all_enc)}")
    gdal_probe = GdalRunner(native_gdal, runtime, data_dir)
    all_enc = drop_cancelled_cells(all_enc, data_dir, gdal_probe, max_workers,
                                   data_dir / "cancelled-cells.json")

    # Stage 1b: cancelled cells whose reschemed successors NOAA files
    # under another district leave holes; fetch those successors.
    if replacements:
        cancelled = json.loads(
            (data_dir / "cancelled-cells.json").read_text())["cancelled"]
        all_enc = all_enc + fetch_replacement_cells(
            cancelled, all_enc, data_dir, gdal_probe, catalog_path,
            data_dir / "replacement-cells.json")

    # The bundle's `scale` metadata: most detailed compilation scale of
    # the live inventory (bundle_scale). Cached DSID reads, so free here.
    # (Named cscl: `scale` below is each band's display string.)
    cscl = bundle_scale(all_enc, data_dir, gdal_probe, max_workers)
    if cscl:
        print(f"Bundle scale (finest cell compilation scale): 1:{cscl:,}")

    by_band = group_by_band(all_enc)

    # Warn about non-NOAA files
    if 0 in by_band:
        print(f"WARNING: {len(by_band[0])} file(s) don't match NOAA naming, "
              f"will be skipped.", file=sys.stderr)

    # Print band inventory
    print("\nBand inventory:")
    for band in sorted(b for b in by_band if b > 0):
        zoom_min, zoom_max, desc, scale = BAND_ZOOM.get(
            band, (None, None, "unknown", "?"))
        skipped = ""
        if zoom_min is not None and zoom_min > maxzoom:
            skipped = "  <- starts above max zoom, skipping"
        render_max = (min(zoom_max + BAND_ZOOM_EXTENSION, maxzoom)
                      if zoom_max is not None else maxzoom)
        render_min = (max(zoom_min - BAND_ZOOM_DOWNWARD, minzoom)
                      if zoom_min is not None and zoom_min <= maxzoom else zoom_min)
        print(f"  Band {band} ({desc}, {scale}): {len(by_band[band])} file(s)"
              f"  z{zoom_min}-{zoom_max} (renders z{render_min}-z{render_max}){skipped}")

    # Build list of bands to process
    band_tasks = []
    for band in sorted(b for b in by_band if b > 0):
        if band not in BAND_ZOOM:
            continue
        zoom_min, zoom_max, desc, scale = BAND_ZOOM[band]
        # BAND_ZOOM_DOWNWARD zooms below the native floor, for bands that
        # render at all (a native floor above maxzoom stays skipped).
        effective_min = max(zoom_min - (BAND_ZOOM_DOWNWARD if zoom_min <= maxzoom else 0),
                            minzoom)
        # Each band renders BAND_ZOOM_EXTENSION levels past its native
        # ceiling (capped at the global maxzoom): wherever no finer band
        # exists, its tiles are the best available chart at deeper zooms.
        # Where a finer band DOES exist, Stage 3b erases that band's chart
        # footprints from the extended zooms before tiling, so the finer
        # chart is the only one in those tiles (tile-join merges
        # overlapping layers; it does not let a later input win). Band 4
        # (native z14) thus reaches z16, closing the no-harbour-chart
        # blanks along the whole charted coast. The cap exists because
        # extending without limit is intractable: overview bands cover
        # entire ocean basins, and rendering them to z16 multiplied
        # district builds 4-6x in size and blew the 6h CI timeout /
        # runner disk on Pacific districts (band 1 of 14CGD at z16 =
        # tiling the whole Pacific EEZ at harbour zoom).
        effective_max = min(zoom_max + BAND_ZOOM_EXTENSION, maxzoom)
        if effective_min > effective_max:
            continue
        band_tasks.append((band, by_band[band], effective_min, effective_max,
                           desc, scale))

    # Stages 2-3 for all bands — each band runs export→consolidate
    # sequentially, bands concurrently via threads. Tiling waits until
    # every band is consolidated because a band's extended zooms are
    # erased by the FINER band's footprints, which come out of its export.
    sources: List[RenderSource] = []

    def _collect(result: Optional[RenderSource]):
        if result is not None:
            sources.append(result)

    if len(band_tasks) <= 1 or max_workers <= 1:
        for band, files, emin, emax, desc, scale in band_tasks:
            _collect(prepare_band(
                band, files, data_dir, emin, emax, desc, scale,
                native_gdal, runtime, max_workers))
    else:
        with ThreadPoolExecutor(max_workers=min(len(band_tasks),
                                                max_workers)) as pool:
            futures = {}
            for band, files, emin, emax, desc, scale in band_tasks:
                f = pool.submit(prepare_band, band, files, data_dir,
                                emin, emax, desc, scale,
                                native_gdal, runtime, max_workers)
                futures[f] = band
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"ERROR: band {futures[future]}: {exc}",
                          file=sys.stderr)
                    sys.exit(1)
                _collect(future.result())

    if not sources:
        print("ERROR: No charts to render", file=sys.stderr)
        sys.exit(1)

    # Gap fills: consolidated like bands, prioritized between band 2 and
    # band 3. See prepare_gap_fill_sources() and the `gap_fills:` comment
    # block in enc-sources.yaml for the rationale (NOAA legacy-ENC
    # discontinuities being closed by the ENC Rescheming Project).
    sources.extend(prepare_gap_fill_sources(data_dir, minzoom, maxzoom))
    sources.sort(key=lambda s: s.priority)

    # Stage 3b: plan the tippecanoe runs and erase finer footprints from
    # every run that shares zooms with a higher-priority source.
    runs = plan_render_runs(sources)
    gdal = GdalRunner(native_gdal, runtime, data_dir)
    print("\n-- Render plan (finer chart wins on overlap) ----------------------")
    run_dirs: Dict[str, Path] = {}
    for run in runs:
        zr = f"z{run.zoom_range[0]}-{run.zoom_range[1]}"
        if run.erased_by:
            print(f"  {run.source.label:<28} {zr:<8} minus "
                  f"{', '.join(e.label for e in run.erased_by)}")
        else:
            print(f"  {run.source.label:<28} {zr}")
    for run in runs:
        if run.erased_by:
            run_dirs[run.stem] = erase_for_run(run, data_dir, gdal,
                                               max_workers)
        else:
            run_dirs[run.stem] = run.source.merged_dir

    # Stage 4: tippecanoe, runs concurrently via threads.
    tile_dir = data_dir / "tiles"
    tiles: List[Tuple[float, Path]] = []

    def _tile(run: RenderRun) -> Optional[Path]:
        """tippecanoe for one render run; drop_rate=1 means no point
        thinning, zoom presence is the per-feature stamp from
        layer_rules (see soundg_rule)."""
        return run_tippecanoe_for_source(
            run_dirs[run.stem], tile_dir, run.stem,
            run.zoom_range[0], run.zoom_range[1], max_workers=max_workers,
            drop_rate=1)

    if len(runs) <= 1 or max_workers <= 1:
        for run in runs:
            out = _tile(run)
            if out is not None:
                tiles.append((run.source.priority, out))
    else:
        with ThreadPoolExecutor(max_workers=min(len(runs),
                                                max_workers)) as pool:
            futures = {pool.submit(_tile, run): run for run in runs}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"ERROR: {futures[future].stem}: {exc}",
                          file=sys.stderr)
                    sys.exit(1)
                out = future.result()
                if out is not None:
                    tiles.append((futures[future].source.priority, out))

    if not tiles:
        print("ERROR: No tiles produced", file=sys.stderr)
        sys.exit(1)

    # Clip band 1/2 (and gap-fill) output to the district's regional
    # extent (union of its band>=3 footprints) — ocean-basin overview
    # cells otherwise put planet-wide tiles and bounds into every
    # district file.
    tiles = trim_low_bands_to_region(tiles)
    if not tiles:
        print("ERROR: No tiles left after district-region clipping",
              file=sys.stderr)
        sys.exit(1)

    # Coarse → fine for tile-join. The erase already made the tilesets
    # disjoint wherever they share a zoom, so the order is cosmetic.
    tiles.sort(key=lambda t: (t[0], t[1].name))
    return [p for _, p in tiles], cscl


# ---------------------------------------------------------------------------
# Per-source pipeline (standard modes)
# ---------------------------------------------------------------------------

def process_source(
    source: Source,
    data_dir: Path,
    idx: int,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
) -> Tuple[Optional[Path], Optional[int]]:
    """Plain (per-input) pipeline for one source: extract, export,
    consolidate, tile over the source's zoom range. Returns (tileset or
    None when the source has nothing to render, bundle scale or None for
    a GeoJSON-dir source with no cells to read it from)."""
    label = source.label or f"source{idx}"
    scale: Optional[int] = None
    safe_label = re.sub(r'[^\w\-.]', '_', label)

    enc_dir = data_dir / "enc" / safe_label
    geojson_dir = data_dir / "geojson" / safe_label
    merged_dir = data_dir / "merged" / safe_label
    tile_dir = data_dir / "tiles"

    enc_dir.mkdir(parents=True, exist_ok=True)
    geojson_dir.mkdir(parents=True, exist_ok=True)

    if source.geojson_dir:
        geojson_dir = source.geojson_dir
        count = len([f for f in geojson_dir.glob("*.geojson")
                     if f.stat().st_size > 100])
        print(f"\n[{label}] Using GeoJSON: {geojson_dir} ({count} layers)")
    else:
        print(f"\n[{label}] z{source.minzoom}-{source.maxzoom}  {source.path}")
        stage_input(source.path, enc_dir, data_dir / "zips")
        enc_files = find_enc_files(enc_dir)
        enc_files = drop_cancelled_cells(
            enc_files, data_dir, GdalRunner(native_gdal, runtime, data_dir),
            max_workers, data_dir / f"cancelled-cells-{safe_label}.json")
        if not enc_files:
            print(f"ERROR: No .000 files in {source.path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(enc_files)} ENC file(s)")
        scale = bundle_scale(enc_files, data_dir,
                             GdalRunner(native_gdal, runtime, data_dir),
                             max_workers)
        # Cells cancelled or removed since the last run must not linger.
        _remove_orphan_cells(geojson_dir, {e.stem.upper() for e in enc_files})
        export_to_geojson(
            enc_dir, geojson_dir, enc_files, label=label,
            native_gdal=native_gdal, runtime=runtime,
            max_workers=max_workers, data_dir=data_dir)

    # Stage 3: Consolidate
    consolidated = consolidate_geojson(geojson_dir, merged_dir,
                                       max_workers=max_workers)
    # Use merged dir if consolidation produced files, else raw geojson
    input_dir = merged_dir if consolidated else geojson_dir

    mbtiles = run_tippecanoe_for_source(
        input_dir, tile_dir, f"s{idx}",
        source.minzoom, source.maxzoom, max_workers=max_workers)
    return mbtiles, scale


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    data_dir = setup_data_dir()
    out_name = resolve_output_name(args)
    tiles_path = data_dir / "tiles" / out_name

    prod_path = None
    if args.output_dir:
        prod_dir = Path(args.output_dir).resolve()
        prod_dir.mkdir(parents=True, exist_ok=True)
        prod_path = prod_dir / out_name

    native_gdal, runtime = check_deps(
        need_gdal=not args.geojson_dir, need_pmtiles=args.pmtiles)
    if not native_gdal and runtime:
        pull_image(runtime, GDAL_IMAGE)

    if args.by_band:
        if args.geojson_dir:
            parser.error("--geojson-dir cannot be used with --by-band")
        if not args.inputs:
            parser.error("--by-band requires at least one input")

        input_paths = [Path(p).resolve() for p in args.inputs]
        for p in input_paths:
            if not p.exists():
                print(f"ERROR: {p} not found", file=sys.stderr)
                sys.exit(1)

        # process_by_band returns one .mbtiles per band, ordered coarse → fine
        tile_files, scale = process_by_band(
            input_paths, data_dir, args.minzoom, args.maxzoom,
            native_gdal, runtime, args.jobs,
            replacements=not args.no_replacements,
            catalog_path=Path(args.catalog).resolve() if args.catalog else None)

        if len(tile_files) == 1:
            shutil.copy2(tile_files[0], tiles_path)
            _patch_metadata(tiles_path, tiles_path.stem, scale=scale)
        else:
            merge_mbtiles(tile_files, tiles_path, tiles_path.stem, scale=scale)

        print(f"\nSummary (by-band):")
        print(f"  Inputs: {', '.join(p.name for p in input_paths)}")
        for path in tile_files:
            print(f"  {path.name} "
                  f"({path.stat().st_size / 1048576:.1f} MB)")

    else:
        sources = build_sources(args, parser)
        validate_sources(sources)

        # Pair each source's mbtiles with its minzoom for coarse→fine sort
        source_tiles: List[Tuple[int, Path]] = []
        scales: List[int] = []
        for i, source in enumerate(sources):
            result, src_scale = process_source(
                source, data_dir, i + 1,
                native_gdal, runtime, args.jobs)
            if result is not None:
                source_tiles.append((source.minzoom, result))
            if src_scale:
                scales.append(src_scale)
        scale = min(scales) if scales else None

        if not source_tiles:
            print("ERROR: No tiles produced", file=sys.stderr)
            sys.exit(1)

        source_tiles.sort(key=lambda x: x[0])
        tile_files = [p for _, p in source_tiles]

        if len(tile_files) == 1:
            shutil.copy2(tile_files[0], tiles_path)
            _patch_metadata(tiles_path, tiles_path.stem, scale=scale)
        else:
            merge_mbtiles(tile_files, tiles_path, tiles_path.stem, scale=scale)

        print(f"\nSummary:")
        for i, s in enumerate(sources):
            print(f"  Source {i+1}: {s.label}  z{s.minzoom}-{s.maxzoom}")

    size_mb = tiles_path.stat().st_size / 1048576
    print(f"  Tiles: {tiles_path} ({size_mb:.1f} MB)"
          + (f", scale 1:{scale:,}" if scale else ", scale unknown"))
    pmtiles_path = write_pmtiles(tiles_path) if args.pmtiles else None
    if prod_path:
        shutil.copy2(tiles_path, prod_path)
        print(f"  Copied: {prod_path}")
        if pmtiles_path:
            shutil.copy2(pmtiles_path, prod_path.with_suffix(".pmtiles"))
            print(f"  Copied: {prod_path.with_suffix('.pmtiles')}")
    print(f"\nAll data preserved in {data_dir.resolve()}/")


if __name__ == "__main__":
    main()
