import os
import unittest
from datetime import date

from linkaja_fee_pipeline.dashboard.repository import LinkAjaDashboardRepository
from linkaja_fee_pipeline.dashboard.service import (
    DashboardFilters,
    LinkAjaDashboardService,
)
from linkaja_fee_pipeline.migrations import migrate_linkaja_database


TEST_DSN = os.environ.get("LINKAJA_TEST_DSN", "").strip()
TEST_DATABASE_GUARD = os.environ.get(
    "LINKAJA_TEST_DATABASE_GUARD",
    "",
).strip()

try:
    import psycopg
    import psycopg_pool  # noqa: F401
except ModuleNotFoundError:
    psycopg = None


def _assert_disposable_database_target():
    if not TEST_DATABASE_GUARD:
        raise RuntimeError(
            "LINKAJA_TEST_DATABASE_GUARD must name the exact disposable "
            "database before destructive dashboard tests can run"
        )
    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            actual_database = str(cursor.fetchone()[0])
    if actual_database != TEST_DATABASE_GUARD:
        raise RuntimeError(
            "LinkAja dashboard test guard mismatch: connected to "
            f"{actual_database!r}, expected {TEST_DATABASE_GUARD!r}"
        )


@unittest.skipUnless(
    TEST_DSN and psycopg is not None,
    "LINKAJA_TEST_DSN, psycopg, and psycopg-pool are required",
)
class LinkAjaDashboardDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable_database_target()
        migrate_linkaja_database(TEST_DSN)

    def setUp(self):
        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE linkaja_transactions, linkaja_monthly_refreshes, "
                    "linkaja_raw_transactions"
                )

    def test_all_dashboard_queries_execute_with_stable_empty_dates(self):
        repository = LinkAjaDashboardRepository(TEST_DSN)
        try:
            snapshot = LinkAjaDashboardService(repository).load(
                DashboardFilters(
                    start_date=date(2026, 7, 1),
                    end_date=date(2026, 7, 2),
                    cluster_ids=("123456",),
                )
            )
        finally:
            repository.close()

        self.assertEqual(len(snapshot.daily_rows), 2)
        self.assertEqual(len(snapshot.fee_rows), 8)
        self.assertEqual(snapshot.expected_out_fee, "Rp0")
        self.assertEqual(snapshot.ppob_fee, "Rp0")
        self.assertEqual(snapshot.mandiri_rows, [])
        self.assertEqual(snapshot.exception_rows, [])
        self.assertEqual(snapshot.monthly_rows, [])
        self.assertEqual(snapshot.reconciliation_status, "Clear")


if __name__ == "__main__":
    unittest.main()
