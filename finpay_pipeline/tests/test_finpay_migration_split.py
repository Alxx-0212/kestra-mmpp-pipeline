import unittest
from unittest.mock import MagicMock, patch

from finpay_pipeline import database


class FinPayMigrationSplitTest(unittest.TestCase):
    def test_daily_migration_contains_only_source_evidence_relations(self):
        daily_sql = database.FINPAY_DAILY_SOURCE_EVIDENCE_MIGRATION.read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("create table if not exists finpay_source_loads", daily_sql)
        self.assertIn("create table if not exists finpay_ledger_events", daily_sql)
        self.assertNotIn("finpay_monthly_transactions", daily_sql)
        self.assertNotIn("finpay_monthly_publications", daily_sql)

    def test_monthly_migration_owns_monthly_relations_and_views(self):
        monthly_sql = database.FINPAY_MONTHLY_MODEL_MIGRATION.read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("create table if not exists finpay_monthly_transactions", monthly_sql)
        self.assertIn("create table if not exists finpay_monthly_publications", monthly_sql)
        self.assertIn("create table if not exists finpay_monthly_previews", monthly_sql)
        self.assertIn("create or replace view finpay_reversal_status_current_v", monthly_sql)

    def test_daily_initializer_executes_only_daily_evidence_ddl(self):
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context

        with patch.object(database.psycopg, "connect", return_value=connection):
            database.ensure_finpay_daily_source_evidence("test-dsn")

        self.assertEqual(cursor.execute.call_count, 1)
        executed = cursor.execute.call_args.args[0].lower()
        self.assertIn("finpay_ledger_events", executed)
        self.assertNotIn("finpay_monthly_transactions", executed)
        connection.commit.assert_called_once()

    def test_monthly_initializer_creates_daily_then_monthly_relations(self):
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context

        with patch.object(database.psycopg, "connect", return_value=connection):
            database.ensure_finpay_monthly_model("test-dsn")

        self.assertEqual(cursor.execute.call_count, 2)
        daily_sql, monthly_sql = [
            call.args[0].lower() for call in cursor.execute.call_args_list
        ]
        self.assertIn("finpay_source_loads", daily_sql)
        self.assertNotIn("finpay_monthly_transactions", daily_sql)
        self.assertIn("finpay_monthly_transactions", monthly_sql)
        connection.commit.assert_called_once()

    def test_legacy_combined_migration_is_retained_but_not_daily_initializer(self):
        legacy_sql = database.FINPAY_TRANSACTION_MODEL_MIGRATION.read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("finpay_source_loads", legacy_sql)
        self.assertIn("finpay_monthly_transactions", legacy_sql)
        self.assertEqual(
            database.FINPAY_TRANSACTION_MODEL_MIGRATION.name,
            "001_finpay_transaction_model_legacy_compat.sql",
        )


if __name__ == "__main__":
    unittest.main()
