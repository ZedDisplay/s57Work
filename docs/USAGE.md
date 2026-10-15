# s57-to-mbtiles.py — Detailed Usage Guide

Convert NOAA S-57 ENC charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

---

## Requirements

| Tool | Purpose | Install |
|------|---------|---------|
| **GDAL** (`ogr2ogr`, `ogrinfo`) | Converts S-57 layers to GeoJSON. Native install is used when found; otherwise the script runs the `ghcr.io/osgeo/gdal:alpine-small-latest` container via **podman** or **docker** | System package manager, or podman/docker |
| **tippecanoe** | Converts GeoJSON into vector `.mbtiles` tiles | Build from source or package manager |
| **tile-join** | Merges multiple `.mbtiles` into one (ships with tippecanoe) | Included with tippecanoe |
| **go-pmtiles** (`pmtiles`) | Optional: writes the `.pmtiles` copy of the final tileset when `--pmtiles` is given | `brew install pmtiles`, or a [release binary](https://github.com/protomaps/go-pmtiles/releases) |
| **Python 3** | Runs the script itself | Pre-installed on most systems |

No Python dependencies beyond the standard library. `pyyaml` is optional: without it the gap-fill config in `enc-sources.yaml` is skipped with a warning.

See [INSTALL.md](INSTALL.md) for platform-specific setup.

---

## Input: Where to Get ENC Files

Download NOAA ENC charts from:
- NOAA ENC Direct to GIS: individual chart ZIPs
- NOAA Office of Coast Survey: bulk region downloads

Each download is a ZIP containing one or more `.000` files. You can pass ZIPs directly to the script, or extract them and pass the directory.

### NOAA ENC Filename Convention

```
US{band}{region_code}{number}{edition}.000
```

- **Band** (digit 1–6): the chart's usage band / scale level
- **Region code** (2–3 letters): geographic area (e.g., `MA1`, `CT1`, `NY1`, `BOS`, `PVD`, `EC`)
- These are **not** always state abbreviations — `BOS` = Boston area, `PVD` = Providence area, `EC` = East Coast regional, `FAV` = Fall River/New Bedford area

Cells may ship with `.001`, `.002`, … update files alongside the `.000` base. The script copies those along with the base so GDAL applies them.

---

## Pipeline Overview

For each band or source, the script runs these stages in order:

```
1. Extract    ZIPs / copy directories        → data/enc/
2. Find       every .000 ENC file recursively; drop cancelled cells (DSID EDTN=0) → data/cancelled-cells.json
3. GDAL       ogr2ogr → one GeoJSON per S-57 layer per cell → data/geojson/
3b. Resolve   (by-band) clip legacy cells under reschemed cells of the same band → data/geojson/<band>.resolved/
4. Consolidate merge per-cell GeoJSON into one file per layer → data/merged/
5. tippecanoe one run per band/source over its full zoom range → data/tiles/
6. tile-join  merge all band/source .mbtiles into the final file
```

Every stage checks freshness and skips work that is already up to date, so re-runs after a partial failure or an added input only redo what changed. The GDAL export keys on each cell's own edition and update numbers; the later stages key on file modification times.

### Skipped S-57 Layers

These metadata-only layers are automatically skipped during GDAL export:
`DSID`, `C_AGGR`, `C_ASSO`, `Generic`

### Sounding Depths (SOUNDG)

The SOUNDG layer gets special handling. S-57 stores soundings as MultiPointZ geometry where the depth is the Z coordinate. The script uses two GDAL S-57 driver options to make depths usable:

- `SPLIT_MULTIPOINT=YES` — explodes each MultiPoint into individual Point features (one per sounding)
- `ADD_SOUNDG_DEPTH=YES` — adds a `DEPTH` property to each feature with the sounding value

This means depth readings show up as a `DEPTH` attribute in the vector tiles, which renderers can display as labels.

### Soundings at a Band's Bottom Zoom

In by-band mode every layer renders from the bottom zoom of its band's render range (or gap fill); tippecanoe's point thinning is switched off (`--drop-rate 1`) so nothing is silently dropped at a run's lower zooms. The one layer dense enough to need gating is `SOUNDG`: for band 3 and finer, and for gap fills, soundings render from the band's native bottom zoom (a gap fill's configured bottom zoom; never below z10), not from the two extra zooms a band renders below its floor, and at that bottom zoom only one in 2.5 of them is present, chosen by a hash of each sounding's identity so the choice is stable across runs and identical in the erased copies. Bands 1-2 carry soundings only at their top zoom (z8, z10). The rule lives in `soundg_rule` at the top of the script and is stamped per feature as the `tippecanoe.minzoom` extension during consolidation. Single-source mode has no band context: no stamps, and tippecanoe's default drop rate is its only thinning.

Before 2026-09-07 the sounding, aids-to-navigation and hazard layers were held back one zoom from a band's bottom, which together with the finer-wins erase left the published files with `SOUNDG` at even zooms only; `count-layer-by-zoom.py` measures a layer's presence per zoom over a bounding box.

---

## Modes of Operation

### 1. Single Source (simplest)

Process one ZIP or directory with a flat zoom range.

```bash
python3 s57-to-mbtiles.py MA_ENCs.zip
python3 s57-to-mbtiles.py MA_ENCs.zip -o ma-charts.mbtiles --minzoom 9 --maxzoom 16
python3 s57-to-mbtiles.py ./extracted_encs/
```

- Default zoom range: z9–z16
- Output filename defaults to `{input_stem}.mbtiles`

### 2. Two-Source Split

Merge two sources at different zoom ranges, split at a specific zoom level.

```bash
python3 s57-to-mbtiles.py region03.zip RI_detail.zip --split 12 -o ri-merged.mbtiles
```

- `--split 12` means: first input gets z9–z11, second input gets z12–z16
- Useful when you have a coarse regional chart and a detailed local chart

### 3. Multi-Source Explicit Ranges

Specify exact zoom ranges per source using the `--sources` flag.

```bash
python3 s57-to-mbtiles.py --sources overview.zip:7-9 regional.zip:10-12 detail.zip:13-16 -o merged.mbtiles
```

- Format: `path:minzoom-maxzoom`
- The script warns if zoom ranges overlap: tile-join merges same-layer features from both sources into the same tiles, it does not let the later source win

### 4. By-Band (recommended for multi-state regions)

**This is the mode you almost certainly want for combining multiple states.**

```bash
python3 s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ma-ny-layers.mbtiles
```

How it works:

1. Extracts all ZIPs / copies all directories into a staging area (`data/enc/all/input0`, `input1`, etc.)
2. Finds every `.000` file recursively and drops **cancelled cells**. NOAA withdraws a cell with an S-57 cancellation update (a DSID-only update setting the edition number to 0) and, as of September 2026, still ships the withdrawn cells in the district zips. GDAL applies the update but keeps the data, so without this step the pipeline renders charts that no longer exist on top of their replacements. Excluded cells are listed in `data/cancelled-cells.json`; the DSID reads are cached in `data/enc/.cell-editions.json`.
3. Groups files by NOAA usage band (the first digit after `US` in the filename)
4. Runs GDAL → consolidate → tippecanoe separately for each band, bands in parallel. Each band renders **two zoom levels past its native ceiling**, capped at `--maxzoom`, so wherever no finer chart exists the best available band still fills the deeper zooms, and **two zoom levels below its native floor**, capped at `--minzoom`, so where NOAA has no chart of the band native to a zoom (the band 2 gap off North Carolina left by the cancelled US2EC02M) the finer chart fills it instead of leaving it blank (`band_render_range`):

| Band | Type | Scale | Native zoom | Renders (defaults z9–z16) |
|------|------|-------|-------------|------------|
| 1 | Overview | ~1:3,500,000 | z7–z8 | z9–z10 |
| 2 | General | ~1:700,000 | z9–z10 | z9–z12 |
| 3 | Coastal | ~1:90,000 | z11–z12 | z9–z14 |
| 4 | Approach | ~1:22,000 | z13–z14 | z11–z16 |
| 5 | Harbour | ~1:8,000 | z15–z16 | z13–z16 |
| 6 | Berthing | ~1:3,000 | z17–z18 | needs `--maxzoom 17` or higher (then from z15) |

   **Finer chart wins where bands overlap.** tile-join does not do this for you: when two inputs have the same tile and layer it merges their features into one layer, so the pipeline enforces it before tiling. At every zoom where a band shares zooms with a finer band (band 2's z11–12 with band 3, band 3's z13–14 with band 4, band 4's z15–16 with band 5), the union of the finer band's chart footprints (`M_COVR`, `CATCOV=1`) is erased from the coarser band's features with `ogr2ogr -clipsrc`, into `data/merged/<band>.minus-<finer>/`. The zooms a band renders below its floor go through the same rule, ranked by band: the native band for the zoom erases them (band 3's z9–10 under band 2, band 4's z11–12 under band 3, band 5's z13–14 under band 4), and they in turn erase a coarser band's extension (band 1's z9–10 loses to band 3, band 2's z11–12 to band 4), because a fine chart drawn small only gets denser while a coarse chart drawn large is wrong by its generalisation. Those zooms are tiled from the erased copy; the band's native zooms are tiled from the untouched merged layers. A coarse chart therefore appears only where no finer chart has coverage, exactly as an ECDIS displays it, and the seams follow chart boundaries rather than tile edges. This step needs GDAL with SpatiaLite/GEOS spatial SQL (Ubuntu `gdal-bin`, Homebrew `gdal`).
   **Remaining same-band overlaps are detected before consolidation.** With cancelled cells gone, the only live-versus-live overlap in NOAA's current data is the legacy `US2EC03M` under the reschemed `US2ATL*` cells (see `docs/SAME-BAND-OVERLAP.md`). Where exactly one cell of an overlapping pair carries an Annex A band 1-2 region code, that cell wins and the other is clipped under its `M_COVR` footprint into `data/geojson/<band>.resolved/`; every other pair is reported as a warning and left alone. All pairs are recorded in `data/merged/<band>/.same-band-overlaps.json`.
5. Clips band 1/2 output to the district's region. Overview cells can span an entire ocean basin (`US1PO02M` covers the whole North Pacific), which would otherwise put planet-wide tiles and −180..180 bounds into every district file. The region is the union of the district's own band 3+ chart footprints as a z11 tile mask, dilated by one tile. The clipped result is written to a separate `bandN-*.region.mbtiles`; the band's tippecanoe output is never modified, so resume stays correct if a later run adds inputs that widen the region.
6. Builds any gap fills configured in `enc-sources.yaml` (see below).
7. Merges everything with tile-join. Gap fills go through the same erase rule with priority `cellband − 0.1`. At each zoom the band NOAA compiled for that zoom wins outright wherever it has coverage; elsewhere finer data wins. A fill therefore shows only in the holes, and a fill of band N cells is fully erased wherever band N itself renders, so nothing is tiled twice.

**Why by-band is better than per-state:** State boundaries are irrelevant to chart scale. A CT approach chart and a RI approach chart are the same band and belong at the same zoom level. By-band groups them correctly so no source overwrites another.

You can clip the zoom range with `--minzoom` and `--maxzoom`:

```bash
python3 s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip --by-band --minzoom 9 --maxzoom 16 -o region.mbtiles
```

This skips bands whose zoom range falls entirely outside the requested range, and clips bands that partially overlap.

Files that don't match the NOAA `US{digit}...` naming convention are reported as warnings and skipped.

#### Gap fills

NOAA's legacy ENC catalog has holes where a stretch of coast has band 3+ detail cells but no band 2 cell, leaving blanks at z9–z12. The `gap_fills:` section of `enc-sources.yaml` lists named groups of higher-band cells to render at a lower zoom range to cover those holes. Cells that are not part of the current build are skipped silently. Each group produces `data/tiles/gapfill-<name>.mbtiles`. The comment block in `enc-sources.yaml` explains the background and how to add a gap.

### 5. Skip GDAL (reuse existing GeoJSON)

If you already have GeoJSON files (from a previous run's `data/geojson/`, for example):

```bash
python3 s57-to-mbtiles.py --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12 -o band3.mbtiles
```

- Skips GDAL entirely — goes straight to consolidate and tippecanoe
- Cannot be combined with `--by-band`
- The GeoJSON directory must contain `.geojson` files; files under 100 bytes are ignored

In practice the freshness checks make this mode rarely necessary: re-running the same by-band command skips GDAL for every cell whose GeoJSON is already up to date.

---

## All Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `inputs` | Positional: ZIP file(s) or directory(ies) with .000 ENC files | — |
| `--by-band` | Auto-group by NOAA band and assign zoom ranges | off |
| `--sources FILE:MIN-MAX` | Explicit per-source zoom ranges | — |
| `--split ZOOM` | Zoom split point for two-input mode | — |
| `-o, --output` | Output `.mbtiles` filename | `{input_stem}.mbtiles` (`-merged` appended for multiple inputs) |
| `--output-dir` | Production directory to copy the final `.mbtiles` into. The file always stays in `data/tiles/` as well | no copy |
| `--minzoom` | Minimum zoom level | 9 |
| `--maxzoom` | Maximum zoom level | 16 |
| `--geojson-dir` | Use existing GeoJSON, skip GDAL | — |
| `-j, --jobs` | Parallel workers for GDAL export, consolidation, and bands | half the CPU count |
| `--catalog XML` | By-band: use this NOAA ENC product catalog instead of downloading `ENCProdCat_19115.xml` into `data/` (refreshed daily) | download |
| `--no-replacements` | By-band: do not fetch the reschemed cells that replace cancelled cells from NOAA's catalog | fetch |
| `--pmtiles` | Also write `<name>.pmtiles` beside the final `.mbtiles` (and into `--output-dir`): the same tiles, layers and metadata as a single-file PMTiles archive. Needs go-pmtiles' `pmtiles` on PATH | off |

---

## Data Directory

Everything the script produces lives in `./data/` under the current working directory, and **nothing is deleted between runs**. This is intentional — the GDAL export is the slowest step, and keeping its output lets a re-run skip straight to whatever actually changed.

Layout for `--by-band` mode:

```
data/
├── zips/                       # copy of every input ZIP
├── enc/
│   ├── all/input0/, input1/…   # staging: each input extracted here
│   └── band3/, band4/, …       # .000 (+ .001… updates) copied per band
├── geojson/
│   ├── band3/, band4/, …       # LAYER_CELL.geojson from ogr2ogr
│   └── band2.resolved/         # legacy cells clipped under reschemed cells (Stage 3b);
│                               # mask/<CELL>/clip.geojson = the erase polygon
├── merged/
│   ├── band3/, band4/, …       # one LAYER.geojson per band
│   ├── band3-coastal_z13-14.minus-band4-approach/
│   │                           # band 3 with band 4 footprints erased
│   │                           # (mask/clip.geojson = erase polygon)
│   └── gapfill-<name>/         # per gap-fill group
└── tiles/
    ├── band3-coastal.mbtiles   # native zooms (z11-12)
    ├── band3-coastal_z13-14.minus-band4-approach.mbtiles  # extended zooms
    ├── band2-general.region.mbtiles  # band 1/2 clipped copies fed to tile-join
    ├── gapfill-<name>*.mbtiles
    ├── <output name>.mbtiles   # final merged file
    └── <output name>.pmtiles   # with --pmtiles: the same tiles as a PMTiles archive
```

For standard (non-by-band) mode the per-source directories are named after the input file (`data/enc/NY_ENCs.zip/`, `data/geojson/NY_ENCs.zip/`, …) and the tippecanoe outputs are `data/tiles/s1.mbtiles`, `s2.mbtiles`, ….

To force a full rebuild, delete `data/` (or just `data/tiles/` to redo only the tippecanoe stage).

### What gets skipped on a re-run

- **Cancellation check and cell versions**: one `ogrinfo` DSID read per cell, cached in `data/enc/.cell-editions.json` by cell name and file time; only new or updated cells are re-read.
- **GDAL**: a cell is re-exported only if its version (S-57 edition and update numbers, read from DSID with updates applied) differs from the one recorded in its completion marker `data/geojson/<band>/.<CELL>.exported`, or its GeoJSON is gone. An export interrupted mid-cell leaves no marker and is redone. Per-layer GDAL failures are listed in `data/geojson/<band>/.export-errors.log` and summarised on stderr.
- **Same-band overlap resolution**: detection is cached on the cells' `M_COVR` exports and reruns only when one changes; a clipped legacy layer is redone only if its source file or the clip polygon is newer. Bands with no resolvable pair cost nothing.
- **Consolidate**: a merged layer is rebuilt only if any of its per-cell inputs (or its clipped replacement) is newer, or the heavy-layer minzoom config changed.
- **Erase**: the erase polygon is recomputed every run (cheap) but only rewritten when it changes; an erased layer is re-clipped only if its source layer or the erase polygon is newer.
- **tippecanoe**: a tileset's `.mbtiles` is reused if it is newer than every merged (or erased) layer and its stored zoom range matches the requested one.
- **Region clipping, gap-fill merging, tile-join**: cheap, always recomputed.

---

## Tippecanoe Behavior

- Runs **once per band or source** over its full zoom range (`-Z min -z max`), with every merged layer passed as a separate `-L` layer
- Uses `--no-tile-size-limit`, `--no-feature-limit`, `--no-simplification`, and `--no-tiny-polygon-reduction` (nautical charts need every feature at full fidelity), plus `--detect-shared-borders` and `--buffer=80`
- GeoJSON filenames like `DEPARE_US5MA1SK.geojson` get the layer name by removing the trailing cell name (so the tile layer is `DEPARE`, and `M_COVR_US5MA1SK.geojson` becomes `M_COVR`)

---

## Output

The final `.mbtiles` file is a standard MBTiles v1.3 vector tileset, written to `data/tiles/<name>.mbtiles` and copied to `--output-dir` if given. Metadata is patched to set `type=S-57`, `name`/`description` to the output stem, and `scale` to the most detailed compilation scale among the bundle's cells (DSPM CSCL, e.g. `22000` for 1:22,000; the finest band's nominal scale if none is readable). Signal K chart plugins read that row and default to 250000 without it; Freeboard-SK uses it to stack charts most-detailed-on-top. In by-band mode the declared bounds reflect the district region, not the full extent of its overview cells.

### PMTiles copy (`--pmtiles`)

With `--pmtiles` the final tileset is also written as `data/tiles/<name>.pmtiles` (and copied to `--output-dir`). It is produced from the finished, metadata-patched `.mbtiles` by go-pmtiles' `pmtiles convert`, so it is a faithful mirror: the gzipped MVT tile blobs are copied unchanged, deduplicated and clustered, and the metadata table becomes the archive's JSON metadata (`type=S-57`, `name`, `description`, `scale`, `minzoom`/`maxzoom`, `vector_layers`), except `bounds` and `center`, which go into the PMTiles header, whose zoom range is derived from the tiles themselves. Decoding both files tile by tile gives identical features. The archive is usually smaller than the `.mbtiles` because the erase stage leaves many identical near-empty coverage tiles, which PMTiles stores once. The `.mbtiles` stays the master copy — the resume logic and the diagnostic scripts read it, and the archive is regenerated every run.

tile-join can emit PMTiles directly, but it resets the metadata `type` to `overlay` and an archive's JSON metadata cannot be patched in place, which is why the conversion runs last instead.

A PMTiles file is served by [signalk-pmtiles-plugin](https://github.com/panaaj/signalk-pmtiles-plugin) or any host that supports HTTP range requests (S3/R2, plain nginx); the [pmtiles](https://github.com/protomaps/PMTiles) JavaScript library reads it in MapLibre and OpenLayers. Note that the Signal K PMTiles plugin currently types every archive as `tilelayer` rather than reading the `S-57` stamp.

### Using with SignalK

Copy or symlink the output to your SignalK charts directory:

```bash
cp ct-ri-ma-ny-layers.mbtiles ~/.signalk/charts-simple/
```

Or use the SignalK charts plugin upload feature.

The chart should appear in Freeboard-SK as a vector overlay.

---

## Long-Running Jobs

The GDAL export and tippecanoe steps can take a long time, especially for multi-state by-band runs. Run with `nohup` and a log file:

```bash
nohup python3 s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip \
  --by-band -o ct-ri-ma-ny-layers.mbtiles \
  --output-dir ~/.signalk/charts-simple/ \
  > ~/s57-rebuild.log 2>&1 &
```

Monitor with:

```bash
tail -f ~/s57-rebuild.log
```

If a run dies part-way, re-run the same command: the freshness checks pick up where it left off.

---

## Common S-57 Layers in Output Tiles

| Layer | Description |
|-------|-------------|
| SOUNDG | Depth soundings (individual points with `DEPTH` property in meters) |
| DEPARE | Depth areas (polygons with `DRVAL1`/`DRVAL2` depth range) |
| DEPCNT | Depth contour lines |
| LNDARE | Land areas |
| COALNE | Coastline |
| BOYCAR | Cardinal buoys |
| BOYLAT | Lateral buoys |
| BCNSPP | Special purpose beacons |
| LIGHTS | Lights |
| NAVLNE | Navigation lines |
| OBSTRN | Obstructions |
| WRECKS | Wrecks |
| BRIDGE | Bridges |
| RESARE | Restricted areas |
| ACHARE | Anchorage areas |
| TSSLPT | Traffic separation scheme lanes |
| RIVERS | Rivers |
| LAKARE | Lake areas |
| SLCONS | Shoreline constructions (piers, wharves) |

Many more layers exist depending on chart content. The script exports all layers except the four skipped metadata layers.

---

## Example: Full Four-State Build

```bash
# Download CT, RI, MA, NY ENC ZIPs from NOAA

# Run the conversion (by-band mode, recommended)
nohup python3 s57-to-mbtiles.py \
  CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip \
  --by-band \
  -o ct-ri-ma-ny-layers.mbtiles \
  --output-dir ~/.signalk/charts-simple/ \
  > ~/s57-build.log 2>&1 &

# Watch progress
tail -f ~/s57-build.log

# When done, restart SignalK or refresh Freeboard-SK to see the new chart
```
