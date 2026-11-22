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
  * the counts per zoom, and that the zoom-14 tile is read once;
  * a chart cell of another file that is rendered deeper than zoom 14
    keeps the zoom-14 tile out of its outline, and one that is not
    rendered deeper does not;
  * the catalogue reader (live cells with their outlines, a cancelled
    cell left out) and the zoom ceilings taken from the chart build;
  * build-mesh.py refuses to decode without --catalog or --no-catalog.
"""
import gzip
import json
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h

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


def make_hand_chart(path):
    """Write the hand-made chart file; returns the XYZ addresses of zoom-16 tiles A and B."""
    a = (Z14[0] * 4 + 1, Z14[1] * 4 + 1)
    b = (Z14[0] * 4 + 2, Z14[1] * 4 + 2)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    con.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [
        (14, Z14[0], 2 ** 14 - 1 - Z14[1], _tile(100)),
        (16, a[0], 2 ** 16 - 1 - a[1], _tile(10)),
        (16, b[0], 2 ** 16 - 1 - b[1], _tile(20, west_half=True))])
    con.commit()
    con.close()
    return a, b


def decode_hand_chart(work, catalog_cells=None):
    """Decode the hand-made chart file in `work`; returns (result, {layer: [(geometry, properties)]}, A, B)."""
    mbtiles = Path(work) / "hand.mbtiles"
    a, b = make_hand_chart(mbtiles)
    out = Path(work) / "z16_layers"
    res = decode.decode_to_pickles([str(mbtiles)], str(out), 1, log=lambda m: None, catalog_cells=catalog_cells)
    pieces = {}
    for L in ("DEPARE", "M_COVR"):
        with open(out / f"{L}.pkl", "rb") as fh:
            pieces[L] = [(shapely.from_wkb(w), json.loads(p)) for w, p in pickle.load(fh)]
    return res, pieces, a, b


def depths_at(pieces, lon, lat):
    return sorted(p["DRVAL1"] for g, p in pieces["DEPARE"] if g.contains(Point(lon, lat)))


@unittest.skipUnless(HAVE_MESH_DEPS, "needs the mesh packages (mesh/requirements.txt)")
class DeepestCoveringZoom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="s57decode-")
        cls.res, cls.pieces, cls.a, cls.b = decode_hand_chart(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts_per_zoom(self):
        self.assertEqual(self.res["by_zoom"], {14: 1, 16: 2})
        self.assertEqual(self.res["tiles"], 3)
        self.assertEqual(self.res["failed"], [])
        self.assertEqual(self.res["left_to_finer"], 0)

    def test_each_spot_gets_the_most_detailed_covering_zoom(self):
        aw, as_, ae, an = decode.tile_bounds_xyz(16, *self.a)
        bw, bs, be, bn = decode.tile_bounds_xyz(16, *self.b)
        self.assertEqual(depths_at(self.pieces, (aw + ae) / 2, (as_ + an) / 2), [10], "tile A: its own depth area only")
        self.assertEqual(depths_at(self.pieces, bw + (be - bw) * 0.25, (bs + bn) / 2), [20], "tile B, covered half")
        self.assertEqual(depths_at(self.pieces, bw + (be - bw) * 0.75, (bs + bn) / 2), [100],
                         "tile B, the half the chart does not cover at zoom 16: the zoom-14 depth area")
        w, s, e, n = decode.tile_bounds_xyz(14, *Z14)
        self.assertEqual(depths_at(self.pieces, w + (e - w) * 0.05, s + (n - s) * 0.05), [100],
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
class FinerChartInAnotherFile(unittest.TestCase):
    """The south-west quarter of the zoom-14 tile has no zoom-16 tile in this file. A cell of another district's
    file whose outline is that quarter decides whether the zoom-14 depth area is read there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s57decode-")
        w, s, e, n = decode.tile_bounds_xyz(14, *Z14)
        self.quarter = box(w, s, (w + e) / 2, (s + n) / 2)
        self.inside = (w + (e - w) * 0.1, s + (n - s) * 0.1)
        self.outside = (w + (e - w) * 0.1, s + (n - s) * 0.9)          # north-west quarter: no zoom-16 tile either

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_approach_cell_keeps_the_coarser_zoom_out_of_its_outline(self):
        _, pieces, _, _ = decode_hand_chart(self.tmp.name, [("US4XX1AA", 4, self.quarter)])   # band 4: rendered to z16
        self.assertEqual(depths_at(pieces, *self.inside), [], "left to the file that holds the finer cell")
        self.assertEqual(depths_at(pieces, *self.outside), [100], "outside the outline the zoom-14 depth area stays")

    def test_a_cell_not_rendered_deeper_changes_nothing(self):
        _, pieces, _, _ = decode_hand_chart(self.tmp.name, [("US2XXAAA", 2, self.quarter)])   # band 2: rendered to z12
        self.assertEqual(depths_at(pieces, *self.inside), [100])

    def test_a_tile_wholly_inside_a_finer_cell_is_not_read(self):
        w, s, e, n = decode.tile_bounds_xyz(14, *Z14)
        res, pieces, a, b = decode_hand_chart(self.tmp.name, [("US4XX1AA", 4, box(w - 1, s - 1, e + 1, n + 1))])
        self.assertEqual(res["by_zoom"], {16: 2})
        self.assertEqual(res["left_to_finer"], 1)
        self.assertEqual(sorted({p["DRVAL1"] for _, p in pieces["DEPARE"]}), [10, 20], "the zoom-16 tiles are untouched")


CATALOG_XML = """<DS_Series><composedOf><DS_DataSet><has><MD_Metadata><identificationInfo><MD_DataIdentification>
<citation><CI_Citation><title><gco:CharacterString>US4MA1IF</gco:CharacterString></title></CI_Citation></citation>
<gml:Polygon><gml:exterior><gml:LinearRing>
<gml:pos>42.6 -70.2</gml:pos><gml:pos>42.9 -70.2</gml:pos><gml:pos>42.9 -69.9</gml:pos><gml:pos>42.6 -69.9</gml:pos><gml:pos>42.6 -70.2</gml:pos>
</gml:LinearRing></gml:exterior></gml:Polygon>
</MD_DataIdentification></identificationInfo></MD_Metadata></has></DS_DataSet></composedOf>
<composedOf><DS_DataSet><has><MD_Metadata><identificationInfo><MD_DataIdentification>
<citation><CI_Citation><title><gco:CharacterString>US3EC11M</gco:CharacterString></title></CI_Citation></citation>
</MD_DataIdentification></identificationInfo></MD_Metadata></has></DS_DataSet></composedOf></DS_Series>
"""


@unittest.skipUnless(HAVE_MESH_DEPS, "needs the mesh packages (mesh/requirements.txt)")
class Catalogue(unittest.TestCase):
    def test_live_cells_with_outlines_and_no_cancelled_cell(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cat.xml"
            p.write_text(CATALOG_XML)
            cells = decode.read_catalog_cells(str(p))
        self.assertEqual([(name, band) for name, band, _ in cells], [("US4MA1IF", 4)], "the cancelled cell has no outline")
        self.assertEqual(tuple(round(v, 6) for v in cells[0][2].bounds), (-70.2, 42.6, -69.9, 42.9), "longitude first")

    def test_zoom_ceilings_come_from_the_chart_build(self):
        self.assertEqual(decode.band_ceilings(9, 16), {1: 10, 2: 12, 3: 14, 4: 16, 5: 16},
                         "band 6 is not rendered in a file that ends at zoom 16")

    def test_which_cells_count_for_a_tile_zoom(self):
        cells = [("US3XX1AA", 3, box(0, 0, 1, 1)), ("US4XX1AA", 4, box(2, 0, 3, 1))]
        f = decode.FinerElsewhere(cells, decode.band_ceilings(9, 16))
        both = box(-1, -1, 4, 2)
        self.assertAlmostEqual(f.over(14, both).area, 1.0, msg="at zoom 14 only the band 4 cell is rendered deeper")
        self.assertAlmostEqual(f.over(13, both).area, 2.0, msg="at zoom 13 the band 3 cell (to zoom 14) is too")
        self.assertIsNone(f.over(14, box(10, 10, 11, 11)))


@unittest.skipUnless(HAVE_MESH_DEPS, "needs the mesh packages (mesh/requirements.txt)")
class CliWantsTheCatalogue(unittest.TestCase):
    def test_refuses_without_catalog_or_no_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            mbtiles = Path(d) / "hand.mbtiles"
            make_hand_chart(mbtiles)
            p = subprocess.run([sys.executable, str(h.ROOT / "build-mesh.py"), str(mbtiles), "-o", str(Path(d) / "out"),
                                "--work", str(Path(d) / "work")], cwd=h.ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr[-500:])
        self.assertIn("--catalog", p.stderr)


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
