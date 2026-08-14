import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linkaja_fee_pipeline.dashboard import app
from linkaja_fee_pipeline.dashboard.config import DashboardConfig
from linkaja_fee_pipeline.dashboard.repository import (
    DAILY_FEE_SQL,
    FEE_TYPES,
    LinkAjaDashboardRepository,
)
from linkaja_fee_pipeline.dashboard.service import (
    DashboardFilters,
    LinkAjaDashboardService,
    format_idr,
)


class FakeDashboardRepository:
    def available_clusters(self):
        return ["123456"]

    def daily_fee_rows(self, *_args, **_kwargs):
        amounts = {
            "EXPECTED_OUT_CLUSTER_RP200": Decimal("400"),
            "IN_CLUSTER_RP20": Decimal("20"),
            "POSTED_DIGIPOS_FEE": Decimal("200"),
            "PPOB_AGENT_TELCO_FEE": None,
        }
        return [
            {
                "cluster_id": "123456",
                "report_date": date(2026, 7, 1),
                "fee_type": fee_type,
                "gross_transaction_count": 2,
                "reversed_transaction_count": 0,
                "active_transaction_count": 2,
                "gross_fee": amount,
                "reversed_fee": Decimal("0"),
                "active_fee": amount,
                "missing_fee_count": 1 if amount is None else 0,
                "active_missing_fee_count": 1 if amount is None else 0,
                "invalid_fee_count": 0,
                "calculation_status": (
                    "INCOMPLETE_MISSING_FEE" if amount is None else "COMPLETE"
                ),
            }
            for fee_type, amount in amounts.items()
        ]

    def daily_summary_rows(self, *_args, **_kwargs):
        return [
            {
                "cluster_id": "123456",
                "report_date": date(2026, 7, 1),
                "withdrawal_amount": Decimal("100000"),
                "expected_out_cluster_fee": Decimal("400"),
                "in_cluster_fee": Decimal("20"),
                "posted_digipos_fee": Decimal("200"),
                "ppob_fee": None,
                "exception_count": 1,
                "daily_status": "REVIEW_REQUIRED",
            }
        ]

    def mandiri_cycles(self, *_args, **_kwargs):
        return [
            {
                "cluster_id": "123456",
                "company_account": "6001",
                "interval_end_inclusive": datetime(2026, 7, 1, 18, 0),
                "opening_balance": Decimal("200000"),
                "non_withdrawal_company_credit": Decimal("0"),
                "non_withdrawal_company_debit": Decimal("100000"),
                "withdrawal_company_debit": Decimal("100000"),
                "closing_balance": Decimal("0"),
                "balance_variance": Decimal("0"),
                "linkaja_reconciliation_status": "BALANCED",
                "mandiri_confirmation_status": "PENDING_BANK_EVIDENCE",
                "reversal_status": "CLEAR",
            }
        ]

    def exceptions(self, *_args, **_kwargs):
        return [
            {
                "cluster_id": "123456",
                "finalized_at_local": datetime(2026, 7, 1, 12, 0),
                "transaction_id": "PPOB-1",
                "severity": "BLOCKING",
                "exception_code": "ACTIVE_MISSING_PPOB_FEE",
                "detail": "Active source fee is missing.",
                "source_files": "july.csv",
            }
        ]

    def transactions(self, *_args, **_kwargs):
        return []

    def monthly_close_rows(self, *_args, **_kwargs):
        return []

    def freshness_rows(self, *_args, **_kwargs):
        return [
            {
                "cluster_id": "123456",
                "source_file": "july.csv",
                "transaction_count": 10,
                "min_report_date": date(2026, 7, 1),
                "max_report_date": date(2026, 7, 31),
                "ingestion_completed_at": datetime(2026, 8, 1, 9, 0),
                "load_status": "SUCCESS",
            }
        ]


class LinkAjaDashboardConfigTest(unittest.TestCase):
    def test_config_is_validated_and_dsn_is_redacted(self):
        with patch.dict(
            "os.environ",
            {
                "LINKAJA_DASHBOARD_DB_DSN":
                    "postgresql://reader:secret@localhost/linkaja",
                "LINKAJA_DASHBOARD_PORT": "5050",
                "LINKAJA_DASHBOARD_MAX_DATE_SPAN_DAYS": "62",
                "LINKAJA_DASHBOARD_DEFAULT_DATE_SPAN_DAYS": "31",
            },
            clear=True,
        ):
            config = DashboardConfig.from_env()

        self.assertEqual(config.port, 5050)
        self.assertEqual(config.max_date_span_days, 62)
        self.assertNotIn("secret", repr(config))
        self.assertIn("<redacted>", repr(config))

    def test_config_rejects_default_range_above_query_bound(self):
        with patch.dict(
            "os.environ",
            {
                "LINKAJA_DASHBOARD_DB_DSN": "postgresql://localhost/linkaja",
                "LINKAJA_DASHBOARD_MAX_DATE_SPAN_DAYS": "10",
                "LINKAJA_DASHBOARD_DEFAULT_DATE_SPAN_DAYS": "31",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "between 1 and 10"):
                DashboardConfig.from_env()


class LinkAjaDashboardRepositoryTest(unittest.TestCase):
    def test_repository_uses_bound_parameters_and_hard_date_limit(self):
        captured = []
        repository = LinkAjaDashboardRepository(
            "postgresql://unused",
            max_date_span_days=31,
            query_executor=lambda sql, params: captured.append((sql, params)) or [],
        )

        repository.daily_fee_rows(
            date(2026, 7, 1),
            date(2026, 7, 31),
            ["123456"],
        )
        sql, params = captured[0]
        self.assertEqual(sql, DAILY_FEE_SQL)
        self.assertEqual(params["cluster_ids"], ["123456"])
        self.assertEqual(params["fee_types"], list(FEE_TYPES))
        self.assertNotIn("123456", sql)

        with self.assertRaisesRegex(ValueError, "limited to 31 days"):
            repository.daily_fee_rows(
                date(2026, 7, 1),
                date(2026, 8, 1),
                ["123456"],
            )

    def test_repository_rejects_empty_or_unsafe_clusters(self):
        repository = LinkAjaDashboardRepository(
            "postgresql://unused",
            query_executor=lambda _sql, _params: [],
        )
        for clusters in ([], ["x'); DROP TABLE fees; --"]):
            with self.assertRaises(ValueError):
                repository.daily_summary_rows(
                    date(2026, 7, 1),
                    date(2026, 7, 2),
                    clusters,
                )


class LinkAjaDashboardServiceTest(unittest.TestCase):
    def test_service_keeps_all_fee_types_separate_and_null_is_incomplete(self):
        service = LinkAjaDashboardService(FakeDashboardRepository())
        snapshot = service.load(
            DashboardFilters(
                date(2026, 7, 1),
                date(2026, 7, 31),
                ("123456",),
            )
        )

        self.assertEqual(snapshot.expected_out_fee, "Rp400")
        self.assertEqual(snapshot.in_cluster_fee, "Rp20")
        self.assertEqual(snapshot.posted_digipos_fee, "Rp200")
        self.assertEqual(snapshot.ppob_fee, "Incomplete")
        self.assertEqual(snapshot.withdrawal_total, "Rp100.000")
        self.assertEqual(snapshot.reconciliation_status, "Review required")
        self.assertEqual(snapshot.exception_count, "1")
        self.assertEqual(len(snapshot.fee_rows), 4)

    def test_fee_selector_filters_detail_without_changing_kpis(self):
        service = LinkAjaDashboardService(FakeDashboardRepository())
        snapshot = service.load(
            DashboardFilters(
                date(2026, 7, 1),
                date(2026, 7, 1),
                ("123456",),
                ("IN_CLUSTER_RP20",),
            )
        )

        self.assertEqual(len(snapshot.fee_rows), 1)
        self.assertEqual(snapshot.expected_out_fee, "Rp400")
        self.assertEqual(format_idr(None), "Incomplete")


class LinkAjaDashboardUiContractTest(unittest.TestCase):
    def test_pages_expose_date_cluster_fee_filters_and_finance_views(self):
        all_pages = "\n".join(app.PAGES.values())

        self.assertIn("|date_range|", all_pages)
        self.assertIn("{selected_clusters}|selector", all_pages)
        self.assertIn("{selected_fee_types}|selector", all_pages)
        self.assertIn("Mandiri settlement cycles", all_pages)
        self.assertIn("Published month close", all_pages)
        self.assertNotIn("Total fee", all_pages)

    def test_state_filter_converts_datetimes_and_retains_selected_values(self):
        state = SimpleNamespace(
            date_range=[
                datetime(2026, 7, 1, 0, 0),
                datetime(2026, 7, 31, 0, 0),
            ],
            selected_clusters=["123456"],
            selected_fee_types=["POSTED_DIGIPOS_FEE"],
        )

        filters = app._state_filters(state)
        self.assertEqual(filters.start_date, date(2026, 7, 1))
        self.assertEqual(filters.end_date, date(2026, 7, 31))
        self.assertEqual(filters.cluster_ids, ("123456",))

    def test_dashboard_runtime_is_domain_local_and_version_pinned(self):
        dashboard_dir = (
            Path(__file__).parents[1] / "linkaja_fee_pipeline" / "dashboard"
        )
        requirements = (dashboard_dir / "requirements.txt").read_text(
            encoding="utf-8"
        )
        dockerfile = (dashboard_dir / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("taipy-gui==4.1.2", requirements)
        self.assertIn("FROM python:3.11-slim", dockerfile)
        self.assertNotIn("Dockerfile.linkaja", dockerfile)


if __name__ == "__main__":
    unittest.main()
