import tempfile
import unittest
from pathlib import Path

from finpay_pipeline.cell_contracts import OBSERVED_CELL_ROSTER
from finpay_pipeline.cell_publication_v2 import CLUSTER_ALIASES, cell_worksheet_title
from finpay_pipeline.tests import run_cell_october_backfill as run


class OctoberBackfillScopeTests(unittest.TestCase):
    def test_verified_roster_aliases_dates_and_all_32_targets(self):
        self.assertEqual(run.ROSTER, OBSERVED_CELL_ROSTER)
        self.assertEqual(run.ALIASES, CLUSTER_ALIASES)
        self.assertEqual((run.DATES[0], run.DATES[-1], len(run.DATES)), ("2026-10-01", "2026-10-17", 17))
        self.assertEqual({item["worksheet_id"] for group in run.TARGETS.values() for item in group.values()},
                         set(range(1900184101, 1900184133)))
        for account, group in run.TARGETS.items():
            for role, item in group.items():
                self.assertEqual(item["title"], cell_worksheet_title(account, role, "cluster_alias"))
        self.assertEqual((run.TARGETS["422090_B"]["summary"]["title"],
                          run.TARGETS["422180_B"]["summary"]["title"]), ("MRT", "MRW"))

    def test_private_source_root_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sources"
            run.private_root(root, create=True)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            link = Path(directory) / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                run.private_root(link)


if __name__ == "__main__":
    unittest.main()
