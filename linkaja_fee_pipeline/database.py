"""Atomic PostgreSQL ingestion and SQL summaries for LinkAja."""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .migrations import (
    acquire_linkaja_ingestion_locks,
    assert_linkaja_schema_current,
)
from .processing import (
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    EXPECTED_FEE_PER_ROW,
    IN_CLUSTER_FEE_PER_TRANSACTION,
    IN_CLUSTER_FEE_HEADER,
    RAW_NORMALIZED_COLUMNS,
    REPORT_DATE_HEADER,
    REVERSAL_IN_CLUSTER_FEE_PER_TRANSACTION,
    SOURCE_FILE_HEADER,
    SOURCE_ROWS_HEADER,
    TOTAL_FEE_HEADER,
    decimal_to_sheet_value,
)
from .sql import (
    AFFECTED_DATES_STATEMENT,
    CAPTURE_AFFECTED_DATES_STATEMENT,
    CREATE_MONTHLY_STAGE_STATEMENT,
    CREATE_REFRESH_SCOPE_STATEMENTS,
    CREATE_STAGE_STATEMENT,
    DAILY_SUMMARY_STATEMENT,
    DELETE_MONTH_SNAPSHOT_STATEMENT,
    EXPAND_IMPACTED_IDS_STATEMENT,
    FEE_SUMMARY_STATEMENT,
    INSERT_MONTHLY_STAGE_STATEMENT,
    LOAD_COUNTS_STATEMENT,
    MONTHLY_DAILY_RAW_STATEMENT,
    MONTHLY_FEE_SUMMARY_STATEMENT,
    MONTHLY_UNRESOLVED_REVERSALS_STATEMENT,
    PUBLISH_MONTH_SNAPSHOT_STATEMENT,
    REPLACE_RAW_STATEMENTS,
    REPLACE_SOURCE_LOAD_STATEMENTS,
    INSERT_LEDGER_VERSION_STATEMENT,
    INSERT_MONTHLY_IMPACTS_STATEMENT,
    STAGE_CONFLICTS_STATEMENT,
    UPSERT_MONTHLY_REFRESH_STATEMENT,
)


DETAIL_REPORT_DATE_HEADER = "REPORT DATE"
DETAIL_SECTION_HEADER = "SECTION"
DETAIL_LABEL_HEADER = "KETERANGAN"
DETAIL_DEBIT_HEADER = "DEBET"
DETAIL_CREDIT_HEADER = "KREDIT"
DETAIL_OTHER_HEADER = "OTHER"

DETAIL_HEADERS = [
    DETAIL_REPORT_DATE_HEADER,
    DETAIL_SECTION_HEADER,
    DETAIL_LABEL_HEADER,
    DETAIL_DEBIT_HEADER,
    DETAIL_CREDIT_HEADER,
    DETAIL_OTHER_HEADER,
]

MEASURE_SCENARIO_HEADER = "TRANSACTION SCENARIO"
MEASURE_COMPANY_DEBIT_HEADER = "COMPANY DEBIT"
MEASURE_COMPANY_CREDIT_HEADER = "COMPANY CREDIT"
MEASURE_SOURCE_FEE_HEADER = "SOURCE FEE"
MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER = "SOURCE FEE MISSING COUNT"
MEASURE_EXPECTED_FEE_HEADER = "EXPECTED FEE"
MEASURE_IN_CLUSTER_FEE_HEADER = "IN CLUSTER FEE"
MEASURE_REVERSAL_COUNT_HEADER = "REVERSAL IN CLUSTER COUNT"
MEASURE_REVERSAL_FEE_HEADER = "REVERSAL IN CLUSTER FEE"
MEASURE_UNRESOLVED_COUNT_HEADER = "UNRESOLVED REVERSAL COUNT"

DAILY_RAW_HEADERS = [
    "REPORT DATE",
    "TRANSACTION SCENARIO",
    "TRANSACTION COUNT",
    "SOURCE LEDGER ROWS",
    "LEDGER DEBIT",
    "LEDGER CREDIT",
    "SIGNED AMOUNT",
    "COMPANY DEBIT",
    "COMPANY CREDIT",
    "SOURCE FEE",
    "PPOB SOURCE FEE MISSING COUNT",
    "REVERSED ORIGINAL TRANSACTION COUNT",
    "COMPLETE REVERSAL COUNT",
    "UNRESOLVED REVERSAL COUNT",
]

UNRESOLVED_REVERSAL_HEADERS = [
    "REPORT DATE",
    "FINALIZED AT LOCAL",
    "REVERSAL TRANSACTION ID",
    "ORIGINAL TRANSACTION ID",
    "REVERSAL SCENARIO",
    "SOURCE LEDGER ROWS",
    "LEDGER DEBIT",
    "LEDGER CREDIT",
    "SIGNED AMOUNT",
    "COMPANY DEBIT",
    "COMPANY CREDIT",
    "SOURCE FEE",
    "COMPANY BALANCE",
    "BALANCE BEFORE",
    "SOURCE FILES",
    "UPDATED LOAD ID",
    "UNUSUAL REASON CODE",
]

DATE_COLUMNS = {"finalized_date", "initiate_date"}
TIME_COLUMNS = {"finalized_time", "initiate_time"}
DECIMAL_COLUMNS = {"debit", "credit", "balance", "fee"}
INTEGER_COLUMNS = {"source_row_number", "source_no"}


def linkaja_postgres_dsn_from_env(prefix: str = "FINPAY_DB_") -> str:
    host = os.environ.get(f"{prefix}HOST", "localhost")
    port = os.environ.get(f"{prefix}PORT", "5432")
    dbname = os.environ.get(f"{prefix}NAME", "finpay")
    user = os.environ.get(f"{prefix}USER", "finpay")
    password = os.environ.get(f"{prefix}PASSWORD", "")
    return (
        f"host={host} port={port} dbname={dbname} user={user} "
        f"password={password}"
    )


def update_linkaja_monthly_google_status(
    dsn: str,
    materialization_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Persist Google rendering status separately from the DB snapshot."""
    if status not in {"PENDING", "COMPLETE", "FAILED", "SKIPPED"}:
        raise ValueError(f"Unsupported LinkAja Google status: {status}")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for LinkAja Google status updates") from exc
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE linkaja_monthly_refreshes
                SET google_status = %s,
                    google_error = %s
                WHERE materialization_id = %s
                """,
                (status, error, materialization_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"LinkAja monthly materialization not found: {materialization_id}"
                )
        connection.commit()


def _normalized_copy_value(column: str, value: str):
    text = str(value or "").strip()
    if not text:
        return None
    if column in DATE_COLUMNS:
        return date.fromisoformat(text)
    if column in TIME_COLUMNS:
        return time.fromisoformat(text)
    if column in DECIMAL_COLUMNS:
        return Decimal(text)
    if column in INTEGER_COLUMNS:
        return int(text)
    return text


def _iter_normalized_rows(normalized_path: str | Path):
    with Path(normalized_path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != RAW_NORMALIZED_COLUMNS:
            raise ValueError("Normalized LinkAja CSV has an unexpected header")
        for row in reader:
            yield tuple(
                _normalized_copy_value(column, row.get(column, ""))
                for column in RAW_NORMALIZED_COLUMNS
            )


def _date_text(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _number(value: Any) -> int | float:
    return decimal_to_sheet_value(Decimal(str(value or 0)))


def _detail_rows(query_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            DETAIL_REPORT_DATE_HEADER: _date_text(row[0]),
            MEASURE_SCENARIO_HEADER: str(row[1] or ""),
            MEASURE_COMPANY_DEBIT_HEADER: _number(row[2]),
            MEASURE_COMPANY_CREDIT_HEADER: _number(row[3]),
            MEASURE_SOURCE_FEE_HEADER: _number(row[4]),
            MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER: int(row[5] or 0),
            MEASURE_EXPECTED_FEE_HEADER: _number(row[6]),
            MEASURE_IN_CLUSTER_FEE_HEADER: _number(row[7]),
            MEASURE_REVERSAL_COUNT_HEADER: int(row[8] or 0),
            MEASURE_REVERSAL_FEE_HEADER: _number(row[9]),
            MEASURE_UNRESOLVED_COUNT_HEADER: int(row[10] or 0),
        }
        for row in query_rows
    ]


def _fee_rows(query_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    rows = []
    for row in query_rows:
        expected_fee = Decimal(str(row[2] or 0))
        in_cluster_fee = Decimal(str(row[3] or 0))
        rows.append({
            REPORT_DATE_HEADER: _date_text(row[0]),
            CLUSTER_ID_HEADER: str(row[1]),
            EXPECTED_FEE_HEADER: decimal_to_sheet_value(expected_fee),
            IN_CLUSTER_FEE_HEADER: decimal_to_sheet_value(in_cluster_fee),
            TOTAL_FEE_HEADER: decimal_to_sheet_value(expected_fee + in_cluster_fee),
            SOURCE_ROWS_HEADER: int(row[4] or 0),
            SOURCE_FILE_HEADER: str(row[5] or ""),
        })
    return rows


def _optional_number(value: Any) -> int | float | None:
    if value is None:
        return None
    return _number(value)


def _monthly_fee_rows(query_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "REPORT MONTH": row[0].isoformat(),
            "CLUSTER ID": str(row[1]),
            "FEE CATEGORY": str(row[2]),
            "GROSS TRANSACTION COUNT": int(row[3] or 0),
            "REVERSED TRANSACTION COUNT": int(row[4] or 0),
            "ACTIVE TRANSACTION COUNT": int(row[5] or 0),
            "GROSS FEE": _optional_number(row[6]),
            "REVERSED FEE": _optional_number(row[7]),
            "NET FEE": _optional_number(row[8]),
            "MONTHLY PAYABLE FEE": _optional_number(row[9]),
            "GROSS MISSING FEE COUNT": int(row[10] or 0),
            "REVERSED MISSING FEE COUNT": int(row[11] or 0),
            "ACTIVE MISSING FEE COUNT": int(row[12] or 0),
            "CALCULATION STATUS": str(row[13]),
        }
        for row in query_rows
    ]


def _daily_raw_rows(query_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "REPORT DATE": _date_text(row[0]),
            "TRANSACTION SCENARIO": str(row[1]),
            "TRANSACTION COUNT": int(row[2] or 0),
            "SOURCE LEDGER ROWS": int(row[3] or 0),
            "LEDGER DEBIT": _number(row[4]),
            "LEDGER CREDIT": _number(row[5]),
            "SIGNED AMOUNT": _number(row[6]),
            "COMPANY DEBIT": _number(row[7]),
            "COMPANY CREDIT": _number(row[8]),
            "SOURCE FEE": _number(row[9]),
            "PPOB SOURCE FEE MISSING COUNT": int(row[10] or 0),
            "REVERSED ORIGINAL TRANSACTION COUNT": int(row[11] or 0),
            "COMPLETE REVERSAL COUNT": int(row[12] or 0),
            "UNRESOLVED REVERSAL COUNT": int(row[13] or 0),
        }
        for row in query_rows
    ]


def _local_datetime_text(value: datetime) -> str:
    return value.isoformat(sep=" ")


def _unresolved_reversal_rows(
    query_rows: list[tuple[Any, ...]],
) -> list[dict[str, Any]]:
    return [
        {
            "REPORT DATE": _date_text(row[0]),
            "FINALIZED AT LOCAL": _local_datetime_text(row[1]),
            "REVERSAL TRANSACTION ID": str(row[2]),
            "ORIGINAL TRANSACTION ID": str(row[3]),
            "REVERSAL SCENARIO": str(row[4]),
            "SOURCE LEDGER ROWS": int(row[5] or 0),
            "LEDGER DEBIT": _number(row[6]),
            "LEDGER CREDIT": _number(row[7]),
            "SIGNED AMOUNT": _number(row[8]),
            "COMPANY DEBIT": _number(row[9]),
            "COMPANY CREDIT": _number(row[10]),
            "SOURCE FEE": _optional_number(row[11]),
            "COMPANY BALANCE": _optional_number(row[12]),
            "BALANCE BEFORE": _optional_number(row[13]),
            "SOURCE FILES": str(row[14] or ""),
            "UPDATED LOAD ID": str(row[15]),
            "UNUSUAL REASON CODE": str(row[16]),
        }
        for row in query_rows
    ]


def _raise_stage_conflicts(conflicts: list[tuple[Any, ...]]) -> None:
    if not conflicts:
        return
    examples = []
    for row in conflicts[:5]:
        examples.append(
            f"{row[0]}/{row[1]} "
            f"(dates={row[2]}, times={row[3]}, scenarios={row[4]}, "
            f"statuses={row[5]}, types={row[6]}, originals={row[7]}, "
            f"fee_values={row[8]}, company_accounts={row[9]})"
        )
    raise ValueError(
        "LinkAja staged transactions violate the transaction-grain contract: "
        + "; ".join(examples)
    )


def persist_linkaja_normalized_csv(
    normalized_path: str | Path,
    manifest: dict[str, Any],
    dsn: str,
) -> dict[str, Any]:
    """Replace affected raw IDs and calculate linked daily summaries atomically."""
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("psycopg is required for LinkAja PostgreSQL persistence") from exc

    cluster_id = str(manifest.get("cluster_id", "")).strip()
    load_id = str(manifest.get("load_id", "")).strip()
    source_rows = int(manifest.get("source_rows", 0) or 0)
    if not cluster_id or not load_id:
        raise ValueError("LinkAja manifest must include cluster_id and load_id")

    copy_columns = sql.SQL(", ").join(sql.Identifier(column) for column in RAW_NORMALIZED_COLUMNS)
    copy_request = sql.SQL("COPY linkaja_raw_stage ({}) FROM STDIN").format(copy_columns)
    params = {
        "cluster_id": cluster_id,
        "load_id": load_id,
        "expected_fee_per_transaction": EXPECTED_FEE_PER_ROW,
        "in_cluster_fee_per_transaction": IN_CLUSTER_FEE_PER_TRANSACTION,
        "reversal_in_cluster_fee_per_transaction":
            REVERSAL_IN_CLUSTER_FEE_PER_TRANSACTION,
        "source_sha256": str(manifest.get("source_sha256", "")),
        "source_file": str(manifest.get("source_file", "")),
        "source_rows": source_rows,
        "source_start_date": min(manifest.get("report_dates", [None])),
        "source_end_date": max(manifest.get("report_dates", [None])),
    }

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            acquire_linkaja_ingestion_locks(cursor, cluster_id)
            assert_linkaja_schema_current(cursor)
            cursor.execute(CREATE_STAGE_STATEMENT)

            copied_rows = 0
            with cursor.copy(copy_request) as copy:
                for normalized_row in _iter_normalized_rows(normalized_path):
                    copy.write_row(normalized_row)
                    copied_rows += 1

            if copied_rows != source_rows:
                raise ValueError(
                    f"Normalized LinkAja row count {copied_rows} does not match manifest "
                    f"source_rows {source_rows}"
                )

            cursor.execute(
                "SELECT DISTINCT cluster_id FROM linkaja_raw_stage ORDER BY cluster_id"
            )
            staged_clusters = [str(row[0]) for row in cursor.fetchall()]
            if staged_clusters != [cluster_id]:
                raise ValueError(
                    "Normalized LinkAja rows do not match manifest cluster_id "
                    f"{cluster_id!r}: found {staged_clusters!r}"
                )

            cursor.execute(STAGE_CONFLICTS_STATEMENT)
            _raise_stage_conflicts(cursor.fetchall())

            for statement in REPLACE_SOURCE_LOAD_STATEMENTS:
                cursor.execute(statement, params)
            cursor.execute(INSERT_LEDGER_VERSION_STATEMENT)

            for statement in CREATE_REFRESH_SCOPE_STATEMENTS:
                cursor.execute(statement)
            cursor.execute(EXPAND_IMPACTED_IDS_STATEMENT)
            cursor.execute(CAPTURE_AFFECTED_DATES_STATEMENT)

            for statement in REPLACE_RAW_STATEMENTS:
                cursor.execute(statement)
            cursor.execute(EXPAND_IMPACTED_IDS_STATEMENT)
            cursor.execute(CAPTURE_AFFECTED_DATES_STATEMENT)
            cursor.execute(INSERT_MONTHLY_IMPACTS_STATEMENT, params)

            cursor.execute(AFFECTED_DATES_STATEMENT, params)
            affected_dates = [row[0] for row in cursor.fetchall()]

            cursor.execute(LOAD_COUNTS_STATEMENT, params)
            raw_rows, impacted_ids, transaction_rows = cursor.fetchone()

            summary_params = {
                **params,
                "report_dates": affected_dates,
            }
            if affected_dates:
                cursor.execute(DAILY_SUMMARY_STATEMENT, summary_params)
                detail_rows = _detail_rows(cursor.fetchall())
                cursor.execute(FEE_SUMMARY_STATEMENT, summary_params)
                fee_rows = _fee_rows(cursor.fetchall())
            else:
                detail_rows = []
                fee_rows = []

        connection.commit()

    if int(raw_rows or 0) != source_rows:
        raise ValueError(
            f"Persisted LinkAja raw row count {raw_rows} does not match source_rows "
            f"{source_rows}"
        )

    return {
        "cluster_id": cluster_id,
        "source_file": str(manifest.get("source_file", "")),
        "source_rows": source_rows,
        "load_id": load_id,
        "report_dates": [_date_text(value) for value in affected_dates],
        "raw_rows": int(raw_rows or 0),
        "impacted_transaction_ids": int(impacted_ids or 0),
        "transaction_rows": int(transaction_rows or 0),
        "fee_rows": fee_rows,
        "detail_rows": detail_rows,
    }


def _parse_report_month(value: str | date) -> date:
    if isinstance(value, date):
        report_month = value
    else:
        text = str(value or "").strip()
        if len(text) == 7:
            text = f"{text}-01"
        try:
            report_month = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "LinkAja report_month must use YYYY-MM or YYYY-MM-01"
            ) from exc
    if report_month.day != 1:
        raise ValueError("LinkAja report_month must be the first day of a month")
    return report_month


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _parse_local_cutoff(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        cutoff = value
    else:
        text = str(value or "").strip()
        try:
            cutoff = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "LinkAja calculation_cutoff must be an ISO local timestamp"
            ) from exc
    if cutoff.tzinfo is not None and cutoff.utcoffset() is not None:
        raise ValueError(
            "LinkAja calculation_cutoff must be local Asia/Makassar time "
            "without a UTC offset"
        )
    return cutoff


def materialize_linkaja_month(
    dsn: str,
    cluster_id: str,
    report_month: str | date,
    calculation_cutoff: str | datetime,
    materialization_id: str,
    *,
    publish: bool = False,
) -> dict[str, Any]:
    """Preview or atomically replace one cluster-month transaction snapshot."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for LinkAja monthly materialization"
        ) from exc

    cluster_id = str(cluster_id or "").strip()
    materialization_id = str(materialization_id or "").strip()
    if not cluster_id or not materialization_id:
        raise ValueError(
            "LinkAja monthly materialization requires cluster_id and "
            "materialization_id"
        )

    month_start = _parse_report_month(report_month)
    month_end = _next_month(month_start)
    cutoff = _parse_local_cutoff(calculation_cutoff)
    if cutoff < datetime.combine(month_end, time.min):
        raise ValueError(
            "LinkAja calculation_cutoff must be on or after the next month"
        )

    params = {
        "cluster_id": cluster_id,
        "month_start": month_start,
        "month_end": month_end,
        "calculation_cutoff": cutoff,
        "materialization_id": materialization_id,
    }

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            acquire_linkaja_ingestion_locks(cursor, cluster_id)
            assert_linkaja_schema_current(cursor)
            cursor.execute(CREATE_MONTHLY_STAGE_STATEMENT)
            cursor.execute(INSERT_MONTHLY_STAGE_STATEMENT, params)

            cursor.execute("SELECT COUNT(*) FROM linkaja_month_snapshot_stage")
            transaction_rows = int(cursor.fetchone()[0] or 0)

            cursor.execute(MONTHLY_DAILY_RAW_STATEMENT)
            daily_rows = _daily_raw_rows(cursor.fetchall())

            cursor.execute(MONTHLY_UNRESOLVED_REVERSALS_STATEMENT, params)
            unresolved_reversal_rows = _unresolved_reversal_rows(
                cursor.fetchall()
            )
            unresolved_reversal_count = len(unresolved_reversal_rows)

            cursor.execute(MONTHLY_FEE_SUMMARY_STATEMENT, params)
            fee_rows = _monthly_fee_rows(cursor.fetchall())
            incomplete_fee_rows = sum(
                row["CALCULATION STATUS"] != "COMPLETE"
                for row in fee_rows
            )

            if publish:
                cursor.execute(DELETE_MONTH_SNAPSHOT_STATEMENT, params)
                cursor.execute(PUBLISH_MONTH_SNAPSHOT_STATEMENT)
                cursor.execute(
                    UPSERT_MONTHLY_REFRESH_STATEMENT,
                    {
                        **params,
                        "transaction_rows": transaction_rows,
                        "unresolved_reversal_count":
                            unresolved_reversal_count,
                    },
                )

        connection.commit()

    return {
        "cluster_id": cluster_id,
        "report_month": month_start.isoformat(),
        "calculation_cutoff": cutoff.isoformat(sep=" "),
        "materialization_id": materialization_id,
        "published": bool(publish),
        "transaction_rows": transaction_rows,
        "daily_rows": daily_rows,
        "unresolved_reversal_count": unresolved_reversal_count,
        "unresolved_reversal_rows": unresolved_reversal_rows,
        "incomplete_fee_rows": incomplete_fee_rows,
        "monthly_payable_complete": incomplete_fee_rows == 0,
        "fee_rows": fee_rows,
    }


def write_linkaja_database_result(result: dict[str, Any], output_path: str | Path) -> None:
    Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_result_rows_csv(
    rows: list[dict[str, Any]],
    headers: list[str],
    output_path: str | Path,
) -> None:
    with Path(output_path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_linkaja_monthly_artifacts(
    result: dict[str, Any],
    *,
    result_path: str | Path,
    daily_path: str | Path,
    unresolved_path: str | Path,
) -> None:
    """Write the monthly JSON plus database-only daily analysis CSVs."""
    write_linkaja_database_result(result, result_path)
    _write_result_rows_csv(
        result.get("daily_rows", []),
        DAILY_RAW_HEADERS,
        daily_path,
    )
    _write_result_rows_csv(
        result.get("unresolved_reversal_rows", []),
        UNRESOLVED_REVERSAL_HEADERS,
        unresolved_path,
    )


def load_linkaja_database_result(input_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(input_path).read_text(encoding="utf-8"))
