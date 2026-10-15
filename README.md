# s57-to-mbtiles

Convert NOAA S-57 ENC nautical charts (`.000` files) into vector MBTiles for use with [SignalK](https://signalk.org/) / [Freeboard-SK](https://github.com/SignalK/freeboard-sk).

## What it does

Takes one or more ZIP archives of S-57 ENC files from NOAA and runs a five-stage pipeline:

1. **Extract** ZIPs into a staging area
2. **GDAL** (`ogr2ogr`) converts each ENC layer to GeoJSON
3. **Consolidate** per-layer GeoJSON across charts
4. **tippecanoe** builds vector tiles per zoom level
5. **tile-join** merges everything into a single `.mbtiles` file

With `--pmtiles` the same tileset is also written as a single-file `.pmtiles` archive (identical tiles and metadata; needs [go-pmtiles](https://github.com/protomaps/go-pmtiles)). The CI build publishes both.

The output can be served directly by [signalk-charts-provider-simple](https://github.com/SignalK/signalk-charts-provider-simple) and rendered by Freeboard-SK's S-57 style engine.

## Quick start

### Prerequisites

- Python 3 (standard library only)
- [tippecanoe](https://github.com/felt/tippecanoe) (native install)
- [go-pmtiles](https://github.com/protomaps/go-pmtiles) — optional, only for `--pmtiles`
- GDAL (`ogr2ogr`) — native install or via Docker/Podman container

See [docs/INSTALL.md](docs/INSTALL.md) for platform-specific setup (macOS, Raspberry Pi).

### Download charts

Get ENC ZIPs from NOAA: https://charts.noaa.gov/ENCs/ENCs.shtml

Files follow the pattern `https://charts.noaa.gov/ENCs/{NAME}_ENCs.zip` — by state (e.g., `CT_ENCs.zip`) or Coast Guard district (e.g., `01CGD_ENCs.zip`).

### Run

```bash
# Single state
./s57-to-mbtiles.py CT_ENCs.zip

# Multiple states, grouped by NOAA band (recommended)
./s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip \
  --by-band -o ne-coast.mbtiles

# Reuse existing GeoJSON (skip the slow GDAL step)
./s57-to-mbtiles.py --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12
```

All intermediate files are stored in `./data/` and preserved between runs for resume capability.

## Modes

| Mode | Use case | Example |
|---|---|---|
| **Single source** | One ZIP or directory | `./s57-to-mbtiles.py NY_ENCs.zip` |
| **By-band** | Multi-state, auto-grouped by NOAA usage band | `--by-band` flag |
| **Two-source split** | Coarse + detail at a zoom boundary | `--split 12` |
| **Multi-source** | Explicit zoom ranges per source | `--sources file1:9-11 file2:12-16` |
| **GeoJSON reuse** | Skip GDAL, rebuild tiles only | `--geojson-dir path/` |

See [docs/USAGE.md](docs/USAGE.md) for full details on all modes and CLI options.

## GitHub Actions / CI

The repo includes a GitHub Actions workflow that can download ENC ZIPs from NOAA and build tiles automatically. Builds are defined in `enc-sources.yaml`:

```yaml
active:
  - 01cgd          # just edit this list to control what gets built
```

Finished `.mbtiles` files are uploaded as GitHub Release assets. Builds run automatically on the 1st of each month (April-November) or manually from the Actions tab.

A second workflow, "Build Navigation Meshes", runs after each chart publish and builds a routing mesh of every district whose chart is newer than its mesh (`build-mesh.py`, see [docs/MESH.md](docs/MESH.md)). The mesh is published beside the charts as `<district>_mesh.tar.zst` and `<district>_mesh.json`, and unpacked at `charts/mesh/<district>/` on R2. The `mesh:` section of `enc-sources.yaml` lists the districts.

## Forking

This repo is designed to be forked. Each fork maintains its own builds and releases independently.

1. **Fork** this repo on GitHub
2. **Edit `enc-sources.yaml`** — set your `active` list to the regions you need
3. **Go to Actions** tab and enable workflows on your fork
4. **Run "Build ENC Charts"** manually, or wait for the monthly schedule

The only file you need to customize is `enc-sources.yaml`. To prevent your local edits from showing up in `git status` or getting accidentally committed:

```bash
git update-index --skip-worktree enc-sources.yaml
```

To pull upstream code changes:

```bash
git remote add upstream https://github.com/motamman/s57Work.git
git fetch upstream
git merge upstream/main
```

If `enc-sources.yaml` conflicts on merge, keep your version — it's the one file meant to differ per fork. To undo skip-worktree (e.g., to resolve a conflict): `git update-index --no-skip-worktree enc-sources.yaml`

## NOAA band / zoom mapping

| Band | Description | Zoom levels |
|---|---|---|
| 1 | Overview | z7-8 |
| 2 | General | z9-10 |
| 3 | Coastal | z11-12 |
| 4 | Approach | z13-14 |
| 5 | Harbour | z15-16 |
| 6 | Berthing | z17-18 |

In by-band mode each band also renders two zoom levels past its native ceiling (capped at `--maxzoom`), so wherever no finer chart exists the best available band still fills the deeper zooms. It also renders two zoom levels below its native floor (capped at `--minzoom`), so where NOAA has no chart of the band native to a zoom (e.g. the band 2 gap off North Carolina left by the cancelled US2EC02M) the finer chart fills those zooms instead of leaving them blank or showing an overview chart stretched past its scale; wherever the native band exists, it wins. Band 1/2 tiles are then clipped to the district's own region, the union of its band 3+ chart footprints, because ocean-basin overview cells would otherwise put planet-wide tiles and bounds into every district file. The clip is written to a separate `*.region.mbtiles` file in `data/tiles/`; the per-band tippecanoe outputs are left untouched so resume keeps working.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — Installation guide (macOS, Raspberry Pi)
- [docs/USAGE.md](docs/USAGE.md) — Detailed usage guide with all CLI options
- [docs/SOUNDG-FIX.md](docs/SOUNDG-FIX.md) — Depth sounding fix writeup
- [docs/MESH.md](docs/MESH.md) — Navigation mesh build and workflow

## License

This project is provided as-is for personal and educational use. NOAA chart data is public domain.
