"""Unit tests for bin_map: every model label maps to a valid physical bin."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bin_map

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LABELS = os.path.join(_REPO, "exports", "labels.txt")


class BinMapTest(unittest.TestCase):
    def test_four_bin_names(self):
        self.assertEqual(set(bin_map.BIN_NAMES), {0, 1, 2, 3})

    def test_all_targets_valid(self):
        for label, b in bin_map.LABEL_TO_BIN.items():
            self.assertIn(b, bin_map.BIN_NAMES, f"{label} -> invalid bin {b}")

    def test_every_exported_label_is_mapped(self):
        with open(_LABELS) as f:
            labels = [ln.strip() for ln in f if ln.strip()]
        missing = [l for l in labels if l not in bin_map.LABEL_TO_BIN]
        self.assertEqual(missing, [], f"unmapped labels: {missing}")

    def test_known_and_unknown(self):
        self.assertEqual(bin_map.label_to_bin("newspaper"), 0)
        self.assertEqual(bin_map.label_to_bin("aluminum_soda_cans"), 1)
        self.assertEqual(bin_map.label_to_bin("food_waste"), 2)
        self.assertEqual(bin_map.label_to_bin("shoes"), 3)
        self.assertEqual(bin_map.label_to_bin("does_not_exist"), bin_map.DEFAULT_BIN)


if __name__ == "__main__":
    unittest.main()
