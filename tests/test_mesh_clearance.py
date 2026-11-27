"""The clearance rule of the mesh build (mesh.build.clearance_of)."""
import unittest

try:
    from mesh.build import clearance_of
except ImportError:          # numpy / shapely / scipy missing
    clearance_of = None


@unittest.skipIf(clearance_of is None, "mesh packages not installed")
class ClearanceOf(unittest.TestCase):
    def test_fixed_bridge_first_of_closed_then_general_then_safe(self):
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "1", "VERCLR": "12.5"}), (12.5, False))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "1", "VERCSA": "9", "VERCLR": "10"}), (10.0, False))
        self.assertEqual(clearance_of("CBLOHD", {"VERCSA": "30"}), (30.0, False))

    def test_fixed_bridge_without_height_is_none_not_zero(self):
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "1"}), (None, False))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "9", "OBJNAM": "footbridge"}), (None, False))
        self.assertEqual(clearance_of("PIPOHD", {}), (None, False))

    def test_opening_bridge_carries_open_clearance_and_flag(self):
        # Broadway Bridge, Harlem River: lift, closed 7.3 m, open 41.1 m
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "4", "VERCCL": "7.3", "VERCOP": "41.1"}), (41.1, True))
        # bascule with only the closed clearance charted: no height, flagged
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "5", "VERCCL": "1.5"}), (None, True))
        for code in ("2", "3", "7", "8"):
            self.assertEqual(clearance_of("BRIDGE", {"CATBRG": code}), (None, True), code)

    def test_list_valued_catbrg(self):
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "1,3"}), (None, True))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "(5,9)"}), (None, True))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "1,9"}), (None, False))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": "12"}), (None, False))
        self.assertEqual(clearance_of("BRIDGE", {"CATBRG": None}), (None, False))
        self.assertEqual(clearance_of("BRIDGE", {}), (None, False))

    def test_pipelines_and_conveyors_carry_a_height(self):
        self.assertEqual(clearance_of("PIPOHD", {"CATPIP": "6", "VERCLR": "8.5"}), (8.5, False))
        self.assertEqual(clearance_of("CONVYR", {"VERCLR": "15"}), (15.0, False))

    def test_only_bridges_open(self):
        # the category is a BRIDGE attribute; a cable carrying one is still a fixed span
        self.assertEqual(clearance_of("CBLOHD", {"CATBRG": "4", "VERCLR": "20"}), (20.0, False))


if __name__ == "__main__":
    unittest.main()
