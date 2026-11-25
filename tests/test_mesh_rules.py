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
  * the cell index: the cells are those a folder's list (cells.json)
    names, with the files it names, and nothing else in the folder; most
    detailed band first, a new-scheme cell of band 1 or 2 before a legacy
    cell of its band, a cell present in two folders read from the first;
    a folder with no list, a listed file that is missing, and a list this
    build cannot read are refused.
"""
import json
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
    """The cells come from each folder's list (cells.json) and from nothing else in the folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s57cells-")
        self.own = h.CellsFolder(Path(self.tmp.name) / "own")
        self.other = h.CellsFolder(Path(self.tmp.name) / "other")
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_and_first_folder(self):
        self.own.add("band5", "US5XX1AA", {"DEPARE": square((0.0, 0.0, 0.5, 0.5))})
        self.own.add("band4", "US4XX1AA", {"DEPARE": square()})
        self.own.add("band2", "US2EC03M", {"DEPARE": square()})                  # legacy name
        self.own.add("band2", "US2ATLPF", {"DEPARE": square()})                  # new scheme (ATL)
        self.own.add("band3", "US3XX1AA", {"M_COVR": square()})   # no skin-of-the-earth area: not a chart that speaks
        self.other.add("band5", "US5YY1AA", {"DEPARE": square()})
        self.other.add("band4", "US4XX1AA", {"DEPARE": square((5.0, 5.0, 6.0, 6.0))})    # also in the first folder
        a, b = self.own.write(), self.other.write()
        cells = B.cell_index([a, b], str(self.work), 1)
        self.assertEqual([c[2] for c in cells], ["US5XX1AA", "US5YY1AA", "US4XX1AA", "US2ATLPF", "US2EC03M"])
        ranks = {c[2]: c[0] for c in cells}
        self.assertEqual(ranks["US5XX1AA"], ranks["US5YY1AA"], "one band, one rank, whichever folder")
        self.assertLess(ranks["US2ATLPF"], ranks["US2EC03M"], "new scheme wins within band 2")
        own4 = next(c for c in cells if c[2] == "US4XX1AA")
        self.assertEqual((own4[3], own4[4], own4[5]), (a, [0.0, 0.0, 1.0, 1.0], True), "read from the first folder")
        self.assertEqual({c[2]: c[5] for c in cells}["US5YY1AA"], False)
        self.assertEqual(next(c for c in cells if c[2] == "US5XX1AA")[4], [0.0, 0.0, 0.5, 0.5])
        self.assertEqual(own4[6], {"DEPARE": str(Path(a) / "band4" / "DEPARE_US4XX1AA.geojson")}, "the files the list names")

    def test_a_folder_without_a_list_is_refused(self):
        self.own.add("band5", "US5XX1AA", {"DEPARE": square()})        # the files are there, the list is not
        with self.assertRaises(RuntimeError) as cm:
            B.listed_cells(str(self.own.path))
        self.assertIn("no cells.json", str(cm.exception))
        self.assertIn("did not finish", str(cm.exception))

    def test_what_is_not_on_the_list_is_not_read(self):
        self.own.add("band5", "US5XX1AA", {"DEPARE": square()})
        self.own.add("band5", "US5XX1AB", {"DEPARE": square()}, listed=False)
        root = self.own.write()
        (Path(root) / "band5" / "DEPARE.geojson").write_text("not even JSON")       # a stray file
        (Path(root) / "band4").mkdir()
        (Path(root) / "band4" / "DEPARE_US4XX1AA.geojson").write_text("not even JSON")
        self.assertEqual([c[1] for c in B.listed_cells(root)], ["US5XX1AA"])
        self.assertEqual([c[2] for c in B.cell_index([root], str(self.work), 1)], ["US5XX1AA"])

    def test_a_listed_file_that_is_missing_stops_the_build(self):
        self.own.add("band5", "US5XX1AA", {"DEPARE": square(), "SOUNDG": []})
        root = self.own.write()
        (Path(root) / "band5" / "SOUNDG_US5XX1AA.geojson").unlink()
        with self.assertRaises(RuntimeError) as cm:
            B.listed_cells(root)
        self.assertIn("SOUNDG_US5XX1AA.geojson, which is not there", str(cm.exception))

    def test_a_list_this_build_cannot_read_is_refused(self):
        self.own.add("band5", "US5XX1AA", {"DEPARE": square()})
        root = self.own.write()
        doc = json.loads((Path(root) / "cells.json").read_text())
        for change, said in (({"version": 2}, "list version 2"),
                             ({"cells": doc["cells"] * 2}, "listed twice"),
                             ({"cells": [dict(doc["cells"][0], band=7)]}, "not a chart band"),
                             ({"cells": [dict(doc["cells"][0], files={"DEPARE": "../elsewhere/DEPARE_US5XX1AA.geojson"})]},
                              "not inside the folder"),
                             ({"cells": [dict(doc["cells"][0], files={"DEPARE": str(Path(root) / "band5" / "DEPARE_US5XX1AA.geojson")})]},
                              "not inside the folder")):
            (Path(root) / "cells.json").write_text(json.dumps(dict(doc, **change)))
            with self.assertRaises(RuntimeError) as cm:
                B.listed_cells(root)
            self.assertIn(said, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
