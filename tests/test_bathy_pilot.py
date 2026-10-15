"""bathy.pilot pieces that run without credentials: the band ratio, the
robust fit, the per-band error statistics, and the ATL24 reader against a
synthetic granule with the data dictionary's layout (/gt1l/class_ph ...).
Skips without numpy, scipy and h5py."""
import os
import tempfile
import unittest

try:
    import numpy as np
    import scipy  # noqa: F401
    import h5py
    from bathy import pilot as P
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "needs numpy, scipy and h5py (bathy/requirements.txt)")
class Pilot(unittest.TestCase):
    def test_ratio_is_nan_where_a_log_is_not_positive(self):
        b2 = np.array([0.05, 0.0005, 0.02, 0.05])
        b3 = np.array([0.04, 0.04, 0.0009, 0.001])
        r = P.ratio_of(b2, b3)
        self.assertAlmostEqual(r[0], np.log(50) / np.log(40))
        self.assertTrue(np.isnan(r[1]) and np.isnan(r[2]))
        self.assertTrue(np.isnan(r[3]), "ln(1000 * 0.001) = 0 must not divide")

    def test_robust_line_recovers_the_slope_through_outliers(self):
        rng = np.random.default_rng(1)
        x = rng.uniform(1.0, 1.2, 2000)
        y = 60.0 * x - 58.0 + rng.normal(0, 0.3, x.size)
        y[:100] += 15.0                                   # 5 % gross outliers
        m1, m0 = P.robust_line(x, y)
        self.assertAlmostEqual(m1, 60.0, delta=1.5)
        self.assertAlmostEqual(m0, 58.0, delta=1.5)

    def test_band_stats_counts_too_deep_and_passes(self):
        truth = np.r_[np.full(100, 1.0), np.full(100, 7.0), np.full(10, 15.0)]
        sdb = truth.copy()
        sdb[:4] += 1.5                                    # 4 % too deep in 0-2: passes
        sdb[100:110] += 1.5                               # 10 % too deep in 5-10: fails
        rows = {r["band"]: r for r in P.band_stats(truth, sdb)}
        self.assertEqual(rows["0-2 m"]["n"], 100)
        self.assertAlmostEqual(rows["0-2 m"]["share_too_deep"], 0.04)
        self.assertTrue(rows["0-2 m"]["pass"])
        self.assertFalse(rows["5-10 m"]["pass"])
        self.assertIsNone(rows["2-5 m"]["pass"])         # no points
        self.assertFalse(rows["10-20 m"]["pass"], "fewer than 30 points cannot pass")

    def test_shallower_sdb_is_not_too_deep(self):
        truth = np.full(50, 6.0)
        rows = {r["band"]: r for r in P.band_stats(truth, truth - 2.0)}
        self.assertEqual(rows["5-10 m"]["share_too_deep"], 0.0)

    def test_atl24_reader_keeps_confident_bathymetry_inside_the_box(self):
        box = (-65.0, 18.0, -64.0, 19.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ATL24_test.h5")
            with h5py.File(path, "w") as f:
                g = f.create_group("gt2l")
                n = 6
                g["class_ph"] = np.array([40, 41, 40, 40, 40, 40], dtype=np.int8)
                g["lon_ph"] = np.array([-64.5, -64.5, -64.5, -64.5, -63.5, -64.5])
                g["lat_ph"] = np.full(n, 18.4)
                g["low_confidence_flag"] = np.array([0, 0, 1, 0, 0, 0], dtype=np.uint8)
                g["sensor_depth_exceeded"] = np.array([0, 0, 0, 0, 0, 1], dtype=np.uint8)
                g["confidence"] = np.array([0.9, 0.9, 0.9, 0.5, 0.9, 0.9])
                g["surface_h"] = np.full(n, -40.0, dtype=np.float32)
                g["ortho_h"] = np.array([-47.5, -40.0, -45.0, -44.0, -50.0, -60.0], dtype=np.float32)
                g["delta_time"] = np.arange(n, dtype=float)
                g["sigma_tvu"] = np.full(n, 0.3, dtype=np.float32)
                g["night_flag"] = np.zeros(n, dtype=np.uint8)
            a = P.read_atl24(path, box, min_conf=0.6)
        # kept: photon 0 only (1 is sea surface, 2 low confidence, 3 below
        # confidence, 4 outside the box, 5 beyond the sensor depth)
        self.assertEqual(a.shape, (1, 6))
        self.assertAlmostEqual(a[0, 2], 7.5, places=4)    # depth = surface_h - ortho_h

    def test_pass_ids_split_on_time_gaps(self):
        t = np.array([10.0, 0.0, 0.5, 500.0, 501.0, 10000.0])
        self.assertEqual(P.pass_ids(t).tolist(), [0, 0, 0, 1, 1, 2])   # 10 s is within 60 s of 0.5 s

    def test_switch_model_uses_the_shallow_line_shallow_and_the_deep_line_deep(self):
        rng = np.random.default_rng(3)
        r = rng.uniform(1.0, 1.4, 6000)
        # truth: shallow slope 30, deep slope 60, joined near 5 m
        d = np.where(30 * r - 28 < 5, 30 * r - 28, 60 * r - 61.5)
        keep = (d > 0) & (d < 25)
        m = P.fit_model(r[keep], d[keep], "switch")
        self.assertAlmostEqual(m["shallow"][0], 30, delta=2)
        self.assertAlmostEqual(m["deep"][0], 60, delta=3)
        rr = np.array([1.0, 1.35])                       # 2 m and about 19.5 m
        pred = P.predict_model(m, rr)
        self.assertAlmostEqual(pred[0], 2.0, delta=0.3)
        self.assertAlmostEqual(pred[1], 60 * 1.35 - 61.5, delta=0.6)

    def test_fit_model_refuses_too_few_points(self):
        self.assertIsNone(P.fit_model(np.ones(10), np.ones(10), "linear"))
        self.assertIsNone(P.fit_model(np.ones(100), np.full(100, 10.0), "switch"))   # no shallow points

    def test_band_stats_shoal_shift_brings_the_band_to_the_limit(self):
        truth = np.full(1000, 7.0)
        sdb = truth + np.linspace(-1.0, 3.0, 1000)
        row = {r["band"]: r for r in P.band_stats(truth, sdb)}["5-10 m"]
        shifted = {r["band"]: r for r in P.band_stats(truth, sdb - row["shoal_shift_m"])}["5-10 m"]
        self.assertLessEqual(shifted["share_too_deep"], 0.05 + 1e-9)


if __name__ == "__main__":
    unittest.main()
