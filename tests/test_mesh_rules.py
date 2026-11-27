"""What a piece of water carries, from the chart shapes it lies inside
(mesh.build.rules), and which cell speaks (mesh.build.cell_index).

`rules` gets, per state of the cut, the shapes of the winning chart the
state is inside: (layer, properties, rank, order). What is pinned:
  * depth is the shallowest depth area's DRVAL1; a dredged area's depth
    replaces it; an obstruction area's depth lowers it;
  * of two dredged areas, or two obstruction areas, the shallower counts
    (the owner's decision of 2026-11-24; the order in the file plays no
    part);
  * a hazard area with no depth is a hazard of unknown depth and leaves
    the depth alone;
  * land, structure, fairway, dredged, navigable, clearance, opening;
  * a state no chart speaks for is "no chart", nothing else;
  * the cell index: of the cells given, those with a skin-of-the-earth
    area, most detailed band first, a new-scheme cell of band 1 or 2
    before a legacy cell of its band; a cell of no chart band and a cell
    across the date line are refused;
  * which tiles are built: those the cells reach.
"""
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h

try:
    from mesh import build as B
    HAVE = True
except ImportError:          # numpy / shapely / scipy missing
    HAVE = False


def row(*shapes):
    """The attribute row of one state lying inside the given (layer, properties) shapes, all of one chart."""
    r = B.rules([list(range(len(shapes)))], [(L, pr, 4, k) for k, (L, pr) in enumerate(shapes)])[0]
    return dict(zip(B.COLS, r.tolist()))


@unittest.skipUnless(HAVE, "mesh packages not installed")
class Rules(unittest.TestCase):
    def test_depth_is_the_shallowest_depth_area(self):
        r = row(("DEPARE", {"DRVAL1": 9.1, "DRVAL2": 18.2}), ("DEPARE", {"DRVAL1": 5.4}))
        self.assertEqual(r["depth"], 5.4)
        self.assertEqual((r["land"], r["haz"], r["dredged"], r["is_nav"], r["nochart"]), (0, 0, 0, 0, 0))

    def test_a_depth_area_without_a_value_gives_no_depth(self):
        self.assertEqual(row(("DEPARE", {}))["depth"], -999.0)
        self.assertEqual(row(("UNSARE", {}))["depth"], -999.0)

    def test_dredged_depth_replaces_the_depth_area(self):
        r = row(("DEPARE", {"DRVAL1": 3.0}), ("DRGARE", {"DRVAL1": 6.0}))
        self.assertEqual(r["depth"], 6.0)
        self.assertEqual((r["dredged"], r["is_nav"]), (1, 1))

    def test_two_dredged_areas_the_shallower(self):
        for a, b in ((7.0, 5.0), (5.0, 7.0)):          # either order in the file
            self.assertEqual(row(("DRGARE", {"DRVAL1": a}), ("DRGARE", {"DRVAL1": b}))["depth"], 5.0)

    def test_two_obstruction_areas_the_shallower(self):
        for a, b in ((4.0, 2.0), (2.0, 4.0)):
            r = row(("DEPARE", {"DRVAL1": 10.0}), ("OBSTRN", {"VALSOU": a}), ("WRECKS", {"VALSOU": b}))
            self.assertEqual((r["depth"], r["haz"], r["hazv"]), (2.0, 1, 2.0))

    def test_an_obstruction_deeper_than_the_water_does_not_deepen_it(self):
        r = row(("DEPARE", {"DRVAL1": 3.0}), ("OBSTRN", {"VALSOU": 8.0}))
        self.assertEqual((r["depth"], r["hazv"]), (3.0, 8.0))

    def test_hazard_of_unknown_depth(self):
        r = row(("DEPARE", {"DRVAL1": 10.0}), ("UWTROC", {}))
        self.assertEqual((r["depth"], r["haz"], r["hazv"]), (10.0, 1, -1e9))

    def test_land_structure_fairway_track(self):
        self.assertEqual(row(("LNDARE", {}))["land"], 1)
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("SLCONS", {}))
        self.assertEqual((r["struct"], r["land"]), (1, 0))
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("FAIRWY", {}))
        self.assertEqual((r["fair"], r["is_nav"]), (1, 1))
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("RECTRC", {}))
        self.assertEqual((r["fair"], r["is_nav"]), (0, 1))
        self.assertEqual(row(("PONTON", {}))["struct"], 1)

    def test_water_needs_a_water_area(self):
        # a pontoon, hulk or floating dock is charted instead of a depth area, not on top of one: on its own it
        # is not water; with a depth, dredged or unsurveyed area under it, it is water with a structure on it
        for layer in ("PONTON", "HULKES", "FLODOC"):
            self.assertEqual(row((layer, {}))["nowater"], 1, layer)
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("PONTON", {}))
        self.assertEqual((r["nowater"], r["struct"], r["depth"]), (0, 1, 4.0))
        for layer in ("DEPARE", "DRGARE", "UNSARE"):
            self.assertEqual(row((layer, {}))["nowater"], 0, layer)

    def test_clearance_is_the_lowest_and_opening_is_kept(self):
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("BRIDGE", {"CATBRG": "1", "VERCLR": "20"}), ("CBLOHD", {"VERCLR": "12"}))
        self.assertEqual((r["clear"], r["opening"]), (12.0, 0))
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("BRIDGE", {"CATBRG": "4", "VERCCL": "7.3"}))
        self.assertEqual((r["clear"], r["opening"]), (-999.0, 1))
        self.assertEqual(row(("DEPARE", {"DRVAL1": 4.0}))["clear"], -999.0)

    def test_no_chart(self):
        r = dict(zip(B.COLS, B.rules([None], [])[0].tolist()))
        self.assertEqual((r["nochart"], r["land"], r["depth"], r["hazv"]), (1, 0, -999.0, 1e9))
        self.assertEqual(r["nowater"], 0, "no chart is its own reason; 'no water area' is said of a chart that speaks")


def square(box=(0.0, 0.0, 1.0, 1.0)):
    w, s, e, n = box
    return [{"type": "Feature", "properties": {"DRVAL1": 5.0},
             "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}]


@unittest.skipUnless(HAVE, "mesh packages not installed")
class CellIndex(unittest.TestCase):
    """Which of the cells given the tile stage reads, and in what order."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s57cells-")
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, band, cell, layers, source="A.zip"):
        """A record as mesh/cells.py gives it: (band, name, source, {layer: file})."""
        band, cell, files = h.shapes_cell(self.folder, band, cell, layers)
        return (band, cell, source, files)

    def test_order_rank_and_what_is_left_out(self):
        records = [
            self.record("band5", "US5XX1AA", {"DEPARE": square((0.0, 0.0, 0.5, 0.5))}),
            self.record("band4", "US4XX1AA", {"DEPARE": square()}),
            self.record("band2", "US2EC03M", {"DEPARE": square()}),                  # legacy name
            self.record("band2", "US2ATLPF", {"DEPARE": square()}),                  # new scheme (ATL)
            self.record("band3", "US3XX1AA", {"M_COVR": square()}),   # no skin-of-the-earth area: not a chart that speaks
            self.record("band5", "US5YY1AA", {"DEPARE": square()}, source="B.zip"),
        ]
        cells = B.cell_index(records, 1)
        self.assertEqual([c[2] for c in cells], ["US5XX1AA", "US5YY1AA", "US4XX1AA", "US2ATLPF", "US2EC03M"])
        ranks = {c[2]: c[0] for c in cells}
        self.assertEqual(ranks["US5XX1AA"], ranks["US5YY1AA"], "one band, one rank, whichever input")
        self.assertLess(ranks["US2ATLPF"], ranks["US2EC03M"], "new scheme wins within band 2")
        own4 = next(c for c in cells if c[2] == "US4XX1AA")
        self.assertEqual((own4[3], own4[4]), ("A.zip", [0.0, 0.0, 1.0, 1.0]))
        self.assertEqual({c[2]: c[3] for c in cells}["US5YY1AA"], "B.zip")
        self.assertEqual(next(c for c in cells if c[2] == "US5XX1AA")[4], [0.0, 0.0, 0.5, 0.5])
        self.assertEqual(own4[5], records[1][3], "the files given")

    def test_a_cell_this_build_does_not_read_is_refused(self):
        for band, cell, box, said in (("band7", "US7XX1AA", (0.0, 0.0, 1.0, 1.0), "is not a chart band"),
                                      ("band4", "US4XX1AB", (179.0, 0.0, 181.0, 1.0), "beyond 180 degrees"),
                                      ("band4", "US4XX1AC", (-170.0, 0.0, 170.0, 1.0), "across the date line")):
            with self.assertRaises(RuntimeError) as cm:
                B.cell_index([self.record(band, cell, {"DEPARE": square(box)})], 1)
            self.assertIn(said, str(cm.exception))
            self.assertIn(cell, str(cm.exception))

    def test_the_tiles_built_are_those_the_cells_reach(self):
        cells = B.cell_index([self.record("band4", "US4XX1AA", {"DEPARE": square((-71.6, 41.05, -71.4, 41.2))}),
                              self.record("band5", "US5XX1AB", {"DEPARE": square((-70.05, 41.05, -70.01, 41.1))}),
                              self.record("band5", "US5XX1AC", {"DEPARE": square((-69.2, 41.05, -69.0, 41.1))})], 1)
        # columns count from -180, rows from -90, in quarter degrees. The first cell lies across the tile edge at
        # -71.5 (tiles 433 and 434); the second inside -70.25..-70.0 (439); the third ends exactly on the tile edge
        # at -69.0, and a cell that touches a tile's edge reaches that tile too (443 and 444).
        self.assertEqual(B.tiles_reached(cells), [(433, 524), (434, 524), (439, 524), (443, 524), (444, 524)])


if __name__ == "__main__":
    unittest.main()
