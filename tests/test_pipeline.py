"""End-to-end: the by-band pipeline on a five-cell fixture district
(tests/fixtures/mini-district.zip: band 4 cell US4NY1BY, band 5 cells
US5RI1AC/AD/AE nested inside it, cancelled cell US5NJ30M), then the
tiles are decoded and checked per zoom. Needs GDAL and tippecanoe; skips
without them.

What is pinned here, and why:
  * the cancelled cell is dropped (it would otherwise render on top of
    its replacement);
  * SOUNDG, and every other point layer, is present at every zoom the
    band renders — the odd-zoom hole of 2026-09-07 — with the bottom
    zoom thinned to about one in 2.5 and the top zoom untouched;
  * finer wins: inside a band 5 cell at z15-16 exactly one chart's
    M_COVR is in the tile and no feature is duplicated; outside the band
    5 cells the band 4 extension still provides the chart;
  * a second run rebuilds nothing;
  * with go-pmtiles installed, the --pmtiles archive holds the same tiles
    and metadata as the MBTiles it mirrors.
"""
import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tests import helpers as h

BAND4_BOX = (-71.7, 40.8, -71.4, 41.1)
BAND5_AD_BOX = (-71.625, 41.025, -71.55, 41.1)  # US5RI1AD, wholly inside band 4
BAND5_AD_CENTER = (-71.5875, 41.0625)
BAND4_ONLY_POINT = (-71.55, 40.9)                 # inside US4NY1BY, no band 5


@unittest.skipUnless(h.HAVE_GDAL and h.HAVE_TIPPECANOE,
                     "needs ogr2ogr/ogrinfo and tippecanoe/tile-join")
class ByBandPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="s57test-"))
        cls.zip = cls.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, cls.zip)
        cls.args = (str(cls.zip), "--by-band", "--no-replacements", "-j", "2",
                    "-o", "mini.mbtiles")
        if h.HAVE_PMTILES:
            cls.args += ("--pmtiles",)
        cls.first = h.run_pipeline(cls.work, *cls.args)
        cls.out = cls.work / "data" / "tiles" / "mini.mbtiles"
        if cls.first.returncode != 0 or not cls.out.exists():
            raise AssertionError("pipeline failed:\n" + cls.first.stdout[-3000:]
                                 + cls.first.stderr[-3000:])
        cls.c = h.counter()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def counts(self, layer, bbox, zooms):
        return self.c.measure(self.out, [layer], bbox, zooms)[layer]

    # -- inventory ---------------------------------------------------------

    def test_cancelled_cell_is_dropped(self):
        rec = json.loads((self.work / "data" / "cancelled-cells.json").read_text())
        self.assertEqual([c["cell"] for c in rec["cancelled"]], [h.FIXTURE_CANCELLED])
        self.assertIn(f"Excluding 1 cancelled cell(s)", self.first.stdout)
        w = float(h.metadata(self.out)["bounds"].split(",")[0])
        self.assertGreater(w, -72.0, "New Jersey cell leaked into the tiles")

    def test_zoom_range_is_band4_downward_floor_to_maxzoom(self):
        # No band 3 in the fixture: band 4 also renders its
        # BAND_ZOOM_DOWNWARD zooms (z11-12) below its native z13.
        meta = h.metadata(self.out)
        self.assertEqual((meta["minzoom"], meta["maxzoom"]), ("11", "16"))
        self.assertEqual(meta["type"], "S-57")

    def test_scale_is_the_finest_cell_compilation_scale(self):
        # US4NY1BY 1:45,000; US5RI1AC/AD/AE 1:22,000; the cancelled
        # US5NJ30M (1:40,000) is not in the inventory.
        self.assertEqual(h.metadata(self.out)["scale"], "22000")
        self.assertIn("Bundle scale (finest cell compilation scale): 1:22,000",
                      self.first.stdout)

    @unittest.skipUnless(h.HAVE_PMTILES, "needs go-pmtiles")
    def test_pmtiles_copy_carries_the_same_metadata(self):
        arch = self.out.with_suffix(".pmtiles")
        self.assertTrue(arch.exists())
        meta = h.pmtiles_metadata(arch)
        self.assertEqual(str(meta.get("scale")), "22000")
        self.assertEqual(meta.get("type"), "S-57")

    def test_render_plan_erases_band4_under_band5(self):
        self.assertIn("band4-approach_z15-16.minus-band5-harbour", self.first.stdout)
        self.assertNotIn("Traceback", self.first.stderr)

    # -- zoom presence (the odd-zoom regression) --------------------------

    def test_soundings_present_at_every_zoom(self):
        r = self.counts("SOUNDG", BAND5_AD_BOX, [13, 14, 15, 16])
        for z in (13, 14, 15, 16):
            self.assertGreater(r[z]["features"], 0, f"no SOUNDG at z{z}")
        # Bottom zoom of each band thinned to ~1 in 2.5 (tile buffers
        # add a little); top zoom carries everything.
        self.assertTrue(0.25 <= r[13]["features"] / r[14]["features"] <= 0.7,
                        (r[13]["features"], r[14]["features"]))
        self.assertTrue(0.25 <= r[15]["features"] / r[16]["features"] <= 0.7,
                        (r[15]["features"], r[16]["features"]))
        self.assertLessEqual(r[13]["max_per_tile"], 2 * r[14]["max_per_tile"])

    def test_point_layers_present_wherever_band_renders(self):
        for layer in ("LIGHTS", "BOYLAT", "BCNLAT", "WRECKS", "OBSTRN", "UWTROC"):
            r = self.counts(layer, BAND4_BOX, [13, 14, 15, 16])
            if r[14]["features"] == 0:
                continue  # layer absent from the fixture cells
            for z in (13, 15, 16):
                self.assertGreater(r[z]["features"], 0, f"{layer} missing at z{z}")

    def test_soundings_unthinned_under_band4_extension(self):
        # Outside band 5, z15-16 come from band 4's extension with every
        # sounding present: z15 (four z16 tiles' worth) >= z16 in a box.
        box = (BAND4_ONLY_POINT[0] - 0.03, BAND4_ONLY_POINT[1] - 0.03,
               BAND4_ONLY_POINT[0] + 0.03, BAND4_ONLY_POINT[1] + 0.03)
        r = self.counts("SOUNDG", box, [14, 15, 16])
        self.assertGreater(r[14]["features"], 0)
        self.assertGreaterEqual(r[15]["features"], 0.8 * r[16]["features"])

    # -- finer wins --------------------------------------------------------

    def _tile_at(self, lon, lat, z):
        x, y = self.c.lonlat_to_tile(lon, lat, z)
        return h.decode_tile(self.out, z, x, y)

    def test_one_chart_per_tile_inside_band5(self):
        for z in (15, 16):
            layers = self._tile_at(*BAND5_AD_CENTER, z)
            covr = [f for f in layers.get("M_COVR", [])
                    if f["properties"].get("CATCOV") == 1]
            self.assertEqual(len(covr), 1, f"z{z}: {len(covr)} charts stacked")
            seen = Counter((name, f["properties"].get("LNAM"),
                            json.dumps(f["geometry"], sort_keys=True))
                           for name, feats in layers.items() for f in feats)
            dupes = [k for k, n in seen.items() if n > 1]
            self.assertEqual(dupes, [], f"z{z}: duplicated features {dupes[:3]}")

    def test_band4_extension_fills_where_band5_is_absent(self):
        for z in (15, 16):
            layers = self._tile_at(*BAND4_ONLY_POINT, z)
            self.assertGreater(len(layers.get("DEPARE", [])), 0, f"z{z} blank")
            covr = [f for f in layers.get("M_COVR", [])
                    if f["properties"].get("CATCOV") == 1]
            self.assertEqual(len(covr), 1)

    # -- downward fill (BAND_ZOOM_DOWNWARD) ----------------------------------

    def test_band4_fills_the_missing_band3_zooms(self):
        # The fixture has no band 3, so band 4 alone draws z11-12: one
        # chart per tile, and no soundings (they start at the native floor).
        for z in (11, 12):
            layers = self._tile_at(*BAND4_ONLY_POINT, z)
            self.assertGreater(len(layers.get("DEPARE", [])), 0, f"z{z} blank")
            covr = [f for f in layers.get("M_COVR", [])
                    if f["properties"].get("CATCOV") == 1]
            self.assertEqual(len(covr), 1, f"z{z}: {len(covr)} charts stacked")
        r = self.counts("SOUNDG", BAND4_BOX, [11, 12])
        self.assertEqual((r[11]["features"], r[12]["features"]), (0, 0))

    def test_band5_downward_zooms_lose_to_native_band4(self):
        # Band 5's z13-14 lie wholly inside band 4's native coverage, so
        # the native band 4 is the only chart there.
        for z in (13, 14):
            layers = self._tile_at(*BAND5_AD_CENTER, z)
            covr = [f for f in layers.get("M_COVR", [])
                    if f["properties"].get("CATCOV") == 1]
            self.assertEqual(len(covr), 1, f"z{z}: {len(covr)} charts stacked")

    # -- PMTiles copy --------------------------------------------------------

    @unittest.skipUnless(h.HAVE_PMTILES, "needs go-pmtiles (pmtiles)")
    def test_pmtiles_archive_matches_mbtiles(self):
        pm = self.out.with_suffix(".pmtiles")
        self.assertTrue(pm.exists(), "no mini.pmtiles beside mini.mbtiles")
        self.assertIn("PMTiles ->", self.first.stdout)
        a, b = h.decode_all(self.out), h.decode_all(pm)
        self.assertEqual(set(a["tiles"]), set(b["tiles"]), "tile sets differ")
        self.assertGreater(len(a["tiles"]), 0)
        for key, digest in a["tiles"].items():
            self.assertEqual(digest, b["tiles"][key], f"tile {key} differs")
        ma, mb = a["metadata"], b["metadata"]
        self.assertEqual(mb.get("type"), "S-57")
        for k in ("name", "description", "format", "minzoom", "maxzoom",
                  "bounds"):
            self.assertEqual(ma.get(k), mb.get(k), k)
        # go-pmtiles re-serialises the `json` row (key order changes);
        # the layer list must survive intact.
        self.assertEqual(json.loads(ma["json"])["vector_layers"],
                         json.loads(mb["json"])["vector_layers"])

    # -- resume ------------------------------------------------------------

    def test_second_run_rebuilds_nothing(self):
        tiles = sorted(p for p in (self.work / "data" / "tiles").glob("*.mbtiles")
                       if p.name != "mini.mbtiles")
        before = {p.name: p.stat().st_mtime_ns for p in tiles}
        second = h.run_pipeline(self.work, *self.args)
        self.assertEqual(second.returncode, 0, second.stderr[-2000:])
        after = {p.name: p.stat().st_mtime_ns for p in tiles}
        self.assertEqual(before, after)
        self.assertIn("GeoJSON fresh", second.stdout)
        self.assertNotIn("Consolidating", second.stdout)


if __name__ == "__main__":
    unittest.main()
