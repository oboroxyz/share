"""Regression checks for races between directory preflight and staging."""

import tempfile
import unittest
from pathlib import Path

from share import bundle


class BundleStagingTests(unittest.TestCase):
    def test_snapshot_rejects_root_symlink_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "report"
            root.mkdir()
            (root / "index.html").write_text("report")
            files, _ = bundle.collect_files(root, [])
            moved = Path(temporary) / "moved-root"
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(bundle.BundleError):
                bundle.snapshot(root, files, Path(temporary) / "snapshot")


if __name__ == "__main__":
    unittest.main()
