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
    the part of it that no deeper tile's coverage already fills (coverage
    is the chart's own M_COVR with CATCOV=1, as carried in the tiles), and
    that no finer chart of another district's file covers (below).

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

And only where no finer chart exists in ANY district's file (2026-11-22).
A district's file holds coarse cells that reach into the next district's
water, which that district's own file charts in detail. Read there, the
coarse chart overrode nothing in this file but said things the detailed
chart contradicts: 01CGD's coastal cell draws the New Jersey shore as one
depth area of 0 to 18.2 m and Barnegat Bay as land, so the 01CGD mesh
published on 2026-11-21 had 274 km2 at 0.0 m in one tile where 05CGD's
mesh has 9.1 m, closing to a 2.5 m boat 144 km2 that 05CGD's mesh allows.
So a coarser tile is also trimmed by the outlines of every live chart
cell, of any district, that the chart build renders deeper than that
tile: NOAA's ENC product catalogue lists the cells with their outlines
(`read_catalog_cells`), and the chart build's own zoom table says how
deep each band is rendered (`band_ceilings`). That water stays as it was
before: water of unknown depth in this mesh, charted by the mesh of the
district that has the finer cell.

Several MBTiles sources are decoded independently. Each piece keeps its
source ("__src") and its position in the tile ("__ord"): within one
source a dredged area replaces the depth and the LAST obstruction wins,
so the order matters.

Output: one pickle per layer in OUT, a list of (wkb, properties-json)
tuples — the same files the experiment wrote to data/z16_layers/ (the
folder keeps that name).
"""
import gzip
import importlib.util
import json
import math
import os
import pickle
import re
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


def band_ceilings(minzoom, maxzoom):
    """{usage band: the deepest zoom the chart build renders a cell of that band at}, for a chart file of zooms
    minzoom..maxzoom, from the chart build's own table (s57-to-mbtiles.py BAND_ZOOM and band_render_range), so the
    two cannot drift apart. A band the chart build does not render at all (band 6 with maxzoom 16) is left out:
    no file holds it, so it is not a finer chart anywhere."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "s57-to-mbtiles.py")
    spec = importlib.util.spec_from_file_location("s57_to_mbtiles", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = {}
    for band in mod.BAND_ZOOM:
        lo, hi = mod.band_render_range(band, minzoom, maxzoom)
        if lo <= hi:
            out[band] = hi
    return out


def read_catalog_cells(path):
    """[(cell name, usage band, outline)] of every live cell in NOAA's ENC product catalogue
    (ENCProdCat_19115.xml, the file the chart build reads for replacement cells). A cancelled cell stays listed
    but carries no outline and drops out. A cell with several outline polygons gives one entry per polygon."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    out = []
    for m in re.finditer(r"<title>\s*<gco:CharacterString>(US\d[A-Z0-9]{5})</gco:CharacterString>", text):
        name = m.group(1).upper()
        end = text.find("</MD_DataIdentification>", m.start())
        blk = text[m.start():end if end > 0 else m.start() + 20000]
        for pm in re.finditer(r"<gml:Polygon.*?</gml:Polygon>", blk, re.S):
            # gml:pos is "latitude longitude"
            pts = [tuple(map(float, q.split())) for q in re.findall(r"<gml:pos>([^<]*)</gml:pos>", pm.group(0))]
            if len(pts) < 4:
                continue
            g = shapely.make_valid(shapely.Polygon([(lon, lat) for lat, lon in pts]))
            if not g.is_empty:
                out.append((name, int(name[2]), g))
    return out


class FinerElsewhere:
    """Where a finer chart exists, in this file or any other district's: for a tile of zoom z, the outlines of
    the cells the chart build renders deeper than z. `cells` as read_catalog_cells returns them; `ceilings` as
    band_ceilings returns them."""

    def __init__(self, cells, ceilings):
        self.trees = {}
        for z in range(ZOOM):
            polys = [g for _, band, g in cells if ceilings.get(band, -1) > z]
            if polys:
                self.trees[z] = (polys, shapely.STRtree(polys))

    def over(self, z, tile_box):
        """The part of `tile_box` (a tile of zoom z) inside such an outline, or None."""
        t = self.trees.get(z)
        if t is None:
            return None
        polys, tree = t
        idx = tree.query(tile_box, predicate="intersects")
        if not len(idx):
            return None
        g = shapely.intersection(shapely.union_all([polys[k] for k in idx]), tile_box)
        return None if g.is_empty else g


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


def decode_to_pickles(mbtiles_paths, out_dir, workers, log=print, catalog_cells=None):
    """Decode the inputs into OUT/<LAYER>.pkl, the most detailed covering zoom first (the module's comment).
    `catalog_cells` (read_catalog_cells): the live chart cells of every district; a coarser tile is not read where
    one of them is rendered deeper. None: this file's own coverage alone decides (a chart file whose water no
    other file charts, and the tests' hand-made files).
    Returns {"tiles": n, "by_zoom": {zoom: n}, "failed": [...], "pieces": {layer: n}, "left_to_finer": n}."""
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    acc, failed, by_zoom = {}, [], {}
    n_tiles = 0
    left_to_finer = 0

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
            finer = None
            if catalog_cells is not None:
                ceilings = band_ceilings(min(zooms), ZOOM)
                finer = FinerElsewhere(catalog_cells, ceilings)
                log(f"catalogue: {len(catalog_cells):,} cell outlines; a band's deepest rendered zoom: "
                    + ", ".join(f"band {b} z{c}" for b, c in sorted(ceilings.items())))
            log(f"zoom {ZOOM}: {len(cov):,} tiles; the chart's coverage fills the tile in "
                f"{sum(1 for v in below.values() if isinstance(v, str)):,}, part of it in "
                f"{sum(1 for v in below.values() if not isinstance(v, str)):,}, none of it in {len(cov) - len(below):,}")
            for z in range(ZOOM - 1, min(zooms) - 1, -1):
                n = 2 ** z
                table = {}                 # this zoom's coverage table, built as `below` was
                plan = []                  # (column, row, region wkb or None) of the tiles to read
                filled = 0
                elsewhere = 0              # tiles whose whole remaining part a finer chart of another file has
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
                    tile_box = box(tw, ts, te, tn)
                    region = tile_box if d is None else shapely.difference(tile_box, d)
                    if region.is_empty or region.area <= tile_box.area * FULL_TOL:
                        table[(x, y)] = FULL
                        filled += 1
                        continue
                    f = finer.over(z, tile_box) if finer is not None else None
                    if f is not None:
                        region = shapely.difference(region, f)
                        if region.is_empty or region.area <= tile_box.area * FULL_TOL:
                            elsewhere += 1
                            continue
                    plan.append((x, y, None if d is None and f is None else shapely.to_wkb(region)))

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
                left_to_finer += elsewhere
                log(f"zoom {z}: {len(have.get(z, ())):,} tiles in the file; filled by deeper zooms (not read) "
                    f"{filled:,}; left whole to a finer chart of another file {elsewhere:,}; "
                    f"read for the part no finer chart covers {len(plan):,}")
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
            "left_to_finer": left_to_finer, "seconds": time.time() - t0}
