"""Stage 1 of the mesh build on a hand-made chart file (mesh/decode.py),
and the tile a triangle is filed under (mesh/build.py tile_index).
Needs the packages in mesh/requirements.txt; skips without.

The chart file: one zoom-14 tile whose depth area (100 m) and coverage
fill it, no zoom-15 tile, and two of its sixteen zoom-16 tiles:

  * A, filled by a depth area of 10 m and by coverage;
  * B, a tile at a chart's edge: its depth area (20 m) and its coverage
    fill only its west half.

What is pinned:
  * every spot gets the most detailed zoom at which the chart covers it:
    10 m in A, 20 m in B's west half, and the zoom-14 100 m in B's east
    half (the strip a "is there a zoom-16 tile" rule left empty) and
    under the fourteen missing zoom-16 tiles;
  * nowhere two zooms at once, and no gap: the depth areas' pieces add up
    to the zoom-14 tile's area;
  * cutting an area leaves only areas (a stray line makes the build drop
    the whole rejoined feature);
  * the counts per zoom, and that the zoom-14 tile is read once.
"""
import gzip
import json
import pickle
import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    import mapbox_vector_tile
    import numpy as np
    import shapely
    from shapely.geometry import Point, box
    from mesh import decode
    from mesh.build import tile_index
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

Z14 = (5008, 6043)                 # column, XYZ row: about 69.96 W, 42.63 N
E = 4096


def _tile(depth, west_half=False):
    """A vector tile with one depth area and one coverage polygon, filling the tile or its west half."""
    x1 = E // 2 if west_half else E
    ring = f"POLYGON((0 0, {x1} 0, {x1} {E}, 0 {E}, 0 0))"
    layers = [
        {"name": "DEPARE", "features": [{"geometry": ring, "properties": {"DRVAL1": depth, "LNAM": f"D{depth}"}}]},
        {"name": "M_COVR", "features": [{"geometry": ring, "properties": {"CATCOV": 1, "LNAM": f"C{depth}"}}]},
    ]
    return gzip.compress(mapbox_vector_tile.encode(layers, default_options={"y_coord_down": True, "extents": E}))


@unittest.skipUnless(HAVE_MESH_DEPS, "needs the mesh packages (mesh/requirements.txt)")
class DeepestCoveringZoom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="s57decode-")
        work = Path(cls.tmp.name)
        cls.mbtiles = work / "hand.mbtiles"
        cls.a = (Z14[0] * 4 + 1, Z14[1] * 4 + 1)       # zoom-16 tile A
        cls.b = (Z14[0] * 4 + 2, Z14[1] * 4 + 2)       # zoom-16 tile B
        con = sqlite3.connect(cls.mbtiles)
        con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        rows = [(14, Z14[0], 2 ** 14 - 1 - Z14[1], _tile(100)),
                (16, cls.a[0], 2 ** 16 - 1 - cls.a[1], _tile(10)),
                (16, cls.b[0], 2 ** 16 - 1 - cls.b[1], _tile(20, west_half=True))]
        con.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", rows)
        con.commit()
        con.close()
        cls.lines = []
        cls.res = decode.decode_to_pickles([str(cls.mbtiles)], str(work / "z16_layers"), 1, log=cls.lines.append)
        cls.pieces = {}
        for L in ("DEPARE", "M_COVR"):
            with open(work / "z16_layers" / f"{L}.pkl", "rb") as fh:
                cls.pieces[L] = [(shapely.from_wkb(w), json.loads(p)) for w, p in pickle.load(fh)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def depths_at(self, lon, lat):
        return sorted(p["DRVAL1"] for g, p in self.pieces["DEPARE"] if g.contains(Point(lon, lat)))

    def test_counts_per_zoom(self):
        self.assertEqual(self.res["by_zoom"], {14: 1, 16: 2})
        self.assertEqual(self.res["tiles"], 3)
        self.assertEqual(self.res["failed"], [])

    def test_each_spot_gets_the_most_detailed_covering_zoom(self):
        aw, as_, ae, an = decode.tile_bounds_xyz(16, *self.a)
        bw, bs, be, bn = decode.tile_bounds_xyz(16, *self.b)
        self.assertEqual(self.depths_at((aw + ae) / 2, (as_ + an) / 2), [10], "tile A: its own depth area only")
        self.assertEqual(self.depths_at(bw + (be - bw) * 0.25, (bs + bn) / 2), [20], "tile B, covered half")
        self.assertEqual(self.depths_at(bw + (be - bw) * 0.75, (bs + bn) / 2), [100],
                         "tile B, the half the chart does not cover at zoom 16: the zoom-14 depth area")
        w, s, e, n = decode.tile_bounds_xyz(14, *Z14)
        self.assertEqual(self.depths_at(w + (e - w) * 0.05, s + (n - s) * 0.05), [100],
                         "no zoom-16 tile there: the zoom-14 depth area")

    def test_no_gap_and_no_overlap(self):
        w, s, e, n = decode.tile_bounds_xyz(14, *Z14)
        tile = box(w, s, e, n)
        total = sum(g.area for g, _ in self.pieces["DEPARE"])
        self.assertAlmostEqual(total / tile.area, 1.0, places=6, msg="the pieces add up to the zoom-14 tile")
        union = shapely.union_all([g for g, _ in self.pieces["DEPARE"]])
        self.assertAlmostEqual(union.area / tile.area, 1.0, places=6, msg="and cover it")

    def test_areas_stay_areas(self):
        for L, rows in self.pieces.items():
            kinds = {g.geom_type for g, _ in rows}
            self.assertLessEqual(kinds, {"Polygon", "MultiPolygon"}, f"{L}: {kinds}")

    def test_pieces_keep_source_and_order(self):
        for g, p in self.pieces["DEPARE"]:
            self.assertEqual(p["__src"], "hand.mbtiles")
            self.assertIn("__ord", p)


@unittest.skipUnless(HAVE_MESH_DEPS, "needs the mesh packages (mesh/requirements.txt)")
class TileIndex(unittest.TestCase):
    BOX = (-74.749875, 39.750125, -65.749875, 45.750125)       # 01CGD: 36 x 24 tiles
    K = 0.734321

    def idx(self, lon, lat):
        ti, tj = tile_index(np.array([[lon * self.K, lat]]), self.BOX, 0.25, self.K)
        return int(ti[0]), int(tj[0])

    def test_inside(self):
        self.assertEqual(self.idx(-74.7, 39.8), (0, 0))
        self.assertEqual(self.idx(-65.8, 45.7), (35, 23))
        self.assertEqual(self.idx(-69.7644, 42.6382), (19, 11))

    def test_a_centroid_just_past_the_east_or_north_edge_stays_in_the_last_tile(self):
        # the two 01CGD triangles of 2026-11-20: centroids 1e-7 and 6e-7 degrees past the east edge, row 2
        self.assertEqual(self.idx(-65.7498748957705, 40.3231029677328), (35, 2))
        self.assertEqual(self.idx(-65.74987437927123, 40.32310751475019), (35, 2))
        self.assertEqual(self.idx(-70.0, 45.7501251), (18, 23))

    def test_a_centroid_just_past_the_west_or_south_edge_stays_in_the_first_tile(self):
        self.assertEqual(self.idx(-74.7498751, 39.7501249), (0, 0))


if __name__ == "__main__":
    unittest.main()
