"""Application service that turns read-model rows into finance-safe UI data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Protocol

from linkaja_fee_pipeline.dashboard.repository import FEE_TYPES


FEE_LABELS = {
    "EXPECTED_OUT_CLUSTER_RP200": "Expected out-cluster fee",
    "IN_CLUSTER_RP20": "In-cluster fee",
    "POSTED_DIGIPOS_FEE": "Posted Digipos fee",
    "PPOB_AGENT_TELCO_FEE": "PPOB / agent-telco fee",
}


class DashboardRepository(Protocol):
    def available_clusters(self) -> list[str]: ...

    def daily_fee_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        fee_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def daily_summary_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
    ) -> list[dict[str, Any]]: ...

    def mandiri_cycles(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    def exceptions(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    def transactions(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
        *,
        row_limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    def monthly_close_rows(
        self,
        start_date: date,
        end_date: date,
        cluster_ids: Iterable[str],
    ) -> list[dict[str, Any]]: ...

    def freshness_rows(
        self,
        cluster_ids: list[str],
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DashboardFilters:
    start_date: date
    end_date: date
    cluster_ids: tuple[str, ...]
    fee_types: tuple[str, ...] = FEE_TYPES


@dataclass
class DashboardSnapshot:
    daily_rows: list[dict[str, Any]] = field(default_factory=list)
    fee_rows: list[dict[str, Any]] = field(default_factory=list)
    fee_trend_rows: list[dict[str, Any]] = field(default_factory=list)
    mandiri_rows: list[dict[str, Any]] = field(default_factory=list)
    exception_rows: list[dict[str, Any]] = field(default_factory=list)
    transaction_rows: list[dict[str, Any]] = field(default_factory=list)
    monthly_rows: list[dict[str, Any]] = field(default_factory=list)
    freshness_rows: list[dict[str, Any]] = field(default_factory=list)
    expected_out_fee: str = "Rp0"
    in_cluster_fee: str = "Rp0"
    posted_digipos_fee: str = "Rp0"
    ppob_fee: str = "Rp0"
    withdrawal_total: str = "Rp0"
    exception_count: str = "0"
    blocking_exception_count: str = "0"
    reconciliation_status: str = "Clear"
    refreshed_at: str = "Not loaded"
    range_label: str = ""


class LinkAjaDashboardService:
    """Loads one internally consistent, read-only dashboard snapshot."""

    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    def available_clusters(self) -> list[str]:
        return self._repository.available_clusters()

    def load(self, filters: DashboardFilters) -> DashboardSnapshot:
        selected_fee_types = set(filters.fee_types)
        fee_rows = self._repository.daily_fee_rows(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
            FEE_TYPES,
        )
        daily_rows = self._repository.daily_summary_rows(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
        )
        mandiri_rows = self._repository.mandiri_cycles(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
        )
        exception_rows = self._repository.exceptions(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
        )
        transaction_rows = self._repository.transactions(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
        )
        monthly_rows = self._repository.monthly_close_rows(
            filters.start_date,
            filters.end_date,
            filters.cluster_ids,
        )
        freshness_rows = self._repository.freshness_rows(
            list(filters.cluster_ids)
        )

        fee_totals: dict[str, Decimal | None] = {
            fee_type: Decimal("0") for fee_type in FEE_TYPES
        }
        incomplete_fee_types: set[str] = set()
        for row in fee_rows:
            fee_type = str(row["fee_type"])
            if str(row.get("calculation_status", "COMPLETE")) != "COMPLETE":
                incomplete_fee_types.add(fee_type)
            amount = _decimal_or_none(row.get("active_fee"))
            if amount is not None and fee_totals[fee_type] is not None:
                fee_totals[fee_type] += amount

        withdrawal_total = sum(
            (_decimal_or_zero(row.get("withdrawal_amount")) for row in daily_rows),
            Decimal("0"),
        )
        blocking_count = sum(
            1
            for row in exception_rows
            if str(row.get("severity")) == "BLOCKING"
        )
        status = _overall_status(
            daily_rows,
            mandiri_rows,
            blocking_count,
        )

        filtered_fee_rows = [
            row for row in fee_rows if row["fee_type"] in selected_fee_types
        ]
        return DashboardSnapshot(
            daily_rows=[_daily_display(row) for row in daily_rows],
            fee_rows=[_fee_display(row) for row in filtered_fee_rows],
            fee_trend_rows=_fee_trend(filtered_fee_rows),
            mandiri_rows=[_mandiri_display(row) for row in mandiri_rows],
            exception_rows=[_exception_display(row) for row in exception_rows],
            transaction_rows=[
                _transaction_display(row) for row in transaction_rows
            ],
            monthly_rows=[_monthly_display(row) for row in monthly_rows],
            freshness_rows=[
                _freshness_display(row) for row in freshness_rows
            ],
            expected_out_fee=_fee_kpi(
                fee_totals,
                incomplete_fee_types,
                "EXPECTED_OUT_CLUSTER_RP200",
            ),
            in_cluster_fee=_fee_kpi(
                fee_totals,
                incomplete_fee_types,
                "IN_CLUSTER_RP20",
            ),
            posted_digipos_fee=_fee_kpi(
                fee_totals,
                incomplete_fee_types,
                "POSTED_DIGIPOS_FEE",
            ),
            ppob_fee=_fee_kpi(
                fee_totals,
                incomplete_fee_types,
                "PPOB_AGENT_TELCO_FEE",
            ),
            withdrawal_total=format_idr(withdrawal_total),
            exception_count=format_count(len(exception_rows)),
            blocking_exception_count=format_count(blocking_count),
            reconciliation_status=status,
            refreshed_at=datetime.now().astimezone().strftime(
                "%d %b %Y, %H:%M %Z"
            ),
            range_label=(
                f"{filters.start_date:%d %b %Y} - "
                f"{filters.end_date:%d %b %Y}"
            ),
        )


def _overall_status(
    daily_rows: list[dict[str, Any]],
    mandiri_rows: list[dict[str, Any]],
    blocking_count: int,
) -> str:
    if blocking_count:
        return "Review required"
    if any(
        str(row.get("daily_status")) not in {"CLEAR", "None"}
        for row in daily_rows
    ):
        return "Review required"
    if any(
        str(row.get("linkaja_reconciliation_status")) == "BALANCE_MISMATCH"
        for row in mandiri_rows
    ):
        return "Balance mismatch"
    if any(
        str(row.get("mandiri_confirmation_status"))
        == "PENDING_BANK_EVIDENCE"
        for row in mandiri_rows
    ):
        return "Awaiting Mandiri"
    return "Clear"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal_or_zero(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def format_idr(value: Any) -> str:
    amount = _decimal_or_none(value)
    if amount is None:
        return "Incomplete"
    rounded = amount.quantize(Decimal("1"))
    sign = "-" if rounded < 0 else ""
    digits = f"{abs(int(rounded)):,}".replace(",", ".")
    return f"{sign}Rp{digits}"


def format_count(value: Any) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def _fee_kpi(
    totals: dict[str, Decimal | None],
    incomplete: set[str],
    fee_type: str,
) -> str:
    if fee_type in incomplete:
        return "Incomplete"
    return format_idr(totals[fee_type])


def _date_label(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)


def _daily_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Date": _date_label(row.get("report_date")),
        "Cluster": row.get("cluster_id"),
        "Withdrawal": format_idr(row.get("withdrawal_amount")),
        "Out-cluster fee": format_idr(row.get("expected_out_cluster_fee")),
        "In-cluster fee": format_idr(row.get("in_cluster_fee")),
        "Posted fee": format_idr(row.get("posted_digipos_fee")),
        "PPOB fee": format_idr(row.get("ppob_fee")),
        "Exceptions": int(row.get("exception_count") or 0),
        "Status": str(row.get("daily_status") or "CLEAR").replace("_", " "),
    }


def _fee_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Date": _date_label(row.get("report_date")),
        "Cluster": row.get("cluster_id"),
        "Fee type": FEE_LABELS.get(str(row.get("fee_type")), row.get("fee_type")),
        "Gross count": int(row.get("gross_transaction_count") or 0),
        "Reversed count": int(row.get("reversed_transaction_count") or 0),
        "Active count": int(row.get("active_transaction_count") or 0),
        "Gross fee": format_idr(row.get("gross_fee")),
        "Reversed fee": format_idr(row.get("reversed_fee")),
        "Active fee": format_idr(row.get("active_fee")),
        "Missing": int(row.get("active_missing_fee_count") or 0),
        "Status": str(row.get("calculation_status") or "COMPLETE").replace(
            "_", " "
        ),
    }


def _fee_trend(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        report_date = _date_label(row.get("report_date"))
        point = by_date.setdefault(
            report_date,
            {"Date": report_date, "Active fee": 0.0, "Reversed fee": 0.0},
        )
        point["Active fee"] += float(_decimal_or_zero(row.get("active_fee")))
        point["Reversed fee"] += float(
            _decimal_or_zero(row.get("reversed_fee"))
        )
    return list(by_date.values())


def _mandiri_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Cluster": row.get("cluster_id"),
        "Purchase account": row.get("company_account"),
        "Cycle end": _date_label(row.get("interval_end_inclusive")),
        "Opening": format_idr(row.get("opening_balance")),
        "Credits": format_idr(row.get("non_withdrawal_company_credit")),
        "Debits": format_idr(row.get("non_withdrawal_company_debit")),
        "Withdrawal": format_idr(row.get("withdrawal_company_debit")),
        "Closing": format_idr(row.get("closing_balance")),
        "Variance": format_idr(row.get("balance_variance")),
        "LinkAja": str(row.get("linkaja_reconciliation_status") or ""),
        "Mandiri": str(row.get("mandiri_confirmation_status") or ""),
        "Reversal": str(row.get("reversal_status") or ""),
    }


def _exception_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Date": _date_label(row.get("finalized_at_local")),
        "Cluster": row.get("cluster_id"),
        "Transaction ID": row.get("transaction_id"),
        "Severity": row.get("severity"),
        "Issue": str(row.get("exception_code") or "").replace("_", " "),
        "Detail": row.get("detail"),
        "Source": row.get("source_files"),
    }


def _transaction_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Date": _date_label(row.get("finalized_at_local")),
        "Cluster": row.get("cluster_id"),
        "Transaction ID": row.get("transaction_id"),
        "Scenario": row.get("transaction_scenario"),
        "Account": row.get("company_account"),
        "Debit": format_idr(row.get("company_debit")),
        "Credit": format_idr(row.get("company_credit")),
        "Fee": format_idr(row.get("source_fee")),
        "Reversal": "Yes" if row.get("is_reversal") else "No",
        "Reversed": "Yes" if row.get("is_reversed") else "No",
        "Resolution": row.get("reversal_resolution_status"),
        "Source": row.get("source_files"),
    }


def _monthly_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Month": _date_label(row.get("report_month")),
        "Cluster": row.get("cluster_id"),
        "Fee category": row.get("fee_category"),
        "Active count": int(row.get("active_transaction_count") or 0),
        "Net fee": format_idr(row.get("net_fee")),
        "Monthly payable": format_idr(row.get("monthly_payable_fee")),
        "Missing": int(row.get("active_missing_fee_count") or 0),
        "Status": row.get("calculation_status"),
        "Cutoff": _date_label(row.get("calculation_cutoff")),
    }


def _freshness_display(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Cluster": row.get("cluster_id"),
        "Source file": row.get("source_file"),
        "Transactions": int(row.get("transaction_count") or 0),
        "Posting range": (
            f"{_date_label(row.get('min_report_date'))} - "
            f"{_date_label(row.get('max_report_date'))}"
        ),
        "Loaded": _date_label(row.get("ingestion_completed_at")),
        "Status": row.get("load_status"),
    }
