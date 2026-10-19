"""The mesh catalogue (charts/mesh/index.json) writer and validator,
mesh-catalog.py: the fields the plugin requires, the URL check, and the
refusal to write a catalogue with a bad entry."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mesh_catalog", ROOT / "mesh-catalog.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

PUB = "https://example.test/US-ENC/charts"


def sidecar(name="01CGD", **over):
    s = {"name": name, "build_date": "2026-10-16T05:57:08Z", "chart_build_date": "2026-10-16T00:56:10Z",
         "format": {"magic": "WRPMESH1", "index_version": 1, "tile_deg": 0.25, "multi_cluster": False},
         "triangles": 42329633, "bytes": 3386387856,
         "clusters": [{"dir": ".", "box": [-74.749875, 39.750125, -65.749875, 45.750125]}]}
    s.update(over)
    return s


def good_head(url):
    return 200, "721491162"


class Entry(unittest.TestCase):
    def test_entry_carries_every_required_field(self):
        e = mc.entry_from_sidecar(sidecar(), PUB + "/")
        self.assertEqual(e["archive"], "01CGD_mesh.tar.zst")
        self.assertEqual(e["archive_url"], PUB + "/01CGD_mesh.tar.zst")
        self.assertEqual(e["dir"], "mesh/01CGD/")
        self.assertEqual(mc.validate_entry(e), [])

    def test_bad_values_are_named(self):
        cases = [
            ({"name": "01 CGD"}, "name"),
            ({"build_date": "yesterday"}, "build_date"),
            ({"chart_build_date": 42}, "chart_build_date"),
            ({"triangles": -1}, "triangles"),
            ({"bytes": "big"}, "bytes"),
            ({"clusters": []}, "clusters"),
            ({"clusters": [{"dir": ".", "box": [1, 2, 3]}]}, "clusters[0].box"),
            ({"clusters": [{"dir": ".", "box": [1, 2, float("nan"), 4]}]}, "clusters[0].box"),
            ({"clusters": [{"dir": ".", "box": [3, 2, 1, 4]}]}, "clusters[0].box"),
        ]
        for over, field in cases:
            e = mc.entry_from_sidecar(sidecar(**over), PUB)
            bad = mc.validate_entry(e)
            self.assertTrue(any(b.startswith(field) for b in bad), (over, bad))

    def test_chart_build_date_may_be_null(self):
        e = mc.entry_from_sidecar(sidecar(chart_build_date=None), PUB)
        self.assertEqual(mc.validate_entry(e), [])

    def test_archive_must_match_name_and_url(self):
        e = mc.entry_from_sidecar(sidecar(), PUB)
        e["archive"] = "other.tar.zst"
        self.assertTrue(any(b.startswith("archive ") for b in mc.validate_entry(e)))
        e = mc.entry_from_sidecar(sidecar(), PUB)
        e["archive_url"] = "http://example.test/01CGD_mesh.tar.zst"
        self.assertTrue(any(b.startswith("archive_url") for b in mc.validate_entry(e)))


class UrlCheck(unittest.TestCase):
    def test_ok(self):
        e = mc.entry_from_sidecar(sidecar(), PUB)
        self.assertEqual(mc.check_url(e, good_head), [])

    def test_status_length_and_failure(self):
        e = mc.entry_from_sidecar(sidecar(), PUB)
        self.assertTrue(mc.check_url(e, lambda u: (404, "1")))
        self.assertTrue(mc.check_url(e, lambda u: (200, None)))
        self.assertTrue(mc.check_url(e, lambda u: (200, "512")))                 # under the minimum
        self.assertTrue(mc.check_url(e, lambda u: (200, str(e["bytes"] + 1))))  # bigger than unpacked

        def boom(u):
            raise OSError("no route")
        self.assertTrue(mc.check_url(e, boom))


class Catalog(unittest.TestCase):
    def write(self, d, *sidecars):
        paths = []
        for s in sidecars:
            p = os.path.join(d, f"{s['name']}_mesh.json")
            json.dump(s, open(p, "w"))
            paths.append(p)
        return paths

    def test_catalog_of_two_meshes(self):
        with tempfile.TemporaryDirectory() as d:
            paths = self.write(d, sidecar("01CGD"), sidecar("09CGD", clusters=[
                {"dir": ".", "box": [-92.499875, 41.000125, -74.499875, 48.750125]}]))
            cat, problems = mc.build_catalog(paths, PUB, check_urls=True, head_fn=good_head)
        self.assertEqual(problems, [])
        self.assertEqual(cat["version"], 1)
        self.assertEqual([m["name"] for m in cat["meshes"]], ["01CGD", "09CGD"])
        self.assertEqual(cat["meshes"][1]["archive_url"], PUB + "/09CGD_mesh.tar.zst")

    def test_one_bad_entry_fails_the_whole_catalogue(self):
        with tempfile.TemporaryDirectory() as d:
            paths = self.write(d, sidecar("01CGD"), sidecar("09CGD", clusters=[]))
            out = os.path.join(d, "index.json")
            with self.assertRaises(SystemExit) as cm:
                mc.main(["--sidecars", d, "--public-url", PUB, "-o", out])
            self.assertEqual(cm.exception.code, 1)
            self.assertFalse(os.path.exists(out))

    def test_main_writes_when_all_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, sidecar("01CGD"))
            out = os.path.join(d, "index.json")
            mc.main(["--sidecars", d, "--public-url", PUB, "-o", out])
            cat = json.load(open(out))
            self.assertEqual(cat["meshes"][0]["archive_url"], PUB + "/01CGD_mesh.tar.zst")
            self.assertEqual(cat["meshes"][0]["archive"], "01CGD_mesh.tar.zst")


if __name__ == "__main__":
    unittest.main()
