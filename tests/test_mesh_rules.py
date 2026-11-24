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
  * the cell index: most detailed band first, a new-scheme cell of band 1
    or 2 before a legacy cell of its band, a cancelled cell left out, a
    cell present in two folders read from the first.
"""
import json
import tempfile
import unittest
from pathlib import Path

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

    def test_clearance_is_the_lowest_and_opening_is_kept(self):
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("BRIDGE", {"CATBRG": "1", "VERCLR": "20"}), ("CBLOHD", {"VERCLR": "12"}))
        self.assertEqual((r["clear"], r["opening"]), (12.0, 0))
        r = row(("DEPARE", {"DRVAL1": 4.0}), ("BRIDGE", {"CATBRG": "4", "VERCCL": "7.3"}))
        self.assertEqual((r["clear"], r["opening"]), (-999.0, 1))
        self.assertEqual(row(("DEPARE", {"DRVAL1": 4.0}))["clear"], -999.0)

    def test_no_chart(self):
        r = dict(zip(B.COLS, B.rules([None], [])[0].tolist()))
        self.assertEqual((r["nochart"], r["land"], r["depth"], r["hazv"]), (1, 0, -999.0, 1e9))


def write_cell(root, band, name, layer="DEPARE", box=(0.0, 0.0, 1.0, 1.0)):
    """A cell with one skin-of-the-earth area, in the layout s57-to-mbtiles.py exports."""
    w, s, e, n = box
    d = Path(root) / "geojson" / band
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{layer}_{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"DRVAL1": 5.0},
         "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}]}))
    (d / f".{name}.exported").write_text("1.0\n")
    return str(Path(root) / "geojson")


@unittest.skipUnless(HAVE, "mesh packages not installed")
class CellIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s57cells-")
        self.own = Path(self.tmp.name) / "own"
        self.other = Path(self.tmp.name) / "other"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_cancelled_and_first_folder(self):
        a = write_cell(self.own, "band5", "US5XX1AA", box=(0.0, 0.0, 0.5, 0.5))
        write_cell(self.own, "band4", "US4XX1AA")
        write_cell(self.own, "band2", "US2EC03M")                  # legacy name
        write_cell(self.own, "band2", "US2ATLPF")                  # new scheme (ATL)
        write_cell(self.own, "band5", "US5XX9ZZ")                  # cancelled, still exported
        write_cell(self.own, "band3", "US3XX1AA", layer="M_COVR")  # no skin-of-the-earth area: not a chart that speaks
        (self.own / "cancelled-cells.json").write_text(json.dumps({"cancelled": [{"cell": "US5XX9ZZ"}]}))
        b = write_cell(self.other, "band5", "US5YY1AA")
        write_cell(self.other, "band4", "US4XX1AA", box=(5.0, 5.0, 6.0, 6.0))    # also in the first folder
        cells = B.cell_index([a, b], str(self.work), 1)
        self.assertEqual([c[2] for c in cells], ["US5XX1AA", "US5YY1AA", "US4XX1AA", "US2ATLPF", "US2EC03M"])
        ranks = {c[2]: c[0] for c in cells}
        self.assertEqual(ranks["US5XX1AA"], ranks["US5YY1AA"], "one band, one rank, whichever folder")
        self.assertLess(ranks["US2ATLPF"], ranks["US2EC03M"], "new scheme wins within band 2")
        own4 = next(c for c in cells if c[2] == "US4XX1AA")
        self.assertEqual((own4[3], own4[4], own4[5]), (a, [0.0, 0.0, 1.0, 1.0], True), "read from the first folder")
        self.assertEqual({c[2]: c[5] for c in cells}["US5YY1AA"], False)
        self.assertEqual(next(c for c in cells if c[2] == "US5XX1AA")[4], [0.0, 0.0, 0.5, 0.5])

    def test_a_band_with_one_cell_has_files_without_the_cell_name(self):
        # as the chart build exports it: DEPARE.geojson, and the cell's name only on its completion marker
        root = write_cell(self.own, "band4", "US4XX1AA")
        band = Path(root) / "band4"
        (band / "DEPARE_US4XX1AA.geojson").rename(band / "DEPARE.geojson")
        self.assertEqual(B.band_cells(root, "band4"), {"US4XX1AA": ""})
        cells = B.cell_index([root], str(self.work), 1)
        self.assertEqual([(c[2], c[4], c[6]) for c in cells], [("US4XX1AA", [0.0, 0.0, 1.0, 1.0], "")])

    def test_an_unfinished_export_stops_the_build(self):
        root = write_cell(self.own, "band5", "US5XX1AA")
        write_cell(self.own, "band5", "US5XX1AB")
        (Path(root) / "band5" / ".US5XX1AB.exported").unlink()
        with self.assertRaises(RuntimeError) as cm:
            B.band_cells(root, "band5")
        self.assertIn("US5XX1AB", str(cm.exception))

    def test_unnamed_files_beside_named_cells_stop_the_build(self):
        root = write_cell(self.own, "band5", "US5XX1AA")
        write_cell(self.own, "band5", "US5XX1AB")
        (Path(root) / "band5" / "DEPARE.geojson").write_text('{"type": "FeatureCollection", "features": []}')
        with self.assertRaises(RuntimeError):
            B.band_cells(root, "band5")

    def test_an_empty_or_missing_band_has_no_cells(self):
        root = write_cell(self.own, "band5", "US5XX1AA")
        self.assertEqual(B.band_cells(root, "band3"), {})


if __name__ == "__main__":
    unittest.main()
