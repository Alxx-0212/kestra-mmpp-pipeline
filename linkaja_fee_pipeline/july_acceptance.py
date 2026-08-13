"""Read-only full-bundle acceptance analysis for the LinkAja monthly close.

This module mirrors the production ledger-to-transaction and exact-ID reversal
rules without connecting to PostgreSQL. It uses a disposable SQLite database
only as a bounded aggregation engine for large source bundles.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .processing import LINKAJA_SOURCE_COLUMNS, cluster_id_from_filename


IN_CLUSTER_SCENARIO = "Digipos B2B Transfer In Cluster"
OUT_CLUSTER_SCENARIO = "Digipos B2B Transfer"
DIGIPOS_FEE_SCENARIO = "Digipos B2B Transfer Fee"
PPOB_SCENARIO = "General to Purchase B2B Transfer Agent Telco"


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _parse_month(value: str) -> date:
    text = str(value).strip()
    if len(text) == 7:
        text += "-01"
    month = date.fromisoformat(text)
    if month.day != 1:
        raise ValueError("report month must use YYYY-MM or YYYY-MM-01")
    return month


def _parse_cutoff(value: str) -> datetime:
    cutoff = datetime.fromisoformat(str(value).strip())
    if cutoff.tzinfo is not None and cutoff.utcoffset() is not None:
        raise ValueError("cutoff must be an offset-free WITA local timestamp")
    return cutoff


def _source_date(value: Any, source: str, row_number: int) -> str:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"{source}:{row_number}: invalid Finalized Date {value!r}"
        ) from exc


def _source_time(value: Any, source: str, row_number: int) -> str:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%H:%M:%S").time().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"{source}:{row_number}: invalid Finalized Time {value!r}"
        ) from exc


def _minor_amount(
    value: Any,
    source: str,
    row_number: int,
    column: str,
    *,
    blank_is_null: bool = False,
) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None if blank_is_null else 0
    normalized = text.replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1]
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(
            f"{source}:{row_number}: invalid {column} amount {value!r}"
        ) from exc
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError(
            f"{source}:{row_number}: {column} has more than two decimals"
        )
    return int(minor)


def _currency(minor: int | None) -> int | str | None:
    if minor is None:
        return None
    value = Decimal(int(minor)) / 100
    if value == value.to_integral_value():
        return int(value)
    return format(value, "f")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _export_order(path: Path) -> tuple[int, str]:
    match = re.search(r"-(\d+)\.csv$", path.name, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"LinkAja filename has no numeric export token: {path.name}")
    return int(match.group(1)), path.name


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute("PRAGMA cache_size = -131072")
    connection.executescript(
        """
        CREATE TABLE raw (
            cluster_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            original_transaction_id TEXT,
            finalized_date TEXT NOT NULL,
            finalized_time TEXT NOT NULL,
            transaction_type TEXT,
            transaction_scenario TEXT NOT NULL,
            transaction_status TEXT,
            top_organization TEXT,
            organization TEXT,
            account TEXT,
            debit_minor INTEGER NOT NULL,
            credit_minor INTEGER NOT NULL,
            fee_minor INTEGER
        );
        CREATE INDEX raw_transaction_idx
            ON raw (cluster_id, transaction_id);

        CREATE TABLE stage AS SELECT * FROM raw WHERE 0;
        CREATE INDEX stage_transaction_idx
            ON stage (cluster_id, transaction_id);
        """
    )
    return connection


def _validate_header(fieldnames: Iterable[str] | None, source: str) -> None:
    actual = [str(column).strip() for column in (fieldnames or [])]
    if actual != LINKAJA_SOURCE_COLUMNS:
        raise ValueError(f"{source}: source columns do not match LinkAja contract")


def _load_file(
    connection: sqlite3.Connection,
    path: Path,
) -> dict[str, Any]:
    cluster_id = cluster_id_from_filename(path.name)
    if not cluster_id:
        raise ValueError(f"cannot determine cluster ID from {path.name}")

    connection.execute("DELETE FROM stage")
    insert = """
        INSERT INTO stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    batch: list[tuple[Any, ...]] = []
    transaction_contracts: dict[str, dict[str, Any]] = {}
    source_rows = 0
    min_date: str | None = None
    max_date: str | None = None

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_header(reader.fieldnames, path.name)
        for row_number, row in enumerate(reader, start=2):
            source_rows += 1
            top_organization = str(row.get("Top Organization") or "").strip()
            top_match = re.match(r"\s*(\d+)-", top_organization)
            if top_match and top_match.group(1) != cluster_id:
                raise ValueError(
                    f"{path.name}:{row_number}: Top Organization cluster "
                    f"{top_match.group(1)} does not match {cluster_id}"
                )
            transaction_id = str(row.get("Transaction ID") or "").strip()
            scenario = str(row.get("Transaction Scenario") or "").strip()
            if not transaction_id or not scenario:
                raise ValueError(
                    f"{path.name}:{row_number}: transaction ID/scenario is blank"
                )
            finalized_date = _source_date(
                row.get("Finalized Date"), path.name, row_number
            )
            finalized_time = _source_time(
                row.get("Finalized Time"), path.name, row_number
            )
            min_date = finalized_date if min_date is None else min(min_date, finalized_date)
            max_date = finalized_date if max_date is None else max(max_date, finalized_date)
            original_id = str(row.get("Original Transaction ID") or "").strip()
            transaction_type = str(row.get("Transaction Type") or "").strip()
            status = str(row.get("Transaction Status") or "").strip()
            organization = str(row.get("Organization") or "").strip()
            account = str(row.get("Account") or "").strip()
            fee_minor = _minor_amount(
                row.get("Fee"),
                path.name,
                row_number,
                "Fee",
                blank_is_null=True,
            )

            contract = transaction_contracts.setdefault(
                transaction_id,
                {
                    "dates": set(),
                    "times": set(),
                    "scenarios": set(),
                    "statuses": set(),
                    "types": set(),
                    "originals": set(),
                    "fees": set(),
                    "company_accounts": set(),
                },
            )
            contract["dates"].add(finalized_date)
            contract["times"].add(finalized_time)
            contract["scenarios"].add(scenario)
            contract["statuses"].add(status.lower().strip())
            contract["types"].add(transaction_type or None)
            contract["originals"].add(original_id or None)
            if fee_minor is not None:
                contract["fees"].add(fee_minor)
            is_company = (
                organization == top_organization
                and "organization mfs purchase account" in account.lower()
            )
            if is_company:
                contract["company_accounts"].add(account)

            batch.append(
                (
                    cluster_id,
                    path.name,
                    transaction_id,
                    original_id or None,
                    finalized_date,
                    finalized_time,
                    transaction_type or None,
                    scenario,
                    status or None,
                    top_organization or None,
                    organization or None,
                    account or None,
                    _minor_amount(row.get("Debit"), path.name, row_number, "Debit"),
                    _minor_amount(row.get("Credit"), path.name, row_number, "Credit"),
                    fee_minor,
                )
            )
            if len(batch) >= 10_000:
                connection.executemany(insert, batch)
                batch.clear()
        if batch:
            connection.executemany(insert, batch)

    if not source_rows:
        raise ValueError(f"{path.name}: no source rows")
    conflicts = []
    for transaction_id, contract in transaction_contracts.items():
        if any(
            len(contract[field]) > 1
            for field in (
                "dates",
                "times",
                "scenarios",
                "statuses",
                "types",
                "originals",
                "fees",
                "company_accounts",
            )
        ):
            conflicts.append(transaction_id)
    if conflicts:
        raise ValueError(
            f"{path.name}: transaction-grain conflicts: {conflicts[:5]}"
        )

    connection.execute(
        """
        DELETE FROM raw
        WHERE EXISTS (
            SELECT 1
            FROM stage
            WHERE stage.cluster_id = raw.cluster_id
              AND stage.transaction_id = raw.transaction_id
        )
        """
    )
    connection.execute("INSERT INTO raw SELECT * FROM stage")
    connection.commit()
    return {
        "cluster_id": cluster_id,
        "source_file": path.name,
        "sha256": _sha256(path),
        "source_rows": source_rows,
        "transaction_ids": len(transaction_contracts),
        "min_finalized_date": min_date,
        "max_finalized_date": max_date,
    }


def _build_facts(
    connection: sqlite3.Connection,
    month_start: date,
    month_end: date,
    cutoff: datetime,
) -> None:
    company_predicate = """
        organization = top_organization
        AND INSTR(LOWER(account), 'organization mfs purchase account') > 0
    """
    connection.executescript(
        f"""
        CREATE TABLE facts AS
        SELECT
            cluster_id,
            transaction_id,
            finalized_date AS report_date,
            MIN(finalized_date || ' ' || finalized_time) AS finalized_at,
            MAX(NULLIF(TRIM(original_transaction_id), '')) AS original_transaction_id,
            MAX(transaction_scenario) AS transaction_scenario,
            COUNT(*) AS source_ledger_row_count,
            SUM(debit_minor) AS ledger_debit_minor,
            SUM(credit_minor) AS ledger_credit_minor,
            MAX(fee_minor) FILTER (WHERE fee_minor IS NOT NULL) AS source_fee_minor,
            COUNT(DISTINCT fee_minor) FILTER (WHERE fee_minor IS NOT NULL)
                AS source_fee_value_count,
            COALESCE(SUM(debit_minor) FILTER (WHERE {company_predicate}), 0)
                AS company_debit_minor,
            COALESCE(SUM(credit_minor) FILTER (WHERE {company_predicate}), 0)
                AS company_credit_minor
        FROM raw
        WHERE LOWER(TRIM(COALESCE(transaction_status, ''))) = 'completed'
        GROUP BY cluster_id, transaction_id, finalized_date;

        CREATE INDEX facts_transaction_idx
            ON facts (cluster_id, transaction_id);
        CREATE INDEX facts_date_idx
            ON facts (report_date, finalized_at);
        """
    )
    connection.execute(
        "CREATE TABLE cutoff_facts AS SELECT * FROM facts WHERE finalized_at < ?",
        (cutoff.isoformat(sep=" "),),
    )
    connection.executescript(
        """
        CREATE INDEX cutoff_transaction_idx
            ON cutoff_facts (cluster_id, transaction_id);
        CREATE INDEX cutoff_original_idx
            ON cutoff_facts (cluster_id, original_transaction_id);

        CREATE TABLE original_info AS
        SELECT
            cluster_id,
            transaction_id,
            COUNT(DISTINCT transaction_scenario) AS scenario_count,
            MAX(transaction_scenario) AS original_scenario,
            MIN(report_date) AS original_report_date,
            MIN(finalized_at) AS original_finalized_at
        FROM cutoff_facts
        GROUP BY cluster_id, transaction_id;
        CREATE UNIQUE INDEX original_info_transaction_idx
            ON original_info (cluster_id, transaction_id);

        CREATE TABLE closing_reversals AS
        SELECT
            reversal.cluster_id,
            reversal.transaction_id AS reversal_transaction_id,
            reversal.original_transaction_id,
            reversal.report_date AS reversal_report_date,
            reversal.finalized_at AS reversal_finalized_at,
            reversal.transaction_scenario AS reversal_scenario,
            reversal.source_ledger_row_count,
            reversal.ledger_debit_minor,
            reversal.ledger_credit_minor,
            reversal.company_debit_minor,
            reversal.company_credit_minor,
            original.scenario_count,
            original.original_scenario,
            original.original_report_date,
            original.original_finalized_at
        FROM cutoff_facts AS reversal
        LEFT JOIN original_info AS original
          ON original.cluster_id = reversal.cluster_id
         AND original.transaction_id = reversal.original_transaction_id
        WHERE reversal.original_transaction_id IS NOT NULL
          AND reversal.finalized_at >= '"""
        + month_start.isoformat()
        + " 00:00:00';\n"
        + """
        CREATE INDEX closing_reversal_original_idx
            ON closing_reversals (cluster_id, original_transaction_id);
        """
    )
    connection.execute(
        """
        CREATE TABLE month_facts AS
        SELECT
            fact.*,
            CASE WHEN fact.original_transaction_id IS NULL THEN 0 ELSE 1 END
                AS is_reversal,
            CASE WHEN EXISTS (
                SELECT 1
                FROM closing_reversals AS reversal
                WHERE reversal.cluster_id = fact.cluster_id
                  AND reversal.original_transaction_id = fact.transaction_id
            ) THEN 1 ELSE 0 END AS is_reversed
        FROM cutoff_facts AS fact
        WHERE fact.report_date >= ?
          AND fact.report_date < ?
        """,
        (month_start.isoformat(), month_end.isoformat()),
    )
    connection.execute(
        "CREATE INDEX month_facts_cluster_scenario_idx "
        "ON month_facts (cluster_id, transaction_scenario)"
    )
    connection.commit()


def _where_cluster(cluster_id: str | None) -> tuple[str, list[Any]]:
    if cluster_id is None:
        return "", []
    return " AND cluster_id = ?", [cluster_id]


def _fixed_fee_metrics(
    connection: sqlite3.Connection,
    scenario: str,
    fee_per_transaction: int,
    cluster_id: str | None,
    *,
    require_company_credit: bool = False,
    require_source_fee: int | None = None,
) -> dict[str, Any]:
    cluster_sql, cluster_params = _where_cluster(cluster_id)
    predicates = ["transaction_scenario = ?", "is_reversal = 0"]
    params: list[Any] = [scenario]
    if require_company_credit:
        predicates.append("company_credit_minor > 0")
    if require_source_fee is not None:
        predicates.append("source_fee_minor = ?")
        params.append(require_source_fee * 100)
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS gross_count,
            COALESCE(SUM(is_reversed), 0) AS reversed_count,
            COALESCE(SUM(CASE WHEN is_reversed = 0 THEN 1 ELSE 0 END), 0)
                AS active_count
        FROM month_facts
        WHERE {' AND '.join(predicates)}{cluster_sql}
        """,
        [*params, *cluster_params],
    ).fetchone()
    gross, reversed_count, active = (int(value or 0) for value in row)
    return {
        "gross_transaction_count": gross,
        "reversed_transaction_count": reversed_count,
        "active_transaction_count": active,
        "gross_fee": gross * fee_per_transaction,
        "reversed_fee": reversed_count * fee_per_transaction,
        "active_fee": active * fee_per_transaction,
    }


def _ppob_metrics(
    connection: sqlite3.Connection,
    cluster_id: str | None,
) -> dict[str, Any]:
    cluster_sql, cluster_params = _where_cluster(cluster_id)
    row = connection.execute(
        f"""
        SELECT
            COUNT(*),
            COALESCE(SUM(is_reversed), 0),
            COALESCE(SUM(CASE WHEN is_reversed = 0 THEN 1 ELSE 0 END), 0),
            SUM(CASE WHEN source_fee_minor IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN is_reversed = 1 AND source_fee_minor IS NULL
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN is_reversed = 0 AND source_fee_minor IS NULL
                     THEN 1 ELSE 0 END),
            COALESCE(SUM(source_fee_minor), 0),
            COALESCE(SUM(CASE WHEN is_reversed = 1 THEN source_fee_minor END), 0),
            COALESCE(SUM(CASE WHEN is_reversed = 0 THEN source_fee_minor END), 0)
        FROM month_facts
        WHERE transaction_scenario = ?
          AND is_reversal = 0{cluster_sql}
        """,
        [PPOB_SCENARIO, *cluster_params],
    ).fetchone()
    (
        gross,
        reversed_count,
        active,
        gross_missing,
        reversed_missing,
        active_missing,
        gross_minor,
        reversed_minor,
        active_minor,
    ) = (int(value or 0) for value in row)
    missing_query = f"""
        SELECT cluster_id, transaction_id, report_date
        FROM month_facts
        WHERE transaction_scenario = ?
          AND is_reversal = 0
          AND is_reversed = 0
          AND source_fee_minor IS NULL{cluster_sql}
        ORDER BY cluster_id, report_date, transaction_id
    """
    active_missing_transactions = [
        {
            "cluster_id": str(row[0]),
            "transaction_id": str(row[1]),
            "report_date": str(row[2]),
        }
        for row in connection.execute(
            missing_query,
            [PPOB_SCENARIO, *cluster_params],
        )
    ]
    return {
        "gross_transaction_count": gross,
        "reversed_transaction_count": reversed_count,
        "active_transaction_count": active,
        "gross_missing_fee_count": gross_missing,
        "reversed_missing_fee_count": reversed_missing,
        "active_missing_fee_count": active_missing,
        "active_missing_fee_transactions": active_missing_transactions,
        "known_gross_fee": _currency(gross_minor),
        "known_reversed_fee": _currency(reversed_minor),
        "known_active_fee": _currency(active_minor),
        "gross_fee": None if gross_missing else _currency(gross_minor),
        "reversed_fee": None if reversed_missing else _currency(reversed_minor),
        "active_fee": None if active_missing else _currency(active_minor),
        "calculation_status": (
            "INCOMPLETE_MISSING_FEE" if active_missing else "COMPLETE"
        ),
    }


def _daily_rows(
    connection: sqlite3.Connection,
    cluster_id: str,
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for scenario, fee, key, require_credit in (
        (IN_CLUSTER_SCENARIO, 20, "in_cluster", False),
        (OUT_CLUSTER_SCENARIO, 200, "out_cluster_expected", True),
    ):
        credit_predicate = " AND company_credit_minor > 0" if require_credit else ""
        query_rows = connection.execute(
            f"""
            SELECT
                report_date,
                COUNT(*) AS gross_count,
                COALESCE(SUM(is_reversed), 0) AS reversed_count,
                COALESCE(SUM(CASE WHEN is_reversed = 0 THEN 1 ELSE 0 END), 0)
                    AS active_count
            FROM month_facts
            WHERE cluster_id = ?
              AND transaction_scenario = ?
              AND is_reversal = 0{credit_predicate}
            GROUP BY report_date
            ORDER BY report_date
            """,
            (cluster_id, scenario),
        )
        for report_date, gross, reversed_count, active in query_rows:
            target = rows.setdefault(report_date, {"report_date": report_date})
            target[f"{key}_gross_transaction_count"] = int(gross)
            target[f"{key}_reversed_transaction_count"] = int(reversed_count)
            target[f"{key}_active_transaction_count"] = int(active)
            target[f"{key}_active_fee"] = int(active) * fee
    defaults = {
        f"{key}_{measure}": 0
        for key in ("in_cluster", "out_cluster_expected")
        for measure in (
            "gross_transaction_count",
            "reversed_transaction_count",
            "active_transaction_count",
            "active_fee",
        )
    }
    return [
        {**defaults, **rows[report_date]}
        for report_date in sorted(rows)
    ]


def _reversal_metrics(
    connection: sqlite3.Connection,
    month_start: date,
    month_end: date,
    cluster_id: str | None,
) -> dict[str, Any]:
    cluster_sql, cluster_params = _where_cluster(cluster_id)
    rows = connection.execute(
        f"""
        SELECT
            reversal_report_date,
            reversal_finalized_at,
            original_report_date,
            original_finalized_at,
            scenario_count,
            original_scenario
        FROM closing_reversals
        WHERE 1 = 1{cluster_sql}
        """,
        cluster_params,
    ).fetchall()
    metrics: Counter[str] = Counter()
    scenario_counts: Counter[str] = Counter()
    lag_days: Counter[int] = Counter()
    max_elapsed_seconds: int | None = None
    for (
        reversal_date,
        reversal_at,
        original_date,
        original_at,
        scenario_count,
        original_scenario,
    ) in rows:
        is_report_month = reversal_date < month_end.isoformat()
        resolved = int(scenario_count or 0) == 1
        metrics["total"] += 1
        metrics["report_month_reversal_count" if is_report_month else "closing_window_reversal_count"] += 1
        if not resolved:
            metrics["unresolved_count"] += 1
            metrics[
                "report_month_unresolved_count"
                if is_report_month
                else "closing_window_unresolved_count"
            ] += 1
            continue
        metrics["resolved_count"] += 1
        scenario_counts[str(original_scenario)] += 1
        if (
            not is_report_month
            and original_date is not None
            and month_start.isoformat() <= original_date < month_end.isoformat()
        ):
            metrics["relevant_late_reversal_count"] += 1
        if (
            not is_report_month
            and original_date is not None
            and original_date >= month_end.isoformat()
        ):
            metrics["next_month_reversal_count"] += 1
        if original_at is not None:
            elapsed = int(
                (
                    datetime.fromisoformat(reversal_at)
                    - datetime.fromisoformat(original_at)
                ).total_seconds()
            )
            max_elapsed_seconds = (
                elapsed if max_elapsed_seconds is None else max(max_elapsed_seconds, elapsed)
            )
            lag_days[
                (
                    date.fromisoformat(reversal_date)
                    - date.fromisoformat(original_date)
                ).days
            ] += 1
    stable_metrics = {
        "total": 0,
        "resolved_count": 0,
        "unresolved_count": 0,
        "report_month_reversal_count": 0,
        "closing_window_reversal_count": 0,
        "report_month_unresolved_count": 0,
        "closing_window_unresolved_count": 0,
        "relevant_late_reversal_count": 0,
        "next_month_reversal_count": 0,
    }
    stable_metrics.update({key: int(value) for key, value in metrics.items()})
    return {
        **stable_metrics,
        "resolved_by_original_scenario": dict(sorted(scenario_counts.items())),
        "calendar_day_lag_distribution": {
            str(key): value for key, value in sorted(lag_days.items())
        },
        "maximum_elapsed_seconds": max_elapsed_seconds,
    }


def _summary_for_cluster(
    connection: sqlite3.Connection,
    month_start: date,
    month_end: date,
    cluster_id: str | None,
) -> dict[str, Any]:
    return {
        "cluster_id": cluster_id or "ALL",
        "active_daily_fee_rules": {
            "digipos_b2b_transfer_in_cluster_rp20": _fixed_fee_metrics(
                connection,
                IN_CLUSTER_SCENARIO,
                20,
                cluster_id,
            ),
            "digipos_b2b_transfer_expected_rp200": _fixed_fee_metrics(
                connection,
                OUT_CLUSTER_SCENARIO,
                200,
                cluster_id,
                require_company_credit=True,
            ),
        },
        "monthly_fee_summary": {
            "digipos_b2b_transfer_fee": _fixed_fee_metrics(
                connection,
                DIGIPOS_FEE_SCENARIO,
                200,
                cluster_id,
                require_source_fee=200,
            ),
            "general_to_purchase_agent_telco": _ppob_metrics(
                connection,
                cluster_id,
            ),
        },
        "reversals": _reversal_metrics(
            connection,
            month_start,
            month_end,
            cluster_id,
        ),
        **(
            {"daily_rows": _daily_rows(connection, cluster_id)}
            if cluster_id is not None
            else {}
        ),
    }


def analyze_linkaja_july_bundle(
    source_directory: str | Path,
    *,
    report_month: str = "2026-07",
    calculation_cutoff: str = "2026-08-10T00:00:00",
) -> dict[str, Any]:
    """Analyze one source bundle using the current LinkAja close semantics."""
    directory = Path(source_directory)
    files = sorted(directory.glob("*.csv"), key=_export_order)
    if not files:
        raise ValueError(f"no LinkAja CSV files found under {directory}")
    month_start = _parse_month(report_month)
    month_end = _next_month(month_start)
    cutoff = _parse_cutoff(calculation_cutoff)
    if cutoff < datetime.combine(month_end, datetime.min.time()):
        raise ValueError("calculation cutoff must be on or after the next month")

    with tempfile.TemporaryDirectory(prefix="linkaja-july-acceptance-") as temp_dir:
        connection = _connect(Path(temp_dir) / "acceptance.sqlite3")
        manifests = [_load_file(connection, path) for path in files]
        _build_facts(connection, month_start, month_end, cutoff)
        cluster_ids = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT cluster_id FROM raw ORDER BY cluster_id"
            )
        ]
        result = {
            "report_month": month_start.isoformat(),
            "calculation_cutoff": cutoff.isoformat(sep=" "),
            "source_directory": str(directory),
            "file_count": len(manifests),
            "source_rows_read": sum(item["source_rows"] for item in manifests),
            "retained_raw_rows": int(
                connection.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
            ),
            "completed_transaction_facts": int(
                connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            ),
            "report_month_transaction_facts": int(
                connection.execute("SELECT COUNT(*) FROM month_facts").fetchone()[0]
            ),
            "cluster_ids": cluster_ids,
            "files": manifests,
            "aggregate": _summary_for_cluster(
                connection,
                month_start,
                month_end,
                None,
            ),
            "clusters": [
                _summary_for_cluster(
                    connection,
                    month_start,
                    month_end,
                    cluster_id,
                )
                for cluster_id in cluster_ids
            ],
        }
        connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory")
    parser.add_argument("--report-month", default="2026-07")
    parser.add_argument(
        "--calculation-cutoff",
        default="2026-08-10T00:00:00",
    )
    parser.add_argument("--output")
    arguments = parser.parse_args(argv)
    result = analyze_linkaja_july_bundle(
        arguments.source_directory,
        report_month=arguments.report_month,
        calculation_cutoff=arguments.calculation_cutoff,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.output:
        Path(arguments.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
