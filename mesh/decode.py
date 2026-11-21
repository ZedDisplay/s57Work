"""Stage 1: decode the tiles of MBTiles chart files into per-layer
geometry lists, the way the old grid build read them (port of the
experiment's mbtiles_to_layers.py):

  * gzip + mapbox_vector_tile.decode(data, y_coord_down=True)
  * tile pixel -> lon/lat: lon = tw + px/4096*(te-tw), lat = tn - py/4096*(tn-ts)
  * each tile's features are clipped to the part of the tile it is read for

Which tiles (changed 2026-11-21; the experiment read zoom 16 only, with
no fallback to coarser tiles): for every spot, the most detailed zoom at
which the chart covers the spot.

  * every zoom-16 tile, whole, as before;
  * then zoom 15, 14, ... down to the file's lowest: each tile only over
    the part of it that no deeper tile's coverage already fills. Coverage
    is the chart's own M_COVR with CATCOV=1, as carried in the tiles.

Why: the chart build renders a coastal-band (band 3) cell to zoom 14 and
a general-band cell to zoom 12 (s57-to-mbtiles.py BAND_ZOOM plus
BAND_ZOOM_EXTENSION); only approach and harbour cells and the gap fills
reach zoom 16. With zoom 16 alone, water whose finest chart is band 3
had no depth area, hazard or sounding in the mesh although the chart
file holds them: 01CGD of 2026-11-19 has a 237.7 m depth area at
42.6382, -69.7644 at zoom 14 and no tile there at zoom 15 or 16.

Why coverage and not "is there a deeper tile": a zoom-16 tile at a
chart's edge exists but the chart covers only part of it; going by the
tile's existence left the rest of it empty, a strip one tile wide along
chart edges (measured at -69.9, 42.625 on the same file).

Several MBTiles sources are decoded independently. Each piece keeps its
source ("__src") and its position in the tile ("__ord"): within one
source a dredged area replaces the depth and the LAST obstruction wins,
so the order matters.

Output: one pickle per layer in OUT, a list of (wkb, properties-json)
tuples — the same files the experiment wrote to data/z16_layers/ (the
folder keeps that name).
"""
import gzip
import json
import math
import os
import pickle
import sqlite3
import time
from multiprocessing import get_context

import mapbox_vector_tile
import shapely
from shapely.geometry import box, shape

OBS_RANK = {"OBSTRN": 0, "UWTROC": 1, "WRECKS": 2}     # obstruction layer order
ZOOM = 16
EXTENT = 4096
# A tile position the chart's coverage fills, in the per-zoom coverage tables below.
FULL = "F"
# The uncovered share of a tile below which it counts as filled: coverage outlines are tile-pixel coordinates
# (1/4096 of a tile), so a filled tile's union differs from the tile's box by rounding only.
FULL_TOL = 1e-6
# Degrees around the zoom-16 tiles' bounding box within which coarser tiles are read: the mesh rectangle is drawn
# around the 0.25-degree cells that hold zoom-16 tiles (mesh/extent.py), so nothing farther out can reach a mesh.
REACH_DEG = 0.3
LAYERS = {"LNDARE", "SLCONS", "PONTON", "FLODOC", "HULKES", "CAUSWY", "DAMCON", "GATCON", "DRYDOC",
          "MORFAC", "OFSPLF", "PILPNT", "DEPARE", "DRGARE", "SOUNDG", "UNSARE", "OBSTRN", "WRECKS",
          "UWTROC", "BOYLAT", "BOYCAR", "BOYSPP", "BOYSAW", "BOYISD", "BOYINB", "BCNLAT", "BCNCAR",
          "BCNSPP", "BCNSAW", "BCNISD", "LIGHTS", "DAYMAR", "PILBOP", "BRIDGE", "CBLOHD", "PIPOHD",
          "CONVYR", "FAIRWY", "RECTRC", "NAVLNE", "TSSLPT", "TSEZNE", "RESARE", "M_QUAL", "M_COVR"}


def tile_bounds_xyz(z, x, y):
    n = 2 ** z
    w = x / n * 360.0 - 180.0
    e = (x + 1) / n * 360.0 - 180.0
    nl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    sl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return w, sl, e, nl


def tile_box_tms(z, x, y_tms):
    """The box of a tile addressed as the MBTiles file does (TMS row)."""
    return box(*tile_bounds_xyz(z, x, (2 ** z - 1) - y_tms))


def z16_tiles(mbtiles):
    """[(tile_column, tile_row_tms)] of every z16 tile in the file."""
    con = sqlite3.connect(mbtiles)
    try:
        return con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?",
                           (ZOOM,)).fetchall()
    finally:
        con.close()


def z16_cells(mbtiles_paths, tile_deg, cell_of):
    """The set of tile cells that contain any z16 tile of the inputs;
    `cell_of(lon, lat)` maps a point to its (col, row) (mesh.extent.cell).
    A z16 tile is tiny (~0.0055 deg) and never straddles a cell edge by
    more than one cell, so all four corners are tested."""
    cells = set()
    n = 2 ** ZOOM
    for p in mbtiles_paths:
        for x, y_tms in z16_tiles(p):
            w, s, e, nl = tile_bounds_xyz(ZOOM, x, (n - 1) - y_tms)
            for lon in (w + 1e-9, e - 1e-9):
                for lat in (s + 1e-9, nl - 1e-9):
                    cells.add(cell_of(lon, lat))
    return cells


def _decode(task):
    """One tile: (src, zoom, tile_column, tile_row_tms, data, region). `region` (wkb, or None for the whole tile)
    is the part of the tile to keep. Returns the pieces per layer and two entries of its own: "__key__"
    (zoom, column, row) and "__cov__", the chart's coverage inside the kept part: FULL when it fills it, its wkb
    when it fills some of it, None when there is none."""
    src, z, tx, ty_tms, data, region = task
    y_xyz = (2 ** z - 1) - ty_tms
    tw, ts, te, tn = tile_bounds_xyz(z, tx, y_xyz)
    out = {"__key__": (z, tx, ty_tms), "__cov__": None}
    try:
        tile = mapbox_vector_tile.decode(gzip.decompress(data), default_options={"y_coord_down": True})
    except Exception:
        out["__failed__"] = [(src, z, tx, ty_tms)]
        return out
    tb = box(tw, ts, te, tn)
    if region is not None:
        tb = shapely.intersection(tb, shapely.from_wkb(region))
        if tb.is_empty:
            return out
    sx, sy = (te - tw) / EXTENT, (tn - ts) / EXTENT
    cov = []
    for L, layer in tile.items():
        if L not in LAYERS:
            continue
        ext = layer.get("extent", EXTENT)
        if ext != EXTENT:
            # The pixel -> degree mapping above assumes tippecanoe's default
            # 4096 extent, as the grid build did. Anything else is a build
            # change upstream, not something to paper over here.
            raise RuntimeError(f"{src} z{z}/{tx}/{y_xyz} layer {L}: MVT extent {ext}, expected {EXTENT}")
        for fi, f in enumerate(layer.get("features", [])):
            try:
                g = shape(f["geometry"])
            except Exception:
                continue
            g = shapely.transform(g, lambda c: c * [sx, -sy] + [tw, tn])
            if g.geom_type in ("Point", "MultiPoint"):
                g = shapely.intersection(g, tb)        # points: inside the kept part only
            else:
                # Only what is of the feature's own kind is kept. Cutting an area by the kept part leaves a
                # line or a point where the two only touch; the build rejoins a feature's pieces into one
                # geometry and keeps it only when that is purely an area (mesh/build.py load), so one stray
                # line dropped the whole feature: with the kept part an irregular region, whole 0.3-degree
                # blocks of LNDARE and DEPARE went that way (01CGD, 2026-11-21).
                want = "Polygon" if g.geom_type in ("Polygon", "MultiPolygon") else "LineString"
                g = shapely.intersection(shapely.make_valid(g), tb)
                parts = [p for p in shapely.get_parts(g) if p.geom_type == want and not p.is_empty]
                if not parts:
                    continue
                if len(parts) == 1:
                    g = parts[0]
                else:
                    g = shapely.multipolygons(parts) if want == "Polygon" else shapely.multilinestrings(parts)
            if g.is_empty:
                continue
            pr = dict(f.get("properties", {}))
            if L == "M_COVR" and pr.get("CATCOV") == 1:
                cov += [p for p in shapely.get_parts(g) if p.geom_type == "Polygon"]
            pr["__src"] = src
            pr["__ord"] = OBS_RANK.get(L, 0) * 1_000_000 + fi
            out.setdefault(L, []).append((shapely.to_wkb(g), json.dumps(pr)))
    if cov:
        u = shapely.union_all(cov)
        out["__cov__"] = FULL if tb.area - u.area <= tb.area * FULL_TOL else shapely.to_wkb(u)
    return out


def covered_below(z, x, y, below):
    """What the deeper zooms cover under tile (z, x, y): None, FULL, or a geometry. `below` is the coverage table
    of zoom z + 1: {(column, row_tms): FULL or geometry}."""
    kids = [(2 * x + dx, 2 * y + dy) for dx in (0, 1) for dy in (0, 1)]
    got = [below.get(k) for k in kids]
    if all(g is None for g in got):
        return None
    if all(isinstance(g, str) for g in got):
        return FULL
    return shapely.union_all([tile_box_tms(z + 1, *k) if isinstance(g, str) else g
                              for k, g in zip(kids, got) if g is not None])


def decode_to_pickles(mbtiles_paths, out_dir, workers, log=print):
    """Decode the inputs into OUT/<LAYER>.pkl, the most detailed covering zoom first (the module's comment).
    Returns {"tiles": n, "by_zoom": {zoom: n}, "failed": [...], "pieces": {layer: n}}."""
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    acc, failed, by_zoom = {}, [], {}
    n_tiles = 0

    def take(res, cov_out):
        nonlocal n_tiles
        n_tiles += 1
        z, x, y = res.pop("__key__")
        cov_out[(x, y)] = res.pop("__cov__")
        by_zoom[z] = by_zoom.get(z, 0) + 1
        for L, v in res.items():
            if L == "__failed__":
                failed.extend(v)
            else:
                acc.setdefault(L, []).extend(v)
        if n_tiles % 20000 == 0:
            log(f"{n_tiles:,} tiles in {time.time()-t0:.0f} s")

    with get_context("fork").Pool(workers) as pool:
        for MBT in mbtiles_paths:
            src = os.path.basename(MBT)
            # The pool's feeder thread, not this one, pulls the tasks from the generators below.
            con = sqlite3.connect(MBT, check_same_thread=False)
            zooms = [r[0] for r in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
            have = {z: set(con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,)).fetchall())
                    for z in zooms if z < ZOOM}

            def tasks16():
                for tx, ty, data in con.execute(
                        "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=?", (ZOOM,)):
                    yield (src, ZOOM, tx, ty, data, None)

            cov = {}
            for res in pool.imap_unordered(_decode, tasks16(), chunksize=64):
                take(res, cov)
            if not cov:
                con.close()
                continue
            n16 = 2 ** ZOOM
            xs = [x for x, _ in cov]
            ys = [y for _, y in cov]
            w0 = tile_bounds_xyz(ZOOM, min(xs), 0)[0] - REACH_DEG
            e0 = tile_bounds_xyz(ZOOM, max(xs), 0)[2] + REACH_DEG
            s0 = tile_bounds_xyz(ZOOM, 0, (n16 - 1) - min(ys))[1] - REACH_DEG
            n0 = tile_bounds_xyz(ZOOM, 0, (n16 - 1) - max(ys))[3] + REACH_DEG
            # The coverage table of the zoom just read, deeper zooms included: {(column, row): FULL or geometry}.
            below = {k: (v if isinstance(v, str) else shapely.from_wkb(v)) for k, v in cov.items() if v is not None}
            log(f"zoom {ZOOM}: {len(cov):,} tiles; the chart's coverage fills the tile in "
                f"{sum(1 for v in below.values() if isinstance(v, str)):,}, part of it in "
                f"{sum(1 for v in below.values() if not isinstance(v, str)):,}, none of it in {len(cov) - len(below):,}")
            for z in range(ZOOM - 1, min(zooms) - 1, -1):
                n = 2 ** z
                table = {}                 # this zoom's coverage table, built as `below` was
                plan = []                  # (column, row, region wkb or None) of the tiles to read
                filled = 0
                for (x, y) in {(bx >> 1, by >> 1) for bx, by in below} | have.get(z, set()):
                    d = covered_below(z, x, y, below)
                    if d is not None:
                        table[(x, y)] = d
                    if (x, y) not in have.get(z, ()):
                        continue
                    tw, ts, te, tn = tile_bounds_xyz(z, x, (n - 1) - y)
                    if te < w0 or tw > e0 or tn < s0 or ts > n0:
                        continue
                    if isinstance(d, str):
                        filled += 1
                        continue
                    if d is None:
                        plan.append((x, y, None))
                        continue
                    region = shapely.difference(box(tw, ts, te, tn), d)
                    if region.is_empty or region.area <= (te - tw) * (tn - ts) * FULL_TOL:
                        table[(x, y)] = FULL
                        filled += 1
                        continue
                    plan.append((x, y, shapely.to_wkb(region)))

                def tasks(z=z, plan=plan):
                    for x, y, region in plan:
                        row = con.execute(
                            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                            (z, x, y)).fetchone()
                        if row is not None:
                            yield (src, z, x, y, row[0], region)

                cov = {}
                for res in pool.imap_unordered(_decode, tasks(), chunksize=64):
                    take(res, cov)
                # A tile's own coverage inside the part it was read for, added to what the deeper zooms cover.
                for (x, y), c in cov.items():
                    if c is None:
                        continue
                    if isinstance(c, str):
                        table[(x, y)] = FULL        # it fills everything the deeper zooms left
                    else:
                        d = table.get((x, y))
                        g = shapely.from_wkb(c)
                        table[(x, y)] = g if d is None else shapely.union(d, g)
                log(f"zoom {z}: {len(have.get(z, ())):,} tiles in the file; filled by deeper zooms (not read) "
                    f"{filled:,}; read for the part no deeper zoom covers {len(plan):,}")
                below = table
            con.close()
    for L, v in acc.items():
        with open(os.path.join(out_dir, f"{L}.pkl"), "wb") as fh:
            pickle.dump(v, fh)
    pieces = {L: len(v) for L, v in sorted(acc.items())}
    log("tiles decoded per zoom: " + ", ".join(f"z{z} {c:,}" for z, c in sorted(by_zoom.items(), reverse=True)))
    log(f"done: {n_tiles:,} tiles decoded in {time.time()-t0:.1f} s; "
        f"tiles that failed to decode: {len(failed)}")
    log("pieces per layer: " + ", ".join(f"{L} {n:,}" for L, n in pieces.items()))
    return {"tiles": n_tiles, "by_zoom": dict(sorted(by_zoom.items())), "failed": failed, "pieces": pieces,
            "seconds": time.time() - t0}
