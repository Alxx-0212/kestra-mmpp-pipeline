import unittest
from pathlib import Path

import linkaja_fee_pipeline

from linkaja_fee_pipeline.migrations import (
    CURRENT_TRANSACTION_VIEW_COLUMN_ORDER,
    CURRENT_TRANSACTION_VIEW_COLUMNS,
    CURRENT_TRANSACTION_COLUMNS,
    DASHBOARD_VIEW_COLUMNS,
    MONTHLY_FEE_SUMMARY_VIEW_COLUMNS,
    _load_migrations,
    latest_linkaja_schema_version,
)
from linkaja_fee_pipeline.sql import (
    AFFECTED_DATES_STATEMENT,
    DAILY_SUMMARY_STATEMENT,
    FEE_SUMMARY_STATEMENT,
    INSERT_MONTHLY_STAGE_STATEMENT,
    LOAD_COUNTS_STATEMENT,
    MONTHLY_DAILY_RAW_STATEMENT,
    MONTHLY_FEE_SUMMARY_STATEMENT,
    MONTHLY_UNRESOLVED_REVERSALS_STATEMENT,
    REPLACE_RAW_STATEMENTS,
    STAGE_CONFLICTS_STATEMENT,
)


class LinkAjaDatabaseSchemaContractTest(unittest.TestCase):
    def test_migrations_are_contiguous_and_current(self):
        migrations = _load_migrations()

        self.assertEqual(
            [migration.version for migration in migrations],
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        )
        self.assertEqual(latest_linkaja_schema_version(), 10)
        self.assertTrue(all(len(migration.checksum) == 64 for migration in migrations))

    def test_final_transaction_schema_keeps_facts_not_report_amounts(self):
        self.assertIn("source_fee", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("source_ledger_row_count", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("ledger_debit_total", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("ledger_credit_total", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("has_company_purchase_account", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("reversal_resolution_status", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("is_unusual", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("unusual_reason_codes", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("report_month", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("calculation_cutoff", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("materialization_id", CURRENT_TRANSACTION_COLUMNS)
        self.assertIn("materialized_at", CURRENT_TRANSACTION_COLUMNS)
        self.assertNotIn("expected_fee", CURRENT_TRANSACTION_COLUMNS)
        self.assertNotIn("reversal_in_cluster_fee", CURRENT_TRANSACTION_COLUMNS)
        self.assertNotIn("report_month", CURRENT_TRANSACTION_VIEW_COLUMNS)
        self.assertIn("monthly_payable_fee", MONTHLY_FEE_SUMMARY_VIEW_COLUMNS)
        self.assertIn("calculation_status", MONTHLY_FEE_SUMMARY_VIEW_COLUMNS)

    def test_dashboard_views_keep_fee_and_bank_evidence_separate(self):
        self.assertEqual(
            set(DASHBOARD_VIEW_COLUMNS),
            {
                "linkaja_daily_fee_reconciliation_v",
                "linkaja_withdrawal_events_v",
                "linkaja_mandiri_settlement_cycles_v",
                "linkaja_reconciliation_exceptions_v",
                "linkaja_load_freshness_v",
            },
        )
        migration_path = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "migrations"
            / "008_dashboard_reconciliation_views.sql"
        )
        normalized = " ".join(
            migration_path.read_text(encoding="utf-8").lower().split()
        )
        self.assertIn("expected_out_cluster_rp200", normalized)
        self.assertIn("ppob_agent_telco_fee", normalized)
        self.assertIn("pending_bank_evidence", normalized)
        self.assertIn("interval_start_exclusive", normalized)
        self.assertIn("interval_end_inclusive", normalized)
        self.assertIn("settlement_cycle_number", normalized)
        self.assertIn(
            "rows between unbounded preceding and 1 preceding",
            normalized,
        )
        self.assertIn("where transaction.is_withdrawal_event", normalized)
        self.assertNotIn("next available withdrawal", normalized)

    def test_live_signed_amount_is_ledger_scoped_and_beside_ledger_totals(self):
        self.assertEqual(
            CURRENT_TRANSACTION_VIEW_COLUMN_ORDER[12:16],
            (
                "ledger_debit_total",
                "ledger_credit_total",
                "signed_amount",
                "source_fee",
            ),
        )
        migration_path = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "migrations"
            / "006_ledger_signed_amount.sql"
        )
        normalized = " ".join(
            migration_path.read_text(encoding="utf-8").lower().split()
        )
        self.assertIn(
            "base.ledger_debit_total - base.ledger_credit_total "
            "as signed_amount",
            normalized,
        )
        self.assertNotIn(
            "company_credit - base.company_debit as signed_amount",
            normalized,
        )

    def test_monthly_transform_includes_non_company_transactions(self):
        normalized = " ".join(INSERT_MONTHLY_STAGE_STATEMENT.lower().split())

        self.assertNotIn("company_transactions", normalized)
        self.assertNotIn("where company_row_count > 0", normalized)
        self.assertIn("from linkaja_transaction_base_v as base", normalized)
        self.assertIn("insert into linkaja_month_snapshot_stage", normalized)

    def test_raw_replacement_uses_cluster_transaction_identity(self):
        delete_statement = " ".join(REPLACE_RAW_STATEMENTS[0].lower().split())

        self.assertIn(
            "select distinct cluster_id, transaction_id from linkaja_raw_stage",
            delete_statement,
        )
        self.assertNotIn("incoming.finalized_date", delete_statement)

    def test_affected_dates_come_from_preserved_refresh_scope(self):
        normalized = " ".join(AFFECTED_DATES_STATEMENT.lower().split())

        self.assertIn("from linkaja_affected_dates", normalized)

    def test_daily_fee_measures_are_calculated_from_transaction_facts(self):
        normalized = " ".join(DAILY_SUMMARY_STATEMENT.lower().split())

        self.assertIn("from linkaja_transactions_current_v", normalized)
        self.assertNotIn("transactions.expected_fee", normalized)
        self.assertNotIn("transactions.reversal_in_cluster_fee", normalized)
        self.assertIn("%(expected_fee_per_transaction)s", normalized)
        self.assertIn("%(in_cluster_fee_per_transaction)s", normalized)
        self.assertIn("%(reversal_in_cluster_fee_per_transaction)s", normalized)
        self.assertIn("reversal_resolution_status = 'complete'", normalized)
        self.assertIn("reversal_resolution_status = 'unresolved'", normalized)
        self.assertGreaterEqual(normalized.count("not transactions.is_reversed"), 2)

        fee_normalized = " ".join(FEE_SUMMARY_STATEMENT.lower().split())
        self.assertIn("from linkaja_transactions_current_v", fee_normalized)
        self.assertIn("%(in_cluster_fee_per_transaction)s", fee_normalized)
        self.assertGreaterEqual(fee_normalized.count("not is_reversed"), 2)
        self.assertNotIn(
            "sum(source_fee) filter ( where transaction_scenario = "
            "'digipos b2b transfer in cluster'",
            fee_normalized,
        )

    def test_stage_rejects_inconsistent_finalized_times(self):
        normalized = " ".join(STAGE_CONFLICTS_STATEMENT.lower().split())

        self.assertIn(
            "count(distinct finalized_time) as finalized_times",
            normalized,
        )
        self.assertIn("or count(distinct finalized_time) > 1", normalized)

    def test_daily_persistence_does_not_write_monthly_snapshot(self):
        normalized = " ".join(LOAD_COUNTS_STATEMENT.lower().split())

        self.assertIn("from linkaja_transactions_current_v", normalized)
        self.assertNotIn("updated_load_id = %(load_id)s", normalized)

    def test_runtime_and_published_monthly_calculations_share_fee_contract(self):
        runtime = " ".join(MONTHLY_FEE_SUMMARY_STATEMENT.lower().split())
        migration_path = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "migrations"
            / "007_monthly_fee_category_stability.sql"
        )
        published = " ".join(
            migration_path.read_text(encoding="utf-8").lower().split()
        )

        for normalized in (runtime, published):
            self.assertIn("transaction.source_fee = 200::numeric", normalized)
            self.assertIn("transaction.source_fee as fee_amount", normalized)
            self.assertIn("where not transaction.is_reversal", normalized)
            self.assertIn("where not is_reversed", normalized)
            self.assertIn("incomplete_missing_fee", normalized)
            self.assertIn("general to purchase b2b transfer agent telco", normalized)
            self.assertIn("fee_categories as", normalized)
            self.assertIn("aggregate.active_missing_fee_count", normalized)
            self.assertNotIn("transaction.company_credit", normalized)
            self.assertNotIn("digipos_debet_variance", normalized)
        self.assertIn("cross join fee_categories", published)

    def test_daily_operational_queries_use_active_original_fees(self):
        query_directory = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "queries"
        )
        for query_name in (
            "linkaja_daily_fee_report.sql",
            "linkaja_daily_detail_measures.sql",
        ):
            normalized = " ".join(
                (query_directory / query_name)
                .read_text(encoding="utf-8")
                .lower()
                .split()
            )
            active_predicates = (
                normalized.count("not is_reversed")
                + normalized.count("not transaction.is_reversed")
            )
            self.assertGreaterEqual(active_predicates, 2)

    def test_monthly_preview_exposes_reversal_aware_daily_raw_rows(self):
        normalized = " ".join(MONTHLY_DAILY_RAW_STATEMENT.lower().split())

        self.assertIn("from linkaja_month_snapshot_stage", normalized)
        self.assertIn("transaction.report_date", normalized)
        self.assertIn("transaction.ledger_debit_total", normalized)
        self.assertIn("transaction.ledger_credit_total", normalized)
        self.assertIn("transaction.signed_amount", normalized)
        self.assertIn("reversal_resolution_status = 'complete'", normalized)
        self.assertIn("reversal_resolution_status = 'unresolved'", normalized)
        self.assertIn("not transaction.is_reversal", normalized)
        self.assertIn("transaction.is_reversed", normalized)

    def test_monthly_unresolved_analysis_uses_ids_and_frozen_cutoff(self):
        normalized = " ".join(
            MONTHLY_UNRESOLVED_REVERSALS_STATEMENT.lower().split()
        )

        self.assertIn("join linkaja_reversal_edges_current_v", normalized)
        self.assertIn("edge.resolution_status <> 'complete'", normalized)
        self.assertIn(
            "reversal.finalized_at_local < %(calculation_cutoff)s",
            normalized,
        )
        self.assertNotIn("company_credit =", normalized)
        self.assertNotIn("ledger_debit_total =", normalized)

    def test_monthly_workflow_publishes_database_analysis_files_only(self):
        workflow_path = (
            Path(__file__).parents[2] / "linkaja_fee_pipeline" / "workflows" / "linkaja_monthly_materialization.yml"
        )
        normalized = " ".join(
            workflow_path.read_text(encoding="utf-8").lower().split()
        )

        self.assertIn("linkaja_daily_raw_calculation.csv", normalized)
        self.assertIn("linkaja_unresolved_reversals.csv", normalized)
        self.assertNotIn("gspread", normalized)
        self.assertNotIn("gcp_sa_key", normalized)
        self.assertNotIn("next available withdrawal", normalized)

    def test_daily_workflow_persists_results_without_sheet_side_effects(self):
        workflow_path = Path(__file__).parents[2] / "linkaja_fee_pipeline" / "workflows" / "linkaja_fee_pipeline.yml"
        normalized = " ".join(
            workflow_path.read_text(encoding="utf-8").lower().split()
        )

        self.assertIn("linkaja_database_result.json", normalized)
        self.assertIn("persist_linkaja_normalized_csv", normalized)
        self.assertNotIn("upload_linkaja_summaries_to_sheets", normalized)
        self.assertNotIn("gspread", normalized)
        self.assertNotIn("gcp_sa_key", normalized)
        self.assertNotIn("target_spreadsheet", normalized)

    def test_linkaja_runtime_api_and_dependencies_are_sheet_free(self):
        self.assertFalse(hasattr(linkaja_fee_pipeline, "make_gspread_client"))
        self.assertFalse(
            hasattr(linkaja_fee_pipeline, "process_linkaja_fee_sheet_upload")
        )
        self.assertFalse(
            hasattr(linkaja_fee_pipeline, "process_linkaja_detail_sheet_upload")
        )

        requirements_path = (
            Path(__file__).parents[2] / "linkaja_fee_pipeline" / "requirements.txt"
        )
        normalized = requirements_path.read_text(encoding="utf-8").lower()
        self.assertNotIn("gspread", normalized)
        self.assertNotIn("google-auth", normalized)

    def test_manual_monthly_query_uses_summary_view_and_published_cutoff(self):
        query_path = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "queries"
            / "linkaja_monthly_fee_summary.sql"
        )
        normalized = " ".join(query_path.read_text(encoding="utf-8").lower().split())

        self.assertIn("from linkaja_monthly_fee_summary_v as summary", normalized)
        self.assertIn("from linkaja_monthly_refreshes as refresh", normalized)
        self.assertIn("join linkaja_transaction_base_v as reversal", normalized)
        self.assertIn("publication.calculation_cutoff", normalized)
        self.assertIn("and not exists", normalized)

    def test_operational_linkaja_queries_are_available_in_domain_directory(self):
        sql_directory = (
            Path(__file__).parents[2]
            / "linkaja_fee_pipeline"
            / "queries"
        )
        expected_files = {
            "linkaja_daily_detail_measures.sql",
            "linkaja_daily_fee_report.sql",
            "linkaja_live_transactions.sql",
            "linkaja_monthly_fee_summary.sql",
            "linkaja_monthly_refresh_candidates.sql",
            "linkaja_monthly_snapshot_audit.sql",
            "linkaja_relation_counts.sql",
            "linkaja_unresolved_reversals.sql",
        }

        self.assertTrue(all((sql_directory / name).is_file() for name in expected_files))

if __name__ == "__main__":
    unittest.main()
