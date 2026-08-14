"""Bounded, parameterized, read-only PostgreSQL access for Taipy."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Iterator, Sequence


FEE_TYPES = (
    "EXPECTED_OUT_CLUSTER_RP200",
    "IN_CLUSTER_RP20",
    "POSTED_DIGIPOS_FEE",
    "PPOB_AGENT_TELCO_FEE",
)

DAILY_FEE_SQL = """
WITH requested_dates AS (
    SELECT generated::DATE AS report_date
    FROM GENERATE_SERIES(
        %(start_date)s::DATE,
        %(end_date)s::DATE,
        INTERVAL '1 day'
    ) AS generated
), requested_clusters AS (
    SELECT UNNEST(%(cluster_ids)s::TEXT[]) AS cluster_id
), requested_fee_types AS (
    SELECT UNNEST(%(fee_types)s::TEXT[]) AS fee_type
)
SELECT
    cluster.cluster_id,
    day.report_date,
    type.fee_type,
    COALESCE(fee.gross_transaction_count, 0) AS gross_transaction_count,
    COALESCE(fee.reversed_transaction_count, 0)
        AS reversed_transaction_count,
    COALESCE(fee.active_transaction_count, 0) AS active_transaction_count,
    COALESCE(fee.gross_fee, 0) AS gross_fee,
    COALESCE(fee.reversed_fee, 0) AS reversed_fee,
    CASE
        WHEN fee.calculation_status LIKE 'INCOMPLETE_%%' THEN fee.active_fee
        ELSE COALESCE(fee.active_fee, 0)
    END AS active_fee,
    COALESCE(fee.missing_fee_count, 0) AS missing_fee_count,
    COALESCE(fee.active_missing_fee_count, 0) AS active_missing_fee_count,
    COALESCE(fee.invalid_fee_count, 0) AS invalid_fee_count,
    COALESCE(fee.resolved_reversal_event_count, 0)
        AS resolved_reversal_event_count,
    COALESCE(fee.unresolved_reversal_count, 0)
        AS unresolved_reversal_count,
    COALESCE(fee.calculation_status, 'COMPLETE') AS calculation_status,
    fee.source_files,
    fee.latest_load_id,
    fee.latest_updated_at
FROM requested_dates AS day
CROSS JOIN requested_clusters AS cluster
CROSS JOIN requested_fee_types AS type
LEFT JOIN linkaja_daily_fee_reconciliation_v AS fee
  ON fee.cluster_id = cluster.cluster_id
 AND fee.report_date = day.report_date
 AND fee.fee_type = type.fee_type
ORDER BY day.report_date, cluster.cluster_id, type.fee_type
"""

DAILY_SUMMARY_SQL = """
WITH requested_dates AS (
    SELECT generated::DATE AS report_date
    FROM GENERATE_SERIES(
        %(start_date)s::DATE,
        %(end_date)s::DATE,
        INTERVAL '1 day'
    ) AS generated
), requested_clusters AS (
    SELECT UNNEST(%(cluster_ids)s::TEXT[]) AS cluster_id
), fee AS (
    SELECT
        reconciliation.cluster_id,
        reconciliation.report_date,
        MAX(reconciliation.active_fee) FILTER (
            WHERE reconciliation.fee_type = 'EXPECTED_OUT_CLUSTER_RP200'
        ) AS expected_out_cluster_fee,
        MAX(reconciliation.active_fee) FILTER (
            WHERE reconciliation.fee_type = 'IN_CLUSTER_RP20'
        ) AS in_cluster_fee,
        MAX(reconciliation.active_fee) FILTER (
            WHERE reconciliation.fee_type = 'POSTED_DIGIPOS_FEE'
        ) AS posted_digipos_fee,
        MAX(reconciliation.active_fee) FILTER (
            WHERE reconciliation.fee_type = 'PPOB_AGENT_TELCO_FEE'
        ) AS ppob_fee,
        BOOL_OR(
            reconciliation.fee_type = 'PPOB_AGENT_TELCO_FEE'
            AND reconciliation.calculation_status <> 'COMPLETE'
        ) AS has_incomplete_ppob_fee,
        MAX(reconciliation.unresolved_reversal_count)
            AS unresolved_reversal_count,
        SUM(reconciliation.active_missing_fee_count)
            AS active_missing_fee_count,
        SUM(reconciliation.invalid_fee_count) AS invalid_fee_count,
        BOOL_OR(reconciliation.calculation_status <> 'COMPLETE')
            AS has_incomplete_fee
    FROM linkaja_daily_fee_reconciliation_v AS reconciliation
    WHERE reconciliation.cluster_id = ANY(%(cluster_ids)s::TEXT[])
      AND reconciliation.report_date BETWEEN
            %(start_date)s::DATE AND %(end_date)s::DATE
    GROUP BY reconciliation.cluster_id, reconciliation.report_date
), withdrawal AS (
    SELECT
        event.cluster_id,
        event.report_date,
        SUM(event.withdrawal_amount) AS withdrawal_amount,
        COUNT(*)::BIGINT AS withdrawal_count
    FROM linkaja_withdrawal_events_v AS event
    WHERE event.cluster_id = ANY(%(cluster_ids)s::TEXT[])
      AND event.report_date BETWEEN %(start_date)s::DATE AND %(end_date)s::DATE
    GROUP BY event.cluster_id, event.report_date
), exception AS (
    SELECT
        issue.cluster_id,
        issue.report_date,
        COUNT(*)::BIGINT AS exception_count,
        COUNT(*) FILTER (WHERE issue.severity = 'BLOCKING')::BIGINT
            AS blocking_exception_count
    FROM linkaja_reconciliation_exceptions_v AS issue
    WHERE issue.cluster_id = ANY(%(cluster_ids)s::TEXT[])
      AND issue.report_date BETWEEN %(start_date)s::DATE AND %(end_date)s::DATE
    GROUP BY issue.cluster_id, issue.report_date
)
SELECT
    cluster.cluster_id,
    day.report_date,
    COALESCE(withdrawal.withdrawal_amount, 0) AS withdrawal_amount,
    COALESCE(withdrawal.withdrawal_count, 0) AS withdrawal_count,
    COALESCE(fee.expected_out_cluster_fee, 0) AS expected_out_cluster_fee,
    COALESCE(fee.in_cluster_fee, 0) AS in_cluster_fee,
    COALESCE(fee.posted_digipos_fee, 0) AS posted_digipos_fee,
    CASE
        WHEN COALESCE(fee.has_incomplete_ppob_fee, FALSE) THEN fee.ppob_fee
        ELSE COALESCE(fee.ppob_fee, 0)
    END AS ppob_fee,
    COALESCE(fee.unresolved_reversal_count, 0)
        AS unresolved_reversal_count,
    COALESCE(fee.active_missing_fee_count, 0) AS active_missing_fee_count,
    COALESCE(fee.invalid_fee_count, 0) AS invalid_fee_count,
    COALESCE(exception.exception_count, 0) AS exception_count,
    COALESCE(exception.blocking_exception_count, 0)
        AS blocking_exception_count,
    CASE
        WHEN COALESCE(exception.blocking_exception_count, 0) > 0
            THEN 'REVIEW_REQUIRED'
        WHEN COALESCE(fee.has_incomplete_fee, FALSE)
            THEN 'INCOMPLETE_FEE'
        WHEN COALESCE(fee.unresolved_reversal_count, 0) > 0
            THEN 'UNRESOLVED_REVERSAL'
        ELSE 'CLEAR'
    END AS daily_status
FROM requested_dates AS day
CROSS JOIN requested_clusters AS cluster
LEFT JOIN fee
  ON fee.cluster_id = cluster.cluster_id
 AND fee.report_date = day.report_date
LEFT JOIN withdrawal
  ON withdrawal.cluster_id = cluster.cluster_id
 AND withdrawal.report_date = day.report_date
LEFT JOIN exception
  ON exception.cluster_id = cluster.cluster_id
 AND exception.report_date = day.report_date
ORDER BY day.report_date DESC, cluster.cluster_id
"""

MANDIRI_CYCLES_SQL = """
SELECT
    cluster_id,
    company_account,
    settlement_cycle_id,
    previous_withdrawal_transaction_id,
    withdrawal_transaction_id,
    interval_start_exclusive,
    interval_end_inclusive,
    opening_balance,
    non_withdrawal_company_credit,
    non_withdrawal_company_debit,
    withdrawal_company_debit,
    closing_balance,
    calculated_closing_balance,
    balance_variance,
    withdrawal_transaction_count,
    source_transaction_count,
    source_ledger_row_count,
    resolved_reversal_count,
    unresolved_reversal_count,
    source_fee_missing_count,
    source_files,
    latest_load_id,
    latest_updated_at,
    data_status,
    reversal_status,
    linkaja_reconciliation_status,
    mandiri_confirmation_status
FROM linkaja_mandiri_settlement_cycles_v
WHERE cluster_id = ANY(%(cluster_ids)s::TEXT[])
  AND interval_end_inclusive::DATE >= %(start_date)s::DATE
  AND COALESCE(interval_start_exclusive::DATE, interval_end_inclusive::DATE)
        <= %(end_date)s::DATE
ORDER BY interval_end_inclusive DESC, cluster_id, company_account
LIMIT %(row_limit)s
"""

EXCEPTIONS_SQL = """
SELECT
    cluster_id,
    report_date,
    finalized_at_local,
    transaction_id,
    exception_code,
    severity,
    detail,
    source_files,
    updated_load_id,
    updated_at
FROM linkaja_reconciliation_exceptions_v
WHERE cluster_id = ANY(%(cluster_ids)s::TEXT[])
  AND report_date BETWEEN %(start_date)s::DATE AND %(end_date)s::DATE
ORDER BY
    CASE severity WHEN 'BLOCKING' THEN 0 ELSE 1 END,
    finalized_at_local DESC,
    cluster_id,
    transaction_id
LIMIT %(row_limit)s
"""

TRANSACTIONS_SQL = """
SELECT
    cluster_id,
    report_date,
    finalized_at_local,
    transaction_id,
    transaction_scenario,
    company_account,
    company_debit,
    company_credit,
    ledger_debit_total,
    ledger_credit_total,
    signed_amount,
    source_fee,
    is_reversal,
    is_reversed,
    reversal_resolution_status,
    reversal_count,
    unusual_reason_codes,
    source_files,
    updated_load_id
FROM linkaja_transactions_current_v
WHERE cluster_id = ANY(%(cluster_ids)s::TEXT[])
  AND report_date BETWEEN %(start_date)s::DATE AND %(end_date)s::DATE
ORDER BY finalized_at_local DESC, cluster_id, transaction_id
LIMIT %(row_limit)s
"""

MONTHLY_CLOSE_SQL = """
SELECT
    report_month,
    calculation_cutoff,
    materialization_id,
    cluster_id,
    fee_category,
    gross_transaction_count,
    reversed_transaction_count,
    active_transaction_count,
    gross_fee,
    reversed_fee,
    net_fee,
    monthly_payable_fee,
    active_missing_fee_count,
    calculation_status
FROM linkaja_monthly_fee_summary_v
WHERE cluster_id = ANY(%(cluster_ids)s::TEXT[])
  AND report_month >= DATE_TRUNC('month', %(start_date)s::DATE)::DATE
  AND report_month <= DATE_TRUNC('month', %(end_date)s::DATE)::DATE
ORDER BY report_month DESC, cluster_id, fee_category
"""

FRESHNESS_SQL = """
SELECT DISTINCT ON (cluster_id)
    cluster_id,
    load_id,
    source_file,
    source_row_count,
    transaction_count,
    min_report_date,
    max_report_date,
    ingestion_completed_at,
    load_status
FROM linkaja_load_freshness_v
WHERE cluster_id = ANY(%(cluster_ids)s::TEXT[])
ORDER BY cluster_id, ingestion_completed_at DESC, load_id DESC
"""


QueryExecutor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class LinkAjaDashboardRepository:
    """Finance read model with hard bounds and no write method."""

    def __init__(
        self,
        dsn: str,
        *,
        max_date_span_days: int = 93,
        query_timeout_seconds: int = 30,
        query_executor: QueryExecutor | None = None,
    ) -> None:
        self._dsn = dsn
        self._max_date_span_days = max_date_span_days
        self._query_timeout_seconds = query_timeout_seconds
        self._query_executor = query_executor
        self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _get_pool(self):
        if self._pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "psycopg-pool is required for the LinkAja dashboard"
                ) from exc
            self._pool = ConnectionPool(
                conninfo=self._dsn,
                min_size=1,
                max_size=5,
                open=True,
                kwargs={"autocommit": False},
            )
        return self._pool

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        pool = self._get_pool()
        with pool.connection() as connection:
            yield connection

    def _execute(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self._query_executor is not None:
            return self._query_executor(sql, params)
        try:
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required for the LinkAja dashboard"
            ) from exc
        with self._connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT SET_CONFIG('statement_timeout', %s, TRUE)",
                        (str(self._query_timeout_seconds * 1000),),
                    )
                    cursor.execute(sql, params)
                    return [dict(row) for row in cursor.fetchall()]

    def _validated_range(self, start_date: date, end_date: date) -> None:
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("start_date and end_date must be dates")
        if start_date > end_date:
            raise ValueError("start date must not be after end date")
        days = (end_date - start_date).days + 1
        if days > self._max_date_span_days:
            raise ValueError(
                f"date range is limited to {self._max_date_span_days} days"
            )

    @staticmethod
    def _validated_clusters(cluster_ids: Iterable[str]) -> list[str]:
        values = sorted(
            {
                str(value).strip()
                for value in cluster_ids
                if str(value).strip()
            }
        )
        if not values:
            raise ValueError("select at least one cluster")
        if len(values) > 50:
            raise ValueError("too many clusters selected")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in values):
            raise ValueError("cluster ID contains unsupported characters")
        return values

    @staticmethod
    def _validated_fee_types(fee_types: Iterable[str] | None) -> list[str]:
        if fee_types is None:
            return list(FEE_TYPES)
        values = list(dict.fromkeys(str(value).strip() for value in fee_types))
        if not values or any(value not in FEE_TYPES for value in values):
            raise ValueError("fee type selection is invalid")
        return values

    def _params(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int | None = None,
    ) -> dict[str, Any]:
        self._validated_range(start_date, end_date)
        params: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "cluster_ids": self._validated_clusters(cluster_ids),
        }
        if row_limit is not None:
            if row_limit < 1 or row_limit > 5000:
                raise ValueError("row limit must be between 1 and 5000")
            params["row_limit"] = row_limit
        return params

    def available_clusters(self) -> list[str]:
        rows = self._execute(
            """
            SELECT DISTINCT cluster_id
            FROM linkaja_transactions_current_v
            ORDER BY cluster_id
            """,
            {},
        )
        return [str(row["cluster_id"]) for row in rows]

    def daily_fee_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        fee_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        params = self._params(start_date, end_date, cluster_ids)
        params["fee_types"] = self._validated_fee_types(fee_types)
        return self._execute(DAILY_FEE_SQL, params)

    def daily_summary_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        return self._execute(
            DAILY_SUMMARY_SQL,
            self._params(start_date, end_date, cluster_ids),
        )

    def mandiri_cycles(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self._execute(
            MANDIRI_CYCLES_SQL,
            self._params(
                start_date,
                end_date,
                cluster_ids,
                row_limit=row_limit,
            ),
        )

    def exceptions(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self._execute(
            EXCEPTIONS_SQL,
            self._params(
                start_date,
                end_date,
                cluster_ids,
                row_limit=row_limit,
            ),
        )

    def transactions(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self._execute(
            TRANSACTIONS_SQL,
            self._params(
                start_date,
                end_date,
                cluster_ids,
                row_limit=row_limit,
            ),
        )

    def monthly_close_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        return self._execute(
            MONTHLY_CLOSE_SQL,
            self._params(start_date, end_date, cluster_ids),
        )

    def freshness_rows(self, cluster_ids: Sequence[str]) -> list[dict[str, Any]]:
        clusters = self._validated_clusters(cluster_ids)
        return self._execute(FRESHNESS_SQL, {"cluster_ids": clusters})


def inclusive_end_to_exclusive(end_date: date) -> date:
    """Expose a tested helper for any future end-exclusive report query."""
    return end_date + timedelta(days=1)
