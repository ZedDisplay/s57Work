# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `mesh-catalog.py` writes and validates the mesh catalogue
  `charts/mesh/index.json` that the router plugin downloads meshes from.
  Every entry now carries `archive_url`, the absolute HTTPS URL of its
  archive, beside the relative `archive` kept for older plugin versions;
  every required field is checked (name charset, `.tar.zst`, ISO dates,
  non-negative `bytes` and `triangles`, non-empty `clusters` with finite
  boxes) and every `archive_url` is HEADed for 200 and a plausible
  `Content-Length` before the catalogue is published. A bad entry fails
  the step instead of publishing a row the plugin would refuse. The
  catalogue version stays 1: `archive_url` is additive and a version 1
  reader without it keeps working. Tests in `tests/test_mesh_catalog.py`.
- CI test pathway: `workflow_dispatch` gained a `publish` input that
  defaults to `test`. A test run builds normally but the `release` job is
  skipped; a `test-stage` job mirrors the outputs to R2
  `charts-test/<branch>/` and lists them in the run summary, so new code
  can be built end to end without overwriting the published charts.
  `release` publishes as before and is refused from any branch but the
  default one; scheduled runs still publish. `verify-r2-charts.py
  --prefix charts-test/<branch>` measures a staged test build.
- `scale` metadata row in every output (and `scale` in the release JSON):
  the most detailed compilation scale among the bundle's live cells, DSPM
  CSCL read from the DSID layer the cancellation check already reads
  (`parse_dsid`, `bundle_scale`), with the finest band's nominal scale as
  fallback. Signal K chart plugins read this row and defaulted to 250000
  for every district file, so Freeboard-SK stacked them all as equals.
- `--pmtiles`: also write the final tileset as a PMTiles archive beside
  the `.mbtiles`, via go-pmtiles' `pmtiles convert` run after the
  metadata patch so `type=S-57`, `name`, bounds and `vector_layers`
  carry over (tile-join's own PMTiles output resets the type to
  `overlay`). Tiles are copied byte-for-byte and deduplicated; on a
  district file the archive is smaller than the SQLite original. The
  chart build workflow installs go-pmtiles 1.31.2, passes the flag, and
  publishes `<district>.pmtiles` next to `<district>.mbtiles` in the
  release and on R2; the per-chart `.json` gains `pmtiles_file_size_mb`.
  Tests: `tests/test_pmtiles.py` (dependency check, conversion of a
  tippecanoe-built tileset, failure leaves no partial file) and the
  pipeline test decode `.mbtiles` and `.pmtiles` and require identical
  tiles and metadata; both skip without the tool.
- Test suite (`tests/`, standard-library `unittest`): 82 unit tests over
  cell/layer naming, the SOUNDG zoom rule and hash thinning, the
  finer-wins render plan, resume freshness, consolidation and overrides,
  clip-debris removal, tile arithmetic and the region mask, catalog
  parsing, same-band overlap resolution, gap-fill config and the MVT
  reader in `count-layer-by-zoom.py`; plus an end-to-end test that runs
  the by-band pipeline on `tests/fixtures/mini-district.zip` (US4NY1BY
  with US5RI1AC/AD/AE nested inside it and the cancelled US5NJ30M) and
  decodes the tiles: cancelled cell dropped, SOUNDG and every point
  layer present at z13/14/15/16 with the bottom zoom thinned, one chart
  per tile inside band 5, band 4 extension outside it, second run
  rebuilds nothing. `.github/workflows/tests.yml` runs it on every push.

### Fixed
- `count-layer-by-zoom.py`: a bbox reaching a pole (S=-90) raised a math
  domain error; latitude is clamped to the Web Mercator limit first.
  zlib-wrapped tiles were only recognised with the default 32 KB window
  header; the full two-byte header is now checked.
- CI workflow declares `contents: read` at the top level; only the
  release job keeps `contents: write`.
- A cell's export completion marker was written even when one of its
  layers failed in `ogr2ogr`, so the next run took the cell as fresh and
  the missing layer stayed a hole until NOAA changed the cell. Both the
  native and the container export now leave a failed cell unmarked, so it
  is re-exported next run; the container path also logs an `ogrinfo`
  failure instead of silently exporting zero layers.
- `count-layer-by-zoom.py` streams tiles from SQLite instead of loading
  every blob of a zoom into memory (a district-wide bbox at z14 over a
  1.9 GB file now peaks at 26 MB resident), and reports the layer's own
  encoded bytes alongside the stored bytes of the tiles in the bbox.
- The tippecanoe freshness check reused an existing per-run `.mbtiles`
  whenever it was newer than its inputs and had the same zoom range,
  ignoring the drop rate it was built with; a plain-mode file (tippecanoe's
  default 2.5) and a by-band file (`--drop-rate 1`) for the same stem could
  stand in for each other. Each run now stamps `drop_rate` into the
  mbtiles metadata and a file is fresh only when it matches. Files built
  before this carry no stamp and are rebuilt once.
- `count-layer-by-zoom.py`: a bbox crossing the antimeridian (W > E)
  queried an empty column range; it now reads both ranges. Sizes under
  1 KB printed as `0KB`; they now print in bytes.
- Published district files carried `SOUNDG` only at even zooms: none at
  z11 and z13, a thin remainder at z15 (01CGD Narragansett: 0 / 95 / 0 /
  3,067 / 738 / 30,757 soundings at z11-16). Lights, buoys, beacons,
  wrecks, obstructions and rocks vanished at the same zooms. Cause: the
  +1 zoom offset those layers got in 0.6.0 (`LAYER_MIN_ZOOM_OFFSET`)
  started each band's copy one zoom above the band's bottom, and the
  finer-wins erase (Stage 3b) removed the coarser band's copy under it,
  so at z13 neither band 3 nor band 4 had soundings; tippecanoe's default
  point drop rate (2.5 per zoom below a run's top zoom) also thinned every
  point layer at the bottom zoom of every run. The offset is gone, and
  by-band tippecanoe runs pass `--drop-rate 1`. Zoom presence is now
  decided per feature (`ZoomRule`, `soundg_rule`, `feature_minzoom`):
  only `SOUNDG` is gated — bands 1-2 keep soundings at their top zoom
  only (z8, z10); band 3 and finer, and gap fills, carry soundings from
  their bottom zoom (never below z10) with one in 2.5 present at that
  zoom, picked by a hash of LNAM and position so the pick is stable and
  the erased copy of a layer keeps it. A z13 tile carries about 1.6x the
  soundings of a z14 tile instead of four times as many. Every merged
  layer and tileset is rebuilt on the next run (stamp marker). New
  `count-layer-by-zoom.py` counts one layer's features per zoom over a
  bounding box, for before/after comparisons.
- Dropping cancelled cells emptied z9-10 over New York Harbor, Long
  Island Sound and the Connecticut coast in the 2026-09-05 01CGD build.
  NOAA cancelled US2EC04M (2026-07-24) and files its reschemed successor
  west of 72°W, US2ATLPC, under district 5, so it is not in
  01CGD_ENCs.zip; the live legacy US2EC03M declares no data there. New
  Stage 1b (`fetch_replacement_cells`) reads NOAA's ENC product catalog
  and, for every cancelled cell, downloads the live reschemed same-band
  cells that overlap both it and the input's band >= 3 extents, then runs
  them through the normal pipeline. Verified by decoding tiles: New York
  Harbor z9 went from 29 features and 1 DEPARE (published) to 218 and 7
  (Sept 3 build: 228 and 8); Long Island Sound z9 from a 190-byte
  CATCOV=2-only tile to 111 features and 3 DEPARE. `--catalog XML` uses a
  local catalog, `--no-replacements` disables the stage; fetched cells are
  listed in `data/replacement-cells.json`.
- A GDAL export interrupted mid-cell (killed run, lost runner) left a
  partial layer set that the resume logic accepted as complete, because
  `cell_outputs_fresh` compared file times only. Each cell now gets a
  completion marker (`data/geojson/<band>/.<CELL>.exported`) written after
  its last layer, and a cell without one is re-exported. Found when a
  resumed local build shipped 39 cells with most layers missing (SOUNDG
  included) at z13-16.
- Export freshness is now keyed on the cell's own version. Each cell's
  completion marker holds its S-57 edition and update numbers
  (`EDTN.UPDN`, e.g. `62.0`, read from DSID with updates applied); a cell
  is re-exported only when those differ from the marker or its outputs
  are gone. File times no longer decide anything: unzip, copy and
  re-download all rewrite them, and a re-downloaded but unchanged zip
  used to re-export every cell. The DSID cache (`data/enc/
  .cell-editions.json`) is now keyed by cell name so the per-band copies
  hit it.
- GDAL export failures are reported. `ogr2ogr`/`ogrinfo` stderr used to
  be discarded, so a layer that failed to export was a silent hole in
  the chart. Failures are appended to `data/geojson/<band>/
  .export-errors.log` (native and container paths) and the run prints
  the count and the first few.
- CI pins tippecanoe to 2.79.0 (`TIPPECANOE_VERSION` in
  `build-charts.yml`) instead of building master on every run.
- Gap-fill groups silently rendered nothing when a configured cell had
  been cancelled: `east_maine_offshore_band3` pointed at US3EC11M
  (cancelled), which blanked the Gulf of Maine at z15-16 (121k z16 tiles
  present in the Sept 3 build, none in the Sept 5 one). A cancelled cell
  in a group now resolves to its live same-band successors, recorded by
  Stage 1b in `replacement-cells.json` under `successors`, and the
  substitution is printed.
- By-band mode shipped two charts in the same tiles at every zoom from 9
  to 16. The zoom extension and gap fills both relied on tile-join letting
  a later input win where tilesets overlap; tile-join instead merges
  same-layer features from all inputs into one layer, with a draw order
  that is not controllable and differs from tile to tile. Opaque depth
  areas from the coarser band therefore showed through in tile-shaped
  patches next to tiles showing the finer band (reported around Block
  Island, RI). The pipeline now enforces "finer chart wins" itself: before
  a band is tiled at zooms shared with a higher-priority source, the union
  of that source's chart footprints (`M_COVR`, `CATCOV=1`) is erased from
  its features with `ogr2ogr -clipsrc`, so a coarse chart is present only
  where no finer chart has coverage, as on an ECDIS. Each band now yields
  a native-zoom tileset and, where it overlaps a finer band, an erased
  extended-zoom tileset (`<band>_z<a>-<b>.minus-<finer>.mbtiles`).
- Cancelled cells were being built. NOAA withdraws a cell with an S-57
  cancellation update (App. B.1 §5.7: a DSID-only update file with edition
  number 0) and still ships the withdrawn cells, cancellation included, in
  its district zips: 190 cells across the eight active districts on
  2026-09-05. GDAL applies the update and reports edition 0 but keeps the
  features, so the pipeline rendered withdrawn legacy charts under and over
  their reschemed replacements. This was the cause of 444 of the 448
  same-band overlaps measured in the published files. Cells with edition 0
  after updates are now dropped at inventory (`drop_cancelled_cells`),
  listed in `data/cancelled-cells.json`, and their stale exports removed.
- Same-band duplicates: NOAA's rescheming ships legacy and reschemed cells
  of one usage band that overlap with `M_COVR CATCOV=1` coverage in both and
  carry the same objects (an IHO S-57 App. B.1 §2.2 violation; verified in
  the raw `09CGD_ENCs.zip`). Consolidation concatenated both, so tiles held
  exact duplicate features (Chicago z10: 51; Block Island z10: 27; also
  Boston, Delaware Bay, Miami, San Diego, Honolulu). New Stage 2b resolves
  it before consolidation: within a band the reschemed cell wins (Design
  Handbook Annex A region codes, unambiguous for bands 1-2), and every layer
  of the legacy cell is clipped under the reschemed footprints into
  `data/geojson/<band>.resolved/`. With cancelled cells excluded this
  applies to one known case, legacy `US2EC03M` under the reschemed
  `US2ATL*` cells; a pair is resolved only when exactly one side carries
  an Annex A band 1-2 region code, and every other overlap is a warning.
  Each overlap is recorded in `data/merged/<band>/.same-band-overlaps.json`
  for reporting to NOAA.
  Research and evidence in `docs/SAME-BAND-OVERLAP.md`.
- Resume: a cell whose GeoJSON directory held an orphan layer file (an
  object class the cell no longer has after an ER update, or a leftover
  from an older export) never passed the freshness check, so it was
  re-exported on every run, which in turn re-consolidated and re-tiled
  its whole band. The export now clears all of a cell's outputs before
  rewriting it. In the 01CGD data 15 cells were in this state, forcing
  bands 3-5 to rebuild on every resume.
- Layer names were taken from the part of the filename before the first
  underscore, which folded every S-57 meta layer (`M_COVR`, `M_QUAL`,
  `M_NSYS`, `M_SDAT`, `M_VDAT`, `M_NPUB`) into one tile layer called `M`
  and `TS_FEB` into `TS`. Only a trailing NOAA cell name is stripped now,
  and stale merged layer files from the old naming are removed on the next
  consolidation.
- `check-tile-overlap.py`: diagnostic that reports tile addresses shared
  between tilesets per zoom and decodes a tile (or a lon/lat at several
  zooms) across all of them, showing which layers each contributes.

### Changed
- Gap fills are render sources with priority `cellband - 0.1` and go
  through the same erase rule. At each zoom the band NOAA compiled for
  that zoom outranks everything, then finer data wins, so a fill appears
  only where the natural pipeline has no chart and is fully erased
  wherever its own band renders. Fills of the same cell band do not erase
  each other.
- Runs whose every layer was erased are skipped instead of being handed
  to tippecanoe, which exits non-zero on empty input.
- GDAL must provide the SQLite dialect's spatial functions (`ST_Union`,
  `ST_Difference`) for by-band mode: a build with SpatiaLite or GEOS.
  Ubuntu `gdal-bin` (CI) and Homebrew `gdal` qualify. The alpine-small
  container image has not been verified.
- `trim_low_bands_to_region` operates on a (priority, path) list so a
  band can contribute more than one tileset.
- `build_clip_complement` now takes footprint files, a label and a mask
  directory (shared by the band-level erase and Stage 2b);
  `consolidate_geojson` accepts per-cell file overrides; gap-fill cell lookup
  only scans `bandN` directories, not `bandN.resolved`.
- `verify-r2-charts.py`: downloads each published district file from R2 one
  at a time, measures coverage gaps and stacking at probe points, and writes
  a Markdown report. Alaska is excluded unless named.

## [0.6.0] - 2026-09-02

### Added
- `BAND_ZOOM_EXTENSION`: in by-band mode each band renders two zoom levels
  past its native ceiling (capped at `--maxzoom`), so band 4 reaches z16 and
  fills deep zooms wherever no harbour chart exists. Overview bands stop
  early; rendering band 1 to z16 blew CI time and disk on Pacific districts.
- Gap fills: named groups of higher-band cells in `enc-sources.yaml`
  (`gap_fills:`) rendered at a lower zoom range to cover NOAA legacy-ENC
  coverage holes. Output slots between band 2 and band 3 in tile-join.
  Groups added for Gulf of Maine, Downeast Maine, and other documented gaps.
- By-band mode clips band 1/2 (overview/general) output to the district's
  regional extent. The region is the union of the district's own band 3+
  chart footprints, rasterized as a z11 tile mask and dilated by one tile,
  so multi-part districts (e.g. Hawaii + Guam + Samoa) are handled without
  a single bounding box. Ocean-basin overview cells such as US1PO02M no
  longer plant planet-wide tiles and -180..180 bounds in district files.
  The clip is written to a derived `<band>.region.mbtiles` and never
  modifies the cached tippecanoe output, so resuming after adding inputs
  that widen the region still yields full overview coverage.
- Heavy layers (`SOUNDG`, lights, buoys, beacons, obstructions, wrecks,
  rocks) start one zoom level above their band's bottom zoom, via a
  per-feature `tippecanoe.minzoom` stamp applied during consolidation
  (`LAYER_MIN_ZOOM_OFFSET`). tippecanoe silently ignores per-layer minzoom
  in the `-L` spec, so the feature-level stamp is the only form it honors.
- `find-band-holes.py`: stdlib tool that reads the NOAA ENC product
  catalog and reports, per district, where lower-band cells are not
  covered by higher-band cells (i.e. where the tile pyramid goes blank).
- CI: Cloudflare R2 mirror for every chart file, with release notes linking
  to the R2 copies; handles an in-bucket folder path and a trailing slash
  in the bucket secret.
- CI: `restore-release.yml` one-shot workflow to re-attach artifacts from a
  prior run to a release without downloading them locally.
- CI: active builds expanded to Coast Guard districts 05, 07, 08, 09, 11,
  13, 14. District 17 (Alaska) is listed but disabled: its 2.3 GB output
  exceeds GitHub's 2 GiB release-asset limit.

### Changed
- tippecanoe runs once per band/source over its full zoom range instead of
  once per zoom level. Merged layers are passed as separate `-L` layers.
- Every stage is incremental by modification time: GDAL re-exports only
  cells whose `.000` or update files are newer than their GeoJSON,
  consolidation rebuilds only layers with newer inputs (or when the heavy-
  layer stamp config changed), and a band's `.mbtiles` is reused only if
  it is newer than every merged layer and its stored zoom range matches.
- The region mask reads only each band's bottom-zoom tile rows instead of
  every row in the file, avoiding multi-million-row reads from band 4/5
  files rendered to z16.
- Version is now tracked only in `pyproject.toml`. The hardcoded
  `__version__` string and the `--version` flag are removed from the script.
- CI: release job runs even when some matrix builds fail and uploads
  assets one at a time, so one oversized or failed asset cannot abort the
  rest. Assets over 2 GiB are skipped with a warning; assets from districts
  not rebuilt this run are preserved.
- CI: workflow pins `actions/checkout@v7`, `actions/upload-artifact@v7`,
  and `actions/download-artifact@v8` (Node 24 runtime), clearing the
  Node 20 deprecation warning.
- `docs/USAGE.md` rewritten to match the current pipeline (data directory
  layout, single tippecanoe run per band, freshness rules, options).

## [0.5.0] - 2026-03-28

### Added
- Five-stage conversion pipeline: extract, GDAL export, consolidate, tippecanoe, tile-join
- By-band mode (`--by-band`) for multi-state builds grouped by NOAA usage band
- Two-source split mode (`--split`) for coarse + detail merging
- Multi-source mode (`--sources`) with explicit zoom ranges per input
- GeoJSON reuse mode (`--geojson-dir`) to skip the slow GDAL step on re-runs
- Resume support: skips zoom levels where tiles already exist
- Parallel processing (`-j` flag) for GDAL export, GeoJSON consolidation, and tippecanoe
- Native GDAL support alongside containerized GDAL (Docker/Podman)
- SOUNDG depth sounding fix: `SPLIT_MULTIPOINT=YES` + `ADD_SOUNDG_DEPTH=YES` for individual depth points
- GeoJSON consolidation stage to merge per-chart layers before tiling
- `enc-sources.yaml` config for CI builds with active list
- GitHub Actions workflow for automated NOAA chart builds
- `--version` flag
- Installation guide for macOS and Raspberry Pi
- Detailed usage guide covering all five modes
- SOUNDG fix writeup documenting the depth sounding bug and solution
- CI: per-build metadata JSON sidecar generated alongside each `.mbtiles`
  and uploaded with it
- MIT license
