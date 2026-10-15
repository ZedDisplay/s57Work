# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python tool (`s57-to-mbtiles.py`) that converts NOAA S-57 ENC nautical charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

Tests: `python3 -m unittest discover -s tests -t .` (standard library only, no pytest). Unit tests cover naming, zoom rules, the render plan, freshness, consolidation, geometry, catalog parsing and the MVT counter; `tests/test_pipeline.py` runs the whole by-band pipeline on `tests/fixtures/mini-district.zip` (a band 4 cell, three nested band 5 cells, one cancelled cell) and decodes the tiles per zoom — it skips when GDAL or tippecanoe is missing. `.github/workflows/tests.yml` runs both on every push and PR. Rebuild the fixture with `tests/fixtures/make-fixture.py`. No linting is configured.

## External Dependencies

- **GDAL** (`ogr2ogr`, `ogrinfo`) — native install or via container (`ghcr.io/osgeo/gdal:alpine-small-latest`) using podman/docker. By-band mode also needs the SQLite dialect's spatial functions (`ST_Union`, `ST_Difference`), i.e. a GDAL built with SpatiaLite or GEOS; Ubuntu's `gdal-bin` and Homebrew's `gdal` both qualify (verified Sept 2026)
- **tippecanoe + tile-join** — converts GeoJSON to vector `.mbtiles` tiles (native install)
- **go-pmtiles** (`pmtiles`) — optional; `--pmtiles` converts the finished `.mbtiles` into a sibling `.pmtiles` archive (`write_pmtiles`). Runs after `_patch_metadata` so `type=S-57` and `vector_layers` carry over; tile-join's own PMTiles output would reset the type to `overlay`. The `.mbtiles` stays the master for resume and the diagnostics. Pinned to 1.31.2 in both workflows
- **Python 3** — standard library only for the chart tool. The mesh build (`build-mesh.py`) needs `mesh/requirements.txt` (numpy, scipy, shapely 2.1, mapbox-vector-tile, pyshp, triangle) and `tar --zstd`; only the mesh workflow and the mesh tests install them

## Running

```bash
# Single source
./s57-to-mbtiles.py NY_ENCs.zip

# By-band mode (recommended for multi-state)
./s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip --by-band -o merged.mbtiles

# Reuse existing GeoJSON (skip slow GDAL step)
./s57-to-mbtiles.py --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12
```

## Pipeline Architecture

Single-file pipeline with five stages per band/source:

1. **Extract** — unzip inputs into `data/enc/`
2. **Find** — discover `.000` ENC files (group by NOAA band in `--by-band` mode), then **drop cancelled cells**: a cell whose DSID edition number is 0 after updates has been withdrawn by NOAA (S-57 App. B.1 §5.7 cancellation update) but still ships in the district zips; 190 such cells across the eight active districts on 2026-09-05. Excluded cells are listed in `data/cancelled-cells.json`; stale exports of them are removed
2b. **Replacement cells** (by-band only) — NOAA files each reschemed cell under one district, so cancelling a legacy cell that spanned two districts can strip a district zip of coverage of its own waters (US2EC04M → US2ATLPC, filed under district 5, left 01CGD with no band 2 west of 72°W on 2026-09-05). `fetch_replacement_cells` reads NOAA's ENC product catalog (`data/ENCProdCat_19115.xml`, refreshed daily, `--catalog` to override) and downloads the live reschemed same-band cells that overlap both a cancelled cell and the input's band ≥3 extents; they join the inventory like any other cell. Record in `data/replacement-cells.json`; `--no-replacements` disables it. Legacy-named cells are never fetched
3. **GDAL export** — `ogr2ogr` (native or container) converts each ENC layer to GeoJSON in `data/geojson/`. SOUNDG layer gets special handling (`SPLIT_MULTIPOINT=YES`, `ADD_SOUNDG_DEPTH=YES`) to produce individual depth points with a `DEPTH` property
3b. **Same-band overlap resolution** (by-band only) — detects live cells of one band whose `M_COVR` coverage overlaps and records every pair in `data/merged/<band>/.same-band-overlaps.json`. After cancelled cells are dropped the only known case is legacy `US2EC03M` under reschemed `US2ATL*` cells; a pair is resolved (reschemed wins, legacy clipped into `data/geojson/<band>.resolved/`) only when exactly one side carries an Annex A band 1-2 region code, everything else is a warning. See `docs/SAME-BAND-OVERLAP.md`
4. **Consolidate** — merge per-chart GeoJSON into one file per layer in `data/merged/`
5. **Erase** (by-band only) — for zooms where a band overlaps a finer band, `ogr2ogr -clipsrc` removes the finer band's chart footprints (`M_COVR`, `CATCOV=1`) from its features, into `data/merged/<band>.minus-<finer>/`
6. **tippecanoe** — builds one `.mbtiles` per band and zoom group in `data/tiles/` with `--no-tile-size-limit --no-feature-limit`
7. **tile-join** — merges all tilesets into one final `.mbtiles`. tile-join does **not** let a later input win: overlapping layers are merged feature-by-feature, which is why the erase stage exists
8. **PMTiles** (`--pmtiles`, on in CI) — `pmtiles convert` writes `<name>.pmtiles` beside the final file; tiles are copied byte-for-byte and deduplicated. `tests/test_pmtiles.py` and the pipeline test decode both files and require identical tiles and metadata

All artifacts stored in `./data/` and preserved between runs for resume capability.

## Key Design Decisions

- **By-band mode** groups charts by NOAA usage band (1-6), not by state. Each band renders `BAND_ZOOM_DOWNWARD` (=2) zoom levels below its native floor (capped at the global minzoom; a band whose floor is above the global maxzoom, band 6, is still skipped) and `BAND_ZOOM_EXTENSION` (=2) zoom levels past its native ceiling, capped at the global maxzoom. The downward zooms rank by their band like the extensions (`priority_at`): the native band for the zoom still wins, but in a hole the finer chart beats a coarser band's extension (a fine chart drawn small only gets denser; band 1 at z10 is a 1:3.5M coastline). They close the holes where NOAA has a finer band but not the one native to a zoom (Cape Fear to New Bern at z9-10 after US2EC02M was cancelled; 178 z14 tiles with band 5 but no band 3/4, hole scan of 2026-10-15). Soundings still start at the native floor. Upward, band 4 (native z13-14) reaches z16, filling deep zooms along the whole charted coast, while ocean-basin overview bands stop early (rendering band 1 to z16 blew CI time and disk on Pacific districts). **Finer chart wins on overlap, and the pipeline has to enforce that itself**: tile-join merges same-layer features from overlapping inputs rather than replacing them, so at every zoom where a band shares zooms with a higher-priority source (the next band, or a gap fill of finer cells), the union of those sources' `M_COVR` footprints is erased from its features before tippecanoe (`plan_render_runs`, `erase_for_run`). The erased copy is tiled separately from the band's native-zoom run, so each band yields up to two tilesets. Gap fills carry priority cellband − 0.1 and go through the same rule; at each zoom the band NOAA compiled for that zoom outranks everything (`priority_at`), so fills and extensions show only where the native chart has no coverage. Native ranges are in the `BAND_ZOOM` dict at the top of the file. Band 1/2 output is then clipped to the district's regional extent — the union of the district's own band ≥3 footprints as a z11 tile mask (`trim_low_bands_to_region`) — because ocean-basin overview cells (e.g. US1PO02M, the whole North Pacific) otherwise put planet-wide tiles and −180..180 bounds into every district file. The clip never touches the cached tippecanoe output: it writes a derived `*.region.mbtiles` copy that is regenerated every run and is what tile-join consumes, so resume stays correct when a later run's band ≥3 inputs widen the region.
- **Resume-friendly**: tippecanoe skips zoom levels where a non-empty `.mbtiles` already exists; GDAL export skips a cell only when its completion marker (`data/geojson/<band>/.<CELL>.exported`, written after the cell's last layer) holds the cell's current version, `EDTN.UPDN` from DSID with updates applied, and its GeoJSON still exists. File times play no part. Without the marker an export interrupted mid-cell left partial layer sets that resumed as complete (Sept 2026). GDAL failures per layer go to `data/geojson/<band>/.export-errors.log` and are summarised on stderr. Gap-fill groups in `enc-sources.yaml` that name a cancelled cell follow it to its successors from `data/replacement-cells.json`.
- **Zoom presence of point layers**: every layer renders from its source's bottom zoom, and by-band tippecanoe runs use `--drop-rate 1` so tippecanoe never thins points. Only `SOUNDG` is gated, per feature via the `tippecanoe.minzoom` stamp (`soundg_rule`, `feature_minzoom`): bands 1-2 at their top zoom only (z8, z10); band 3 and finer, and gap fills, from their bottom zoom (never below z10) with one in 2.5 present at that zoom, picked by a hash of LNAM + position so the erased copy keeps the same pick. The old +1 offset for aids/hazards/soundings plus the erase left the published files with `SOUNDG` at even zooms only (fixed 2026-09-07); measure every zoom with `count-layer-by-zoom.py` after touching this.
- **`scale` metadata**: every output carries a `scale` row (and the release JSON a `scale` key): the most detailed compilation scale among the live cells, DSPM CSCL read from the DSID layer alongside the cancellation fields (`bundle_scale`), falling back to the finest band's nominal scale. A bundle has no single true scale; this is the value Signal K chart plugins read (they default to 250000 without it) and Freeboard-SK uses it only to stack charts most-detailed-on-top.
- **Skipped layers**: `DSID`, `C_AGGR`, `C_ASSO`, `Generic` are metadata-only and excluded from GDAL export. `DSID` is still read per cell (`read_cell_dsid`) for the cancellation check; it is the only record that says whether a cell is alive.
- **Layer naming**: GeoJSON filenames like `DEPARE_US5MA1SK.geojson` — the tippecanoe layer name is the stem with a trailing NOAA cell name removed (`layer_name_from_stem`). Layer names can contain underscores (`M_COVR`, `M_QUAL`, `TS_FEB`), so never split on the first one.

## Navigation mesh (`build-mesh.py`, `mesh/`)

The District 1 mesh experiment of Oct 2026 made district-agnostic, stage for stage (see `docs/MESH.md`; the user's standing instruction is to reproduce it exactly and ask before changing anything). Four stages with the experiment's intermediate files under `data/mesh/<name>/`: decode the chart's z16 tiles to per-layer pickles (`mesh/decode.py`), derive the extent (`mesh/extent.py`: the rectangle around every 0.25° cell holding a z16 tile, cells counted from −180°+0.000125°/−90°+0.000125° so no seam lies on the sounding grid's 0.00025° lattice, which on 01CGD kept 14,954 label-differing seam segments instead of 26 and made the refinement 5.7× slower; split into longitude clusters at gaps over 4°; one `xScale` = cos(mid-latitude) per cluster; `--box` overrides for diagnostics), the tile build + seam weld + global Triangle q20 refinement + checks (`mesh/build.py`, the experiment's `build_mesh_full.py` with globals replaced by `configure()`; the CLI runs the tile stage and the global stage as two freshly spawned interpreters with `build/tiles.json` as the handoff, because the refinement is the memory peak and must not inherit the tile stage's heap: a 16 GB runner killed 01CGD there on 2026-10-09; outputs are byte-identical to the one-process `run()`), finalize (`mesh/finalize.py`) and the `WRPMESH1` binary tiles + `index.json` version 1 (`mesh/binary.py`) that the signalk-weather-router-plus plugin reads from its `meshDir`. Output: `<name>_mesh/`, `<name>_mesh.json` sidecar (provenance, extent, counts, every requirement PASS/FAIL), `--archive` → `<name>_mesh.tar.zst`. Exit 2 when a gating requirement fails (all but "2 layers loaded" and "7 angles"; a z16 tile that fails to decode also gates), intermediates are kept on failure, so CI never publishes a failed mesh. The old grid's lattice origin (−75.5, 38.7, 0.00025°) stays verbatim. The seam weld is followed by `split_segments_through_vertices`: a weld that puts a segment through another vertex (a T-junction) makes Triangle's refinement non-terminating (01CGD New York Harbor, 2026-10-10, killed on 16 and 32 GB runners); a per-tile refinement scan plus a merged-block refinement is the diagnostic for a global refinement that will not finish. Tests: `tests/test_mesh_extent.py` (stdlib) and `tests/test_mesh_pipeline.py` (fixture chart → mesh, skips without GDAL/tippecanoe/mesh packages; land fixture in `tests/fixtures/land/`).

## CI / GitHub Actions

- `enc-sources.yaml` — defines all available builds (CG districts and individual states) with an `active` list controlling which run; the `mesh:` section (`runner`, `workers`, `active`) drives the mesh workflow. 17cgd is not in `mesh.active`: its cells straddle the antimeridian
- `.github/workflows/build-mesh.yml` — runs after every "Build ENC Charts" run on main (and on demand with the same `publish` test/release input). Rebuilds a district's mesh when the published chart's `build_date` is newer than the mesh sidecar's `chart_build_date` (`force` overrides; test runs always build). Reads the chart from R2, publishes `<D>_mesh.tar.zst` + `<D>_mesh.json` to the release and R2 `charts/`, syncs the unpacked folder to `charts/mesh/<D>/`, rewrites `charts/mesh/index.json`, and the release notes via `release-notes.sh` (shared with the chart workflow)
- `.github/workflows/build-charts.yml` — downloads ENC ZIPs from NOAA, runs the pipeline, uploads `.mbtiles` as GitHub Release assets
- Manual trigger supports overriding the active build list. **Test pathway**: the `publish` input defaults to `test`, which builds everything but leaves the `latest` release and R2 `charts/` untouched; outputs go to run artifacts (7 days) and R2 `charts-test/<branch>/` (job `test-stage`), and `verify-r2-charts.py --prefix charts-test/<branch>` reads them. `release` publishes and is refused off the default branch. Scheduled runs always publish

## Repo Structure

```
s57-to-mbtiles.py          # the tool
build-mesh.py               # navigation mesh of a finished chart file (mesh/ package, docs/MESH.md)
mesh/                       # decode.py extent.py build.py finalize.py binary.py cli.py requirements.txt
release-notes.sh            # 'latest' release notes from the R2 listing; used by both workflows
check-tile-overlap.py       # diagnostic: shared tile addresses between tilesets, per-tile layer counts
check-tile-duplicates.py    # diagnostic: chart cells stacked and exact duplicate features per tile in a merged file
count-layer-by-zoom.py      # diagnostic: one layer's feature count per zoom over a bbox (compare builds)
verify-r2-charts.py         # downloads each published district file, measures coverage gaps and stacking, writes a report
tests/                      # unittest suite; tests/fixtures/mini-district.zip is the five-cell end-to-end fixture
pyproject.toml              # project metadata; the version lives here and nowhere else
enc-sources.yaml            # CI build definitions
docs/
  INSTALL.md                # platform-specific setup (macOS, Raspberry Pi)
  USAGE.md                  # detailed usage guide, all CLI options
  SOUNDG-FIX.md             # depth sounding bug fix writeup
data/                       # gitignored working directory
  zips/  enc/  geojson/  merged/  tiles/
```

## Documentation

- `docs/INSTALL.md` — install guide for tippecanoe, GDAL, podman/docker (Raspberry Pi and macOS)
- `docs/USAGE.md` — detailed usage guide covering all five modes of operation and CLI options
- `docs/SOUNDG-FIX.md` — documents the SOUNDG depth sounding fix (both the tile generation bug and the Freeboard-SK rendering bug)
- `docs/MESH.md` — the navigation mesh: inputs, stages and files, extent rule, binary format, sidecar, gating, the mesh workflow, and the agreed list of differences from the experiment
- `docs/SAME-BAND-OVERLAP.md` — NOAA ships legacy and reschemed cells of the same band with overlapping data (an S-57 App. B.1 §2.2 violation); evidence, standards, NOAA's stated intent, how OpenCPN and others cope, and the Stage 2b mechanism (reschemed wins, bands 1-2)
