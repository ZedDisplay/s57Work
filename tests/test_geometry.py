"""Geometry and tile arithmetic: clip-debris removal after the erase,
bbox tests used to find overlapping cells, tile bounds and the district
region mask that keeps ocean-basin overview cells out of district files."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()

SQ = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
SQ2 = [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]


def feature(geom, prim=None):
    props = {} if prim is None else {"PRIM": prim}
    return {"type": "Feature", "properties": props, "geometry": geom}


class KeepOwnDimension(unittest.TestCase):
    def test_polygon_keeps_polygon_and_drops_edge_debris(self):
        f = feature({"type": "GeometryCollection", "geometries": [
            {"type": "Polygon", "coordinates": SQ},
            {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
            {"type": "Point", "coordinates": [0, 0]}]}, prim=3)
        self.assertTrue(s57._keep_own_dimension(f))
        self.assertEqual(f["geometry"]["type"], "Polygon")

    def test_several_parts_fold_into_multi(self):
        f = feature({"type": "GeometryCollection", "geometries": [
            {"type": "Polygon", "coordinates": SQ},
            {"type": "MultiPolygon", "coordinates": [SQ2]}]}, prim=3)
        self.assertTrue(s57._keep_own_dimension(f))
        self.assertEqual(f["geometry"]["type"], "MultiPolygon")
        self.assertEqual(len(f["geometry"]["coordinates"]), 2)

    def test_only_debris_left_means_drop(self):
        f = feature({"type": "LineString", "coordinates": [[0, 0], [1, 0]]}, prim=3)
        self.assertFalse(s57._keep_own_dimension(f))
        self.assertFalse(s57._keep_own_dimension(feature(None, prim=1)))

    def test_without_prim_highest_dimension_wins(self):
        f = feature({"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}]})
        self.assertTrue(s57._keep_own_dimension(f))
        self.assertEqual(f["geometry"]["type"], "LineString")

    def test_plain_geometry_passes_through(self):
        f = feature({"type": "Point", "coordinates": [0, 0]}, prim=1)
        self.assertTrue(s57._keep_own_dimension(f))
        self.assertEqual(f["geometry"]["type"], "Point")


class Bboxes(unittest.TestCase):
    def test_intersects_is_strict(self):
        self.assertTrue(s57._bbox_intersects((0, 0, 2, 2), (1, 1, 3, 3)))
        self.assertFalse(s57._bbox_intersects((0, 0, 1, 1), (1, 0, 2, 1)))  # shared edge
        self.assertFalse(s57._bbox_intersects((0, 0, 1, 1), (5, 5, 6, 6)))

    def test_candidates_exclude_grid_edge_sharing(self):
        boxes = {"A": (0, 0, 1, 1), "B": (1, 0, 2, 1), "C": (0.5, 0.5, 1.5, 1.5)}
        self.assertEqual(s57._bbox_candidates(boxes), [("A", "C"), ("B", "C")])

    def test_geom_bbox_walks_nested_coordinates(self):
        feats = [feature({"type": "MultiPolygon", "coordinates": [SQ, SQ2]})]
        self.assertEqual(s57._geom_bbox(feats), (0, 0, 3, 1))


class TileMath(unittest.TestCase):
    def test_world_tile_bounds(self):
        w, s, e, n = s57._tile_lonlat_bounds(0, 0, 0)
        self.assertEqual((w, e), (-180.0, 180.0))
        self.assertAlmostEqual(n, 85.0511, places=3)
        self.assertAlmostEqual(s, -85.0511, places=3)

    def test_tms_row_order(self):
        # z1: TMS row 1 is the northern half.
        _, s, _, n = s57._tile_lonlat_bounds(1, 0, 1)
        self.assertAlmostEqual(s, 0.0, places=9)
        self.assertGreater(n, 80)

    def test_counter_and_pipeline_agree_on_tile_addressing(self):
        from tests.helpers import counter
        c = counter()
        z = 13
        x, y = c.lonlat_to_tile(-71.58, 41.17, z)          # XYZ
        w, s, e, n = s57._tile_lonlat_bounds(z, x, (1 << z) - 1 - y)  # TMS
        self.assertTrue(w <= -71.58 <= e and s <= 41.17 <= n)


def make_mbtiles(path, coords):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE tiles (zoom_level int, tile_column int, "
               "tile_row int, tile_data blob)")
    db.execute("CREATE TABLE metadata (name text, value text)")
    db.executemany("INSERT INTO tiles VALUES (?,?,?,?)",
                   [(z, x, y, b"\x00") for z, x, y in coords])
    db.commit()
    return db


class RegionMask(unittest.TestCase):
    def test_mask_is_dilated_parent_of_detail_tiles(self):
        p = Path(tempfile.mkdtemp()) / "band4.mbtiles"
        make_mbtiles(p, [(13, 4936, 5100), (14, 9872, 10200)]).close()
        mask = s57._region_mask([p])
        parent = (4936 >> 2, 5100 >> 2)
        self.assertIn(parent, mask)
        self.assertIn((parent[0] + 1, parent[1] - 1), mask)
        self.assertEqual(len(mask), 9)

    def test_files_below_mask_zoom_are_ignored(self):
        p = Path(tempfile.mkdtemp()) / "band2.mbtiles"
        make_mbtiles(p, [(9, 300, 380)]).close()
        self.assertEqual(s57._region_mask([p]), set())

    def test_file_starting_above_mask_zoom_is_read_at_mask_zoom(self):
        # Band 3 at z9-12 in one run (a district with band 1 but no band 2,
        # so nothing erases its downward zooms).
        p = Path(tempfile.mkdtemp()) / "band3.mbtiles"
        make_mbtiles(p, [(9, 146, 307), (10, 292, 614), (11, 585, 1229),
                         (12, 1170, 2458)]).close()
        mask = s57._region_mask([p])
        self.assertIn((585, 1229), mask)
        self.assertEqual(len(mask), 9)

    def test_recompute_bounds_uses_deepest_zoom(self):
        p = Path(tempfile.mkdtemp()) / "t.mbtiles"
        db = make_mbtiles(p, [(0, 0, 0), (2, 1, 2)])
        w, s, e, n = (float(v) for v in s57._recompute_bounds(db).split(","))
        self.assertEqual((w, e), (-90.0, 0.0))
        self.assertGreater(n, 66)
        self.assertAlmostEqual(s, 0.0, places=9)
        db.close()


if __name__ == "__main__":
    unittest.main()
