"""Pure cell-scoped daily calculations and contiguous reporting coverage."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .cell_contracts import (
    MAX_EXACT_RUPIAH, CellIdentity, business_date, configured_cells, fingerprint, parse_cell_amount,
    roster_fingerprint, validate_source_entry,
)
from .cell_database import prepare_cell_day
from .classification import (
    _collect_validation_unusual_transactions, preprocess_transaction_labels,
)
from .dedup import deduplicate_rows_by_minute_with_report
from .detail_exports import prepare_reversal_detail_export, prepare_transaction_detail_export
from .digipos_scraper import HEADERS
from .summary import summarize_by_transaction

RULES_VERSION = "finpay-cell-rules-v1-october-preprocessing"
TEMPLATE_VERSION = "finpay-cell-cashflow-v2"
MAKASSAR = ZoneInfo("Asia/Makassar")

# Legacy operational category semantics, excluding every invoice integration.
# October SLSFEE remains classified/stored, but is not added to the legacy-layout
# ST footer; the October change is preprocessing-only, not a new fee policy.
DETAIL_CATEGORIES = (
    ("TRANSFER MASUK DARI FINPAY", "CASHOUT APOLLO"),
    ("QRISDUWIT", "QRISDUWIT"),
    ("DISBURSEMENT", "DISBURSEMENT"),
    ("PPOB", "FEETRANSAKSI"),
    ("NGRS", "RECHARGE"),
    ("BIAYA FEE NGRS", "RECHARGEFEE"),
    ("Pembelian Recharge Out Cluster", "PEMBELIAN RECHARGE OUT CLUSTER"),
    ("RECHARGE OUT CLUSTER", "RECHARGE OUT CLUSTER"),
    ("RECHARGE OUT CLUSTER FEE", "RECHARGE OUT CLUSTER FEE"),
    ("REVERSAL NGRS", "REVERSAL - NGRS"),
    ("REVERSAL NGRS FEE", "REVERSAL - NGRS FEE"),
    ("Reversal - PEMBELIAN RECHARGE OUT CLUSTER", "REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER"),
    ("REVERSAL RECHARGE OUT CLUSTER", "REVERSAL - RECHARGE OUT CLUSTER"),
    ("REVERSAL RECHARGE OUT CLUSTER FEE", "REVERSAL - RECHARGE OUT CLUSTER FEE"),
    ("ST", "SELLTHRU"),
    ("BIAYA FEE ST", "SELLTHRUFEE"),
)
MANDIRI_GROUPS = (
    ("NGRS", ("RECHARGE", "RECHARGEFEE", "PEMBELIAN RECHARGE OUT CLUSTER")),
    ("Recharge Out Cluster", ("RECHARGE OUT CLUSTER", "RECHARGE OUT CLUSTER FEE")),
    ("Reversal - NGRS", ("REVERSAL - NGRS", "REVERSAL - NGRS FEE")),
    ("Reversal - Pembelian Recharge Out Cluster", ("REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER",)),
    ("Reversal - Recharge Out Cluster", ("REVERSAL - RECHARGE OUT CLUSTER", "REVERSAL - RECHARGE OUT CLUSTER FEE")),
    ("PPOB", ("FEETRANSAKSI",)),
    ("ST", ("SELLTHRU", "SELLTHRUFEE")),
    ("DISBURSEMENT", ("DISBURSEMENT",)),
    ("QRISDUWIT", ("QRISDUWIT",)),
)


@dataclass(frozen=True)
class CellReportConfig:
    account: str
    cell_codes: tuple[str, ...]
    first_report_date: str
    opening_boundary: str
    opening_balances: dict[str, int]
    roster_version: str
    rules_version: str = RULES_VERSION
    template_version: str = TEMPLATE_VERSION

    @property
    def cells(self):
        return configured_cells({self.account: self.cell_codes})

    def to_dict(self):
        first = business_date(self.first_report_date)
        cells = self.cells
        boundary = datetime.fromisoformat(self.opening_boundary)
        if boundary.tzinfo is None or boundary.astimezone(MAKASSAR) != datetime.combine(first, time(), MAKASSAR):
            raise ValueError("opening balance must be as-of the start boundary in Asia/Makassar")
        if set(self.opening_balances) != {cell.cell_code for cell in cells}:
            raise ValueError("explicit opening balance required for every cell")
        if any(type(value) is not int for value in self.opening_balances.values()):
            raise ValueError("opening balances must be integer rupiah, not implicit zero")
        if sum(abs(value) for value in self.opening_balances.values()) > MAX_EXACT_RUPIAH:
            raise ValueError("opening balances exceed safe spreadsheet arithmetic")
        if not self.roster_version or self.rules_version != RULES_VERSION or self.template_version != TEMPLATE_VERSION:
            raise ValueError("explicit supported rules/roster/template versions required")
        return {
            "account": self.account, "cells": [cell.to_dict() for cell in cells],
            "first_report_date": self.first_report_date,
            "opening_boundary": boundary.astimezone(MAKASSAR).isoformat(),
            "opening_balances": self.opening_balances.copy(),
            "roster_version": self.roster_version, "roster_fingerprint": roster_fingerprint(cells),
            "rules_version": self.rules_version, "template_version": self.template_version,
        }


def _frame(rows: list[dict], report_date: str) -> pd.DataFrame:
    expected = business_date(report_date)
    normalized = []
    for position, row in enumerate(rows, 1):
        if set(row) - {"No"} != set(HEADERS) - {"No"}:
            raise ValueError("cell source must have the exact transaction schema")
        values = dict(row)
        # No is an in-memory display/diagnostic counter; it is never persisted.
        values["No"] = position
        for column in ("Transaction ID", "Transaction Type", "Transaction", "Nomor RS", "Remarks"):
            if not isinstance(values[column], str):
                raise ValueError(f"source {column} must be exact text")
        if not values["Transaction ID"].strip():
            raise ValueError("empty source transaction identifier")
        parsed = None
        for pattern in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(values["Transaction Date"], pattern)
                break
            except (ValueError, TypeError):
                continue
        if parsed is None or parsed.date() != expected:
            raise ValueError("source transaction date differs from report date")
        values["Transaction Date"] = parsed
        for column in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir"):
            values[column] = parse_cell_amount(values[column], column)
            if abs(values[column]) > MAX_EXACT_RUPIAH:
                raise ValueError("source amount exceeds exact spreadsheet integer precision")
        if values["Kredit"] != 0 and values["Debet"] != 0:
            raise ValueError("cell source has simultaneous debit and credit")
        normalized.append(values)
    if sum(abs(row["Kredit"]) + abs(row["Debet"]) for row in normalized) > MAX_EXACT_RUPIAH:
        raise ValueError("source movement aggregate exceeds safe spreadsheet arithmetic")
    frame = pd.DataFrame(normalized, columns=HEADERS)
    for column in ("No", "Saldo Awal", "Kredit", "Debet", "Saldo Akhir"):
        frame[column] = frame[column].astype("int64")
    frame["Transaction Date"] = pd.to_datetime(frame["Transaction Date"])
    return frame


def _records(frame: pd.DataFrame, *, processed: bool = False) -> list[dict]:
    # Pandas JSON serialization preserves text and converts timestamps without
    # numpy scalars/NaN. Persistence must not include source No or duplicate label.
    cleaned = frame.drop(columns=["No"], errors="ignore").copy()
    if "Transaction ID" in cleaned:
        identifiers = cleaned["Transaction ID"].astype(str).str.strip()
        cleaned["base_id"] = identifiers.str.replace(r"(SLSHLD|SLSFEE|SALESFEE|FEE)$", "", regex=True)
        suffix = identifiers.str.extract(r"(SLSHLD|SLSFEE|SALESFEE|FEE)$", expand=False)
        cleaned["transaction_id_type"] = suffix.astype("string").fillna("MAIN").replace({"SLSHLD": "SLSFEE"})
    if processed and "Transaction" in cleaned:
        cleaned["processed_transaction_label"] = cleaned["Transaction"].str.lower()
        cleaned = cleaned.drop(columns="Transaction")
    for column in ("Transaction ID", "Remarks", "Transaction Type", "Nomor RS"):
        source_column = "_source_" + column
        if source_column in cleaned:
            cleaned[column] = cleaned[source_column]
            cleaned = cleaned.drop(columns=source_column)
    if "Transaction Date" in cleaned:
        cleaned["Transaction Date"] = cleaned["Transaction Date"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return json.loads(cleaned.to_json(orient="records"))


def calculate_cell_day(identity: CellIdentity, entry: dict, raw_rows: list[dict],
                       config: CellReportConfig) -> dict:
    configuration = config.to_dict()
    if identity not in config.cells:
        raise ValueError("cell is not in calculation configuration")
    report_date = entry.get("report_date")
    validate_source_entry(entry, identity, report_date)
    if business_date(report_date) < business_date(config.first_report_date):
        raise ValueError("source is before opening-balance boundary")
    if len(raw_rows) != entry["validated_rows"]:
        raise ValueError("source row count does not match manifest")
    raw = _frame(raw_rows, report_date)
    if raw.empty:
        preprocessed = deduplicated = unusual = summary_ready = qris = reversal = raw.copy()
        summary = []
    else:
        working = raw.copy()
        source_columns = []
        for column in ("Transaction ID", "Remarks", "Transaction Type", "Nomor RS"):
            name = "_source_" + column
            working[name] = raw[column]
            source_columns.append(name)
            working[column] = working[column].str.strip()
        working["Transaction"] = working["Transaction"].str.strip()
        preprocessed = preprocess_transaction_labels(working)
        preprocessed["raw_transaction_label"] = raw["Transaction"]
        preprocessed["processed_transaction_label"] = preprocessed["Transaction"]
        # Like legacy unusual detection, use a nonduplicate validation view and
        # preserve original duplicate evidence before selecting summary members.
        # Evaluate each family only once for unusual and summary membership.
        comparison = preprocessed.drop(columns=source_columns).copy()
        comparison["raw_transaction_label"] = comparison["raw_transaction_label"].str.strip()
        deduplicated, duplicates = deduplicate_rows_by_minute_with_report(
            preprocessed, comparison_frame=comparison,
        )
        validation_unusual, excluded, _ = _collect_validation_unusual_transactions(deduplicated)
        parts = [frame for frame in (validation_unusual, duplicates) if not frame.empty]
        unusual = pd.concat(parts, ignore_index=True, sort=False) if parts else validation_unusual
        summary_ready = deduplicated.drop(index=sorted(excluded)).reset_index(drop=True)
        qris = prepare_transaction_detail_export(summary_ready, "QRISDUWIT", True)
        reversal = prepare_reversal_detail_export(deduplicated)
        # Python integer objects prevent pandas int64 sum wrap before validation.
        aggregation = summary_ready.copy()
        aggregation[["Kredit", "Debet"]] = aggregation[["Kredit", "Debet"]].astype(object)
        summary = _records(summarize_by_transaction(aggregation))
    result = {
        "report_date": report_date,
        "processed": _records(preprocessed, processed=True),
        "deduplicated": _records(deduplicated, processed=True),
        "summary_ready": _records(summary_ready, processed=True),
        "unusual": _records(unusual, processed=True),
        "qris": _records(qris, processed=True), "reversal": _records(reversal, processed=True),
        "summary": summary,
        "raw_row_count": len(raw), "calculation_row_count": len(summary_ready),
        "duplicate_row_count": len(raw) - len(deduplicated),
    }
    return prepare_cell_day(identity, entry, _records(raw), result, configuration, config.rules_version)


def category_measures(summary: list[dict]) -> dict[str, dict]:
    measures = {}
    for row in summary:
        key = str(row["Transaction"]).strip().upper()
        if key in measures:
            raise ValueError("duplicate summary category")
        values = [row["Sum_of_Kredit"], row["Sum_of_Debet"], row["Transaction_Count"]]
        if any(type(value) is not int for value in values):
            raise ValueError("summary measures must be exact integers")
        measures[key] = {"debet": values[0], "kredit": values[1], "count": values[2]}
    return measures


def detail_categories(report_date: str):
    categories = list(DETAIL_CATEGORIES)
    if business_date(report_date) < business_date("2026-09-01"):
        categories.append(("BIAYA FEE BAR A ST (HOLD)", "SELLTHRUSALESFEE"))
    return tuple(categories)


def mandiri_measures(measures: dict, report_date: str) -> list[dict]:
    rows = []
    for label, base_keys in MANDIRI_GROUPS:
        keys = base_keys
        if label == "ST" and business_date(report_date) < business_date("2026-09-01"):
            keys = (*keys, "SELLTHRUSALESFEE")
        signed = sum(measures.get(key, {}).get("debet", 0) -
                     measures.get(key, {}).get("kredit", 0) for key in keys)
        rows.append({"label": label, "signed": signed,
                     "debet": max(signed, 0), "kredit": max(-signed, 0)})
    signed = sum(row["signed"] for row in rows)
    rows.append({"label": "Total", "signed": signed,
                 "debet": max(signed, 0), "kredit": max(-signed, 0)})
    return rows


def build_cluster_report(config: CellReportConfig, cell_days: list[dict], through_date: str) -> dict:
    """Produce a contiguous cell/combined report with no DB or Google calls."""
    configuration = config.to_dict()
    first, last = business_date(config.first_report_date), business_date(through_date)
    if last < first:
        raise ValueError("report range is before balance boundary")
    facts = {}
    known = {cell.cell_code for cell in config.cells}
    for item in cell_days:
        cell_code = item["identity"]["cell_code"]
        if item["identity"] not in [cell.to_dict() for cell in config.cells]:
            raise ValueError("report facts belong to another account/cell")
        if item["config_id"] != fingerprint(configuration):
            raise ValueError("report facts use a stale balance/roster/template configuration")
        if item["calculation_id"] != fingerprint({
            "generation_id": item["generation_id"], "config_id": item["config_id"],
            "rules_version": config.rules_version, "calculation": item["calculation"],
        }):
            raise ValueError("report calculation fingerprint mismatch")
        if item["calculation"]["report_date"] != item["report_date"]:
            raise ValueError("calculation report date mismatch")
        key = (item["report_date"], cell_code)
        if key in facts:
            raise ValueError("duplicate current cell/date facts")
        if not first <= business_date(item["report_date"]) <= last:
            raise ValueError("report facts outside requested coverage")
        facts[key] = item
    balances = config.opening_balances.copy()
    output = []
    day = first
    while day <= last:
        date_string = day.isoformat()
        if any((date_string, code) not in facts for code in known):
            raise ValueError(f"missing cell coverage for report date {date_string}")
        cells = []
        combined = {}
        for code in sorted(known):
            fact = facts[(date_string, code)]
            values = category_measures(fact["calculation"]["summary"])
            opening = balances[code]
            for _, key in detail_categories(date_string):
                measure = values.get(key, {})
                balances[code] += measure.get("debet", 0) - measure.get("kredit", 0)
            for key, measure in values.items():
                combined.setdefault(key, {"debet": 0, "kredit": 0, "count": 0})
                for field in ("debet", "kredit", "count"):
                    combined[key][field] += measure[field]
            cells.append({
                "cell_code": code, "measures": values,
                "mandiri": mandiri_measures(values, date_string),
                "opening_saldo": opening, "closing_saldo": balances[code],
                "calculation_id": fact["calculation_id"], "generation_id": fact["generation_id"],
            })
        output.append({
            "report_date": date_string, "cells": cells,
            "combined": {
                "measures": combined, "mandiri": mandiri_measures(combined, date_string),
                "opening_saldo": sum(cell["opening_saldo"] for cell in cells),
                "closing_saldo": sum(cell["closing_saldo"] for cell in cells),
            },
        })
        day += timedelta(days=1)
    return {"configuration": configuration, "through_date": through_date, "days": output}


def affected_publication_range(first_date: str, changed_dates: list[str], latest_date: str) -> tuple[str, str]:
    first, latest = business_date(first_date), business_date(latest_date)
    changed = [business_date(value) for value in changed_dates]
    if not changed or min(changed) < first or max(changed) > latest:
        raise ValueError("changed dates must be inside report coverage")
    return max(first, min(changed) - timedelta(days=1)).isoformat(), latest.isoformat()


def recalculate_stale_history(store, config: CellReportConfig, through_date: str,
                              *, submitted_dates: set[str]) -> list[list[dict]]:
    """Recalculate old configurations from immutable source, without new downloads.

    These complete date cohorts are prepared before any Google call. The caller
    selects them with the submitted cohort in one outer database transaction.
    """
    expected_config = fingerprint(config.to_dict())
    current = store.current_results(config.first_report_date, through_date)
    stale = {str(row["report_date"]) for row in current
             if row["config_id"] != expected_config and str(row["report_date"]) not in submitted_dates}
    if not stale:
        return []
    by_identity = {(cell.account, cell.cell_code): cell for cell in config.cells}
    cohorts = {day: [] for day in stale}
    for source in store.current_source_rows(min(stale), max(stale)):
        entry = source["manifest"]
        if entry["report_date"] not in stale:
            continue
        identity = by_identity.get((entry["account"], entry["cell_code"]))
        if identity is None:
            raise ValueError("historical roster change needs a separate cutover/mapping contract")
        raw = [{key: row[key] for key in HEADERS if key != "No"} for row in source["raw_rows"]]
        cohorts[entry["report_date"]].append(calculate_cell_day(identity, entry, raw, config))
    from .cell_database import validate_cluster_batch
    result = []
    for day in sorted(cohorts):
        validate_cluster_batch(cohorts[day], config.cells)
        result.append(cohorts[day])
    return result
