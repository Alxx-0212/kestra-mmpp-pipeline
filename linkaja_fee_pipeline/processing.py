"""LinkAja CSV normalization plus legacy fee aggregation helpers."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


LINKAJA_SOURCE_COLUMNS = [
    "No",
    "Top Organization",
    "Parent Organization",
    "Organization",
    "Transaction ID",
    "Original Transaction ID",
    "Partner Reference Number",
    "Invoice ID",
    "Finalized Date",
    "Finalized Time",
    "Initiate Date",
    "Initiate Time",
    "Transaction Type",
    "Transaction Scenario",
    "Transaction Status",
    "Transaction Statement",
    "Account",
    "Counter Party",
    "Debit",
    "Credit",
    "Balance",
    "Fee",
]

REQUIRED_COLUMNS = ("Finalized Date", "Transaction Scenario", "Debit", "Credit", "Fee")
EXPECTED_SCENARIO = "Digipos B2B Transfer"
IN_CLUSTER_SCENARIO = "Digipos B2B Transfer In Cluster"
EXPECTED_FEE_PER_ROW = Decimal("200")
PPOB_SCENARIO = "General to Purchase B2B Transfer Agent Telco"
DIGIPOS_FEE_SCENARIO = "Digipos B2B Transfer Fee"
DIGIPOS_FEE_PER_TRANSACTION = Decimal("200")
IN_CLUSTER_FEE_PER_TRANSACTION = Decimal("20")
REVERSAL_IN_CLUSTER_FEE_PER_TRANSACTION = Decimal("20")

RAW_NORMALIZED_COLUMNS = [
    "cluster_id",
    "source_file",
    "source_row_number",
    "load_id",
    "source_no",
    "top_organization",
    "parent_organization",
    "organization",
    "transaction_id",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "finalized_date",
    "finalized_time",
    "initiate_date",
    "initiate_time",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "transaction_statement",
    "account",
    "counter_party",
    "debit",
    "credit",
    "balance",
    "fee",
]

REPORT_DATE_HEADER = "REPORT DATE"
CLUSTER_ID_HEADER = "CLUSTER ID"
EXPECTED_FEE_HEADER = "EXPECTED RECHARGE OUT CLUSTER FEE"
IN_CLUSTER_FEE_HEADER = "DIGIPOS B2B TRANSFER IN CLUSTER FEE"
TOTAL_FEE_HEADER = "TOTAL FEE"
SOURCE_FILE_HEADER = "SOURCE FILE"
SOURCE_ROWS_HEADER = "SOURCE ROWS"

SHEET_HEADERS = [
    REPORT_DATE_HEADER,
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    TOTAL_FEE_HEADER,
    SOURCE_FILE_HEADER,
    SOURCE_ROWS_HEADER,
]


@dataclass(frozen=True)
class LinkAjaFeeSummary:
    cluster_id: str
    source_file: str
    source_rows: int
    report_dates: list[str]
    rows: list[dict[str, Any]]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "source_file": self.source_file,
            "source_rows": self.source_rows,
            "report_dates": self.report_dates,
            "rows": self.rows,
        }


@dataclass(frozen=True)
class LinkAjaLoadManifest:
    cluster_id: str
    source_file: str
    source_rows: int
    report_dates: list[str]
    scenario_counts: dict[str, int]
    load_id: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "source_file": self.source_file,
            "source_rows": self.source_rows,
            "report_dates": self.report_dates,
            "scenario_counts": self.scenario_counts,
            "load_id": self.load_id,
        }


def display_name(value: str | Path) -> str:
    text = str(value).rstrip("/")
    return text.rsplit("/", 1)[-1] or text


def cluster_id_from_filename(value: str | Path) -> str | None:
    match = re.search(r"laporan-(\d+)-", display_name(value))
    return match.group(1) if match else None


def cluster_id_from_rows(rows: Iterable[dict[str, Any]]) -> str | None:
    for row in rows:
        for column in ("Top Organization", "Parent Organization", "Organization"):
            value = str(row.get(column, "") or "")
            match = re.match(r"\s*(\d+)-", value)
            if match:
                return match.group(1)
    return None


def parse_amount(value: Any, source_name: str, row_number: int, column: str) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value):
            return Decimal("0")
        return Decimal(str(value))

    text = str(value).strip()
    if not text or text.lower() == "nan":
        return Decimal("0")

    normalized = text.replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1]

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(
            f"{source_name}:{row_number}: cannot parse {column} value {value!r} as a number"
        ) from exc


def normalize_report_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")

    text = str(value).strip()
    if not text:
        return ""

    for date_format in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, date_format).strftime("%d/%m/%Y")
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text).strftime("%d/%m/%Y")
    except ValueError:
        return text


def date_sort_key(value: str) -> tuple[int, Any]:
    for date_format in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return (0, datetime.strptime(value, date_format).date())
        except ValueError:
            pass
    return (1, value)


def decimal_to_sheet_value(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def load_linkaja_rows(
    source_path: str | Path,
    filename_hint: str | None = None,
) -> list[dict[str, Any]]:
    path = Path(source_path)
    suffix = path.suffix.lower() or Path(filename_hint or "").suffix.lower()

    if suffix == ".csv" or suffix == "":
        return _load_csv_rows(path)

    raise ValueError(f"Unsupported LinkAja input file extension: {suffix or '<none>'}")


def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_columns(reader.fieldnames or [], display_name(path))
        return [dict(row) for row in reader]


def _validate_columns(fieldnames: list[str], source_name: str) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{source_name}: missing required column(s): {missing_text}")


def _validate_exact_source_columns(fieldnames: list[str], source_name: str) -> None:
    normalized = [str(column).strip() for column in fieldnames]
    if normalized == LINKAJA_SOURCE_COLUMNS:
        return

    missing = [column for column in LINKAJA_SOURCE_COLUMNS if column not in normalized]
    unexpected = [column for column in normalized if column not in LINKAJA_SOURCE_COLUMNS]
    details = []
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected: {', '.join(unexpected)}")
    if not missing and not unexpected:
        details.append("columns are not in the expected order")
    raise ValueError(f"{source_name}: invalid LinkAja CSV header ({'; '.join(details)})")


def _strict_source_date(value: Any, source_name: str, row_number: int, column: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{source_name}:{row_number}: {column} is blank")
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"{source_name}:{row_number}: invalid {column} {value!r}; expected DD/MM/YYYY"
        ) from exc


def _optional_source_date(value: Any, source_name: str, row_number: int, column: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _strict_source_date(text, source_name, row_number, column)


def _strict_source_time(value: Any, source_name: str, row_number: int, column: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{source_name}:{row_number}: {column} is blank")
    try:
        return datetime.strptime(text, "%H:%M:%S").time().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"{source_name}:{row_number}: invalid {column} {value!r}; expected HH:MM:SS"
        ) from exc


def _optional_source_time(value: Any, source_name: str, row_number: int, column: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _strict_source_time(text, source_name, row_number, column)


def _decimal_text(
    value: Any,
    source_name: str,
    row_number: int,
    column: str,
    *,
    blank_is_null: bool = False,
) -> str:
    if blank_is_null and not str(value or "").strip():
        return ""
    return format(parse_amount(value, source_name, row_number, column), "f")


def _source_cluster_id(value: Any) -> str | None:
    match = re.match(r"\s*(\d+)-", str(value or ""))
    return match.group(1) if match else None


def normalize_linkaja_csv(
    source_path: str | Path,
    output_path: str | Path,
    *,
    filename_hint: str | None = None,
    load_id: str,
) -> LinkAjaLoadManifest:
    """Validate one LinkAja CSV and write a typed, DB-ready CSV artifact."""
    path = Path(source_path)
    source_name = display_name(filename_hint or source_path)
    suffix = Path(filename_hint or source_path).suffix.lower()
    if suffix not in {"", ".csv"}:
        raise ValueError(f"Unsupported LinkAja input file extension: {suffix}")
    if not str(load_id).strip():
        raise ValueError("LinkAja load_id must not be blank")

    cluster_from_filename = cluster_id_from_filename(filename_hint or source_path)
    cluster_id = cluster_from_filename
    source_rows = 0
    report_dates: set[str] = set()
    scenario_counts: Counter[str] = Counter()

    with path.open(newline="", encoding="utf-8-sig") as source_handle, Path(output_path).open(
        "w", newline="", encoding="utf-8"
    ) as output_handle:
        reader = csv.DictReader(source_handle)
        _validate_exact_source_columns(reader.fieldnames or [], source_name)
        writer = csv.DictWriter(output_handle, fieldnames=RAW_NORMALIZED_COLUMNS)
        writer.writeheader()

        for row_number, row in enumerate(reader, start=2):
            source_rows += 1
            top_cluster_id = _source_cluster_id(row.get("Top Organization"))
            if cluster_id is None:
                cluster_id = top_cluster_id
            if not cluster_id:
                raise ValueError(
                    f"{source_name}:{row_number}: cannot determine cluster ID from filename "
                    "or Top Organization"
                )
            if top_cluster_id and top_cluster_id != cluster_id:
                raise ValueError(
                    f"{source_name}:{row_number}: Top Organization cluster {top_cluster_id} "
                    f"does not match expected cluster {cluster_id}"
                )

            transaction_id = str(row.get("Transaction ID") or "").strip()
            if not transaction_id:
                raise ValueError(f"{source_name}:{row_number}: Transaction ID is blank")
            transaction_scenario = str(row.get("Transaction Scenario") or "").strip()
            if not transaction_scenario:
                raise ValueError(f"{source_name}:{row_number}: Transaction Scenario is blank")

            finalized_date = _strict_source_date(
                row.get("Finalized Date"), source_name, row_number, "Finalized Date"
            )
            finalized_time = _strict_source_time(
                row.get("Finalized Time"), source_name, row_number, "Finalized Time"
            )
            report_dates.add(finalized_date)
            scenario_counts[transaction_scenario] += 1

            writer.writerow({
                "cluster_id": cluster_id,
                "source_file": source_name,
                "source_row_number": row_number,
                "load_id": str(load_id).strip(),
                "source_no": str(row.get("No") or "").strip(),
                "top_organization": str(row.get("Top Organization") or "").strip(),
                "parent_organization": str(row.get("Parent Organization") or "").strip(),
                "organization": str(row.get("Organization") or "").strip(),
                "transaction_id": transaction_id,
                "original_transaction_id": str(
                    row.get("Original Transaction ID") or ""
                ).strip(),
                "partner_reference_number": str(
                    row.get("Partner Reference Number") or ""
                ).strip(),
                "invoice_id": str(row.get("Invoice ID") or "").strip(),
                "finalized_date": finalized_date,
                "finalized_time": finalized_time,
                "initiate_date": _optional_source_date(
                    row.get("Initiate Date"), source_name, row_number, "Initiate Date"
                ),
                "initiate_time": _optional_source_time(
                    row.get("Initiate Time"), source_name, row_number, "Initiate Time"
                ),
                "transaction_type": str(row.get("Transaction Type") or "").strip(),
                "transaction_scenario": transaction_scenario,
                "transaction_status": str(row.get("Transaction Status") or "").strip(),
                "transaction_statement": str(
                    row.get("Transaction Statement") or ""
                ).strip(),
                "account": str(row.get("Account") or "").strip(),
                "counter_party": str(row.get("Counter Party") or "").strip(),
                "debit": _decimal_text(
                    row.get("Debit"), source_name, row_number, "Debit"
                ),
                "credit": _decimal_text(
                    row.get("Credit"), source_name, row_number, "Credit"
                ),
                "balance": _decimal_text(
                    row.get("Balance"),
                    source_name,
                    row_number,
                    "Balance",
                    blank_is_null=True,
                ),
                "fee": _decimal_text(
                    row.get("Fee"),
                    source_name,
                    row_number,
                    "Fee",
                    blank_is_null=True,
                ),
            })

    if source_rows == 0:
        raise ValueError(f"{source_name}: no data rows found")
    if not cluster_id:
        raise ValueError(f"{source_name}: cannot determine cluster ID")

    return LinkAjaLoadManifest(
        cluster_id=cluster_id,
        source_file=source_name,
        source_rows=source_rows,
        report_dates=sorted(report_dates),
        scenario_counts=dict(sorted(scenario_counts.items())),
        load_id=str(load_id).strip(),
    )


def write_linkaja_manifest(
    manifest: LinkAjaLoadManifest,
    output_path: str | Path,
) -> None:
    Path(output_path).write_text(
        json.dumps(manifest.to_json_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_linkaja_manifest(input_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(input_path).read_text(encoding="utf-8"))


def aggregate_linkaja_fee_file(
    source_path: str | Path,
    filename_hint: str | None = None,
) -> LinkAjaFeeSummary:
    rows = load_linkaja_rows(source_path, filename_hint=filename_hint)
    source_name = display_name(filename_hint or source_path)
    cluster_id = cluster_id_from_filename(filename_hint or source_path) or cluster_id_from_rows(rows)

    if not cluster_id:
        raise ValueError(
            f"{source_name}: cannot determine cluster ID from filename or organization columns"
        )

    expected_counts: dict[str, int] = defaultdict(int)
    in_cluster_fees: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    source_rows_by_date: dict[str, int] = defaultdict(int)

    for row_index, row in enumerate(rows, start=2):
        report_date = normalize_report_date(row.get("Finalized Date"))
        if not report_date:
            raise ValueError(f"{source_name}:{row_index}: Finalized Date is blank")

        parse_amount(row.get("Debit"), source_name, row_index, "Debit")
        credit = parse_amount(row.get("Credit"), source_name, row_index, "Credit")
        fee = parse_amount(row.get("Fee"), source_name, row_index, "Fee")
        scenario = str(row.get("Transaction Scenario", "") or "").strip()

        source_rows_by_date[report_date] += 1
        if scenario == EXPECTED_SCENARIO and credit != 0:
            expected_counts[report_date] += 1
        if scenario == IN_CLUSTER_SCENARIO:
            in_cluster_fees[report_date] += fee

    report_dates = sorted(source_rows_by_date, key=date_sort_key)
    output_rows = []
    for report_date in report_dates:
        expected_fee = Decimal(expected_counts.get(report_date, 0)) * EXPECTED_FEE_PER_ROW
        in_cluster_fee = in_cluster_fees.get(report_date, Decimal("0"))
        total_fee = expected_fee + in_cluster_fee
        output_rows.append({
            REPORT_DATE_HEADER: report_date,
            CLUSTER_ID_HEADER: cluster_id,
            EXPECTED_FEE_HEADER: decimal_to_sheet_value(expected_fee),
            IN_CLUSTER_FEE_HEADER: decimal_to_sheet_value(in_cluster_fee),
            TOTAL_FEE_HEADER: decimal_to_sheet_value(total_fee),
            SOURCE_FILE_HEADER: source_name,
            SOURCE_ROWS_HEADER: source_rows_by_date[report_date],
        })

    return LinkAjaFeeSummary(
        cluster_id=cluster_id,
        source_file=source_name,
        source_rows=len(rows),
        report_dates=report_dates,
        rows=output_rows,
    )


def write_linkaja_fee_json(summary: LinkAjaFeeSummary, output_path: str | Path) -> None:
    Path(output_path).write_text(
        json.dumps(summary.to_json_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_linkaja_fee_json(input_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(input_path).read_text(encoding="utf-8"))
