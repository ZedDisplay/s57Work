"""count-mesh-triangles.py: the runs of stored longitude a box covers. A
mesh stores longitudes between -180 and 180 and no tile lies across the
date line, so a box or a point's window across it is two runs."""
import unittest

from tests import helpers as h

try:
    cm = h.load_script("count-mesh-triangles.py")
except ImportError:          # numpy missing
    cm = None


@unittest.skipIf(cm is None, "numpy not installed")
class LonRuns(unittest.TestCase):
    def test_an_ordinary_box_is_one_run(self):
        self.assertEqual(cm.lon_runs(-71.6, -71.4), [(-71.6, -71.4)])
        self.assertEqual(cm.lon_runs(170.0, 180.0), [(170.0, 180.0)])
        self.assertEqual(cm.lon_runs(-180.0, -170.0), [(-180.0, -170.0)])

    def test_a_box_across_the_date_line_is_two(self):
        self.assertEqual(cm.lon_runs(179.75, -179.75), [(179.75, 180.0), (-180.0, -179.75)])

    def test_a_window_round_a_point_near_the_line(self):
        (w0, e0), (w1, e1) = cm.lon_runs(179.99 - 0.05, 179.99 + 0.05)
        self.assertAlmostEqual(w0, 179.94); self.assertEqual((e0, w1), (180.0, -180.0)); self.assertAlmostEqual(e1, -179.96)
        (w0, e0), (w1, e1) = cm.lon_runs(-179.99 - 0.05, -179.99 + 0.05)
        self.assertAlmostEqual(w0, 179.96); self.assertEqual((e0, w1), (180.0, -180.0)); self.assertAlmostEqual(e1, -179.94)

    def test_the_whole_turn(self):
        self.assertEqual(cm.lon_runs(-180.0, 180.0), [(-180.0, 180.0)])


if __name__ == "__main__":
    unittest.main()
