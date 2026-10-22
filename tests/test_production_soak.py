import os
import unittest
from unittest.mock import patch

from scripts import production_soak


class ProductionSoakProcessTests(unittest.TestCase):
    def test_candidate_dependency_inventory_does_not_require_psutil(self):
        dependencies = production_soak.candidate_identity()["dependencies"]

        self.assertNotIn("psutil", dependencies)

    def test_process_table_tracks_descendants_across_multiple_generations(self):
        parents = production_soak._parse_process_table("10 1\n11 10\n12 11\n20 1\n")

        self.assertEqual(production_soak._descendants(10, parents), {11, 12})
        self.assertEqual(production_soak._descendants(20, parents), set())

    def test_windows_process_table_excludes_its_own_probe(self):
        output = '[{"pid":10,"ppid":1},{"pid":11,"ppid":10},{"pid":99,"ppid":10}]'
        with patch("scripts.production_soak.platform.system", return_value="Windows"), \
             patch("scripts.production_soak.shutil.which", return_value="powershell"), \
             patch("scripts.production_soak._process_output", return_value=(output, 99)):
            self.assertEqual(production_soak._process_table(), {10: 1, 11: 10})

    def test_windows_process_snapshot_maps_native_resource_counters(self):
        with patch("scripts.production_soak.platform.system", return_value="Windows"), \
             patch("scripts.production_soak._powershell", return_value=
                   '{"rssBytes":4096,"threads":3,"openHandles":8}'):
            self.assertEqual(production_soak._process_snapshot(10),
                             {"rss_bytes": 4096, "threads": 3, "open_handles": 8})

    def test_current_platform_process_probe_returns_bounded_resource_counts(self):
        snapshot = production_soak._process_snapshot(os.getpid())

        self.assertGreater(snapshot["rss_bytes"], 0)
        self.assertGreaterEqual(snapshot["threads"], 1)
        self.assertGreater(snapshot["open_handles"], 0)


if __name__ == "__main__":
    unittest.main()
