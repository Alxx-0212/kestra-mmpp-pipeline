"""Reversal-aware FinPay monthly preview and publication helpers."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg

from .classification import prepare_reversal_summary_transactions, preprocess_transaction_labels
from .detail_exports import extract_disbursement_date_from_remarks
from .database import ensure_finpay_monthly_model
from .sheets_common import (
    _add_protected_sheet_request,
    _delete_all_banded_range_requests,
    _delete_all_protected_range_requests,
    _grid_range,
    ensure_row_capacity,
    open_or_create_finpay_spreadsheet,
    uppercase_sheet_rows,
)


MONTHLY_STATUS_PROVISIONAL = "PROVISIONAL"
MONTHLY_STATUS_INCOMPLETE_SOURCE = "INCOMPLETE_SOURCE"
MONTHLY_STATUS_INCOMPLETE_REVERSALS = "INCOMPLETE_REVERSALS"
MONTHLY_STATUS_READY = "READY_FOR_APPROVAL"
MONTHLY_STATUS_FINAL = "FINAL"
MONTHLY_STATUS_RESTATEMENT_REQUIRED = "RESTATEMENT_REQUIRED"
MONTHLY_STATUS_SUPERSEDED = "SUPERSEDED"
CLASSIFICATION_VERSION = "finpay-classification-v2"


def _month_bounds(report_month: str | date) -> tuple[date, date]:
    parsed = pd.to_datetime(report_month, errors="raise").date()
    start = parsed.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _calendar_dates(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days)}


def _cutoff_timestamp(calculation_cutoff: str | datetime) -> pd.Timestamp:
    parsed = pd.to_datetime(calculation_cutoff, errors="raise")
    if pd.isna(parsed):
        raise ValueError("calculation_cutoff must be a valid timestamp")
    if parsed.tzinfo is None:
        return parsed.tz_localize("Asia/Makassar").tz_localize(None)
    return parsed.tz_convert(ZoneInfo("Asia/Makassar")).tz_localize(None)


def _event_id(cluster_id: str, transaction_id: str, role: str, timestamp) -> str:
    value = f"{cluster_id}:{transaction_id}:{role}:{pd.Timestamp(timestamp).isoformat()}"
    return hashlib.sha256(value.encode()).hexdigest()


def _category_for_label(label: str) -> str:
    normalized = str(label or "").strip().upper()
    result = {
        "RECHARGE": "NGRS_PRINCIPAL",
        "RECHARGEFEE": "NGRS_FEE",
        "PEMBELIAN RECHARGE OUT CLUSTER": "OUT_CLUSTER_PRINCIPAL",
        "PEMBELIAN RECHARGE OUT CLUSTER FEE": "OUT_CLUSTER_FEE",
        "RECHARGE OUT CLUSTER": "OUT_CLUSTER_PRINCIPAL",
        "RECHARGE OUT CLUSTER FEE": "OUT_CLUSTER_FEE",
        "QRISDUWIT": "QRISDUWIT",
    }.get(normalized, normalized or "UNKNOWN")
    return result


def _as_monthly_input(events: pd.DataFrame) -> pd.DataFrame:
    """Convert current ledger-view columns to the classification contract."""
    result = events.copy()
    result["Transaction Date"] = pd.to_datetime(
        result["transaction_date"], errors="coerce"
    )
    result["event_timestamp"] = result["Transaction Date"]
    result["Transaction ID"] = result["transaction_id"].astype(str)
    result["Transaction"] = result["raw_transaction_label"].fillna("")
    result["raw_transaction_label"] = result["Transaction"]
    result["Transaction Type"] = result.get("transaction_type", "")
    result["Remarks"] = result["remarks"].fillna("")
    result["Kredit"] = pd.to_numeric(result["kredit"], errors="coerce").fillna(0)
    result["Debet"] = pd.to_numeric(result["debet"], errors="coerce").fillna(0)
    result["Saldo Awal"] = result.get("saldo_awal", 0)
    result["Saldo Akhir"] = result.get("saldo_akhir", 0)
    result["Nomor RS"] = result.get("nomor_rs", "")
    result["No"] = result.get("source_row_number", range(1, len(result) + 1))
    result["__monthly_row_id"] = range(len(result))
    return result


def _group_events(
    rows: pd.DataFrame,
    *,
    cluster_id: str,
) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "event_id", "cluster_id", "transaction_id", "event_role",
                "event_timestamp", "signed_amount", "kredit_total", "debet_total",
                "raw_labels", "processed_labels", "remarks", "eligible",
                "category", "source_load_ids", "disbursement_dates",
            ]
        )

    source = rows.copy()
    source["event_role"] = source["raw_transaction_label"].fillna("").astype(str).str.strip().str.upper().eq("REVERSAL").map({True: "REVERSAL", False: "ORIGINAL"})
    source["event_timestamp"] = pd.to_datetime(source["Transaction Date"], errors="coerce")
    source["signed_amount"] = source["Kredit"] - source["Debet"]
    source["eligible_row"] = source["__monthly_row_id"].isin(
        set(rows.loc[rows["__eligible"], "__monthly_row_id"])
    )
    grouped_rows = []
    group_columns = ["Transaction ID", "event_role", "event_timestamp"]
    for (transaction_id, event_role, timestamp), group in source.groupby(
        group_columns, dropna=False, sort=False
    ):
        labels = sorted(set(group["Transaction"].astype(str)))
        processed_labels = sorted(set(group["Transaction"].astype(str)))
        category = _category_for_label(processed_labels[0] if processed_labels else "")
        disbursement_dates = sorted(
            set(
                group.loc[group["Transaction"].astype(str).str.upper().eq("QRISDUWIT"), "Remarks"]
                .map(extract_disbursement_date_from_remarks)
            )
        )
        valid_disbursement_dates = [
            pd.to_datetime(value, dayfirst=True, errors="coerce").date()
            for value in disbursement_dates
            if value
        ]
        if category == "QRISDUWIT" and len(set(valid_disbursement_dates)) == 1:
            settlement_date = valid_disbursement_dates[0]
        elif category == "QRISDUWIT":
            settlement_date = None
        else:
            settlement_date = pd.Timestamp(timestamp).date()
        grouped_rows.append({
            "event_id": _event_id(cluster_id, str(transaction_id), event_role, timestamp),
            "cluster_id": cluster_id,
            "transaction_id": str(transaction_id),
            "event_role": event_role,
            "event_timestamp": timestamp,
            "signed_amount": float(group["signed_amount"].sum()),
            "kredit_total": float(group["Kredit"].sum()),
            "debet_total": float(group["Debet"].sum()),
            "raw_labels": labels,
            "processed_labels": processed_labels,
            "remarks": sorted(set(group["Remarks"].astype(str))),
            "eligible": bool(group["eligible_row"].all()),
            "category": category,
            "settlement_date": settlement_date,
            "source_load_ids": sorted(set(group.get("load_id", pd.Series(dtype=str)).astype(str))),
            "disbursement_dates": disbursement_dates,
        })
    return pd.DataFrame(grouped_rows)


def _deduplicate_monthly_rows(rows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove exact source replays before monthly event grouping."""
    if rows.empty:
        return rows, 0
    source = rows.copy()
    source["__event_role"] = (
        source["raw_transaction_label"].fillna("").astype(str).str.strip().str.upper()
        .eq("REVERSAL").map({True: "REVERSAL", False: "ORIGINAL"})
    )
    source["__source_hash"] = source.get(
        "source_row_hash",
        pd.Series("", index=source.index),
    )
    missing_hash = source["__source_hash"].isna() | source["__source_hash"].eq("")
    if missing_hash.any():
        source.loc[missing_hash, "__source_hash"] = source.loc[missing_hash].apply(
            lambda row: hashlib.sha256(
                json.dumps(
                    {
                        "transaction_id": str(row.get("Transaction ID", "")),
                        "transaction_date": str(row.get("Transaction Date", "")),
                        "transaction": str(row.get("Transaction", "")),
                        "remarks": str(row.get("Remarks", "")),
                        "kredit": str(row.get("Kredit", "")),
                        "debet": str(row.get("Debet", "")),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            axis=1,
        )
    source["__load_order"] = source.get(
        "load_id",
        pd.Series("", index=source.index),
    ).astype(str)
    source = source.sort_values("__load_order")
    duplicate_mask = source.duplicated(
        ["cluster_id", "Transaction ID", "__event_role", "Transaction Date", "__source_hash"],
        keep="last",
    )
    duplicate_count = int(duplicate_mask.sum())
    source = source.loc[~duplicate_mask].drop(
        columns=["__event_role", "__source_hash", "__load_order"],
        errors="ignore",
    )
    return source.reset_index(drop=True), duplicate_count


def _resolve_reversals(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events.assign(
            reversal_resolution_status=pd.Series(dtype=str),
            original_event_id=pd.Series(dtype=str),
            reversal_count=pd.Series(dtype=int),
            active_at_cutoff=pd.Series(dtype=bool),
            is_unusual=pd.Series(dtype=bool),
        )

    result = events.copy()
    result["reversal_resolution_status"] = "NOT_REVERSAL"
    result["original_event_id"] = None
    result["reversal_count"] = 0
    result["active_at_cutoff"] = result["event_role"].eq("ORIGINAL")
    result["is_unusual"] = False

    originals = result[result["event_role"] == "ORIGINAL"]
    reversals = result[result["event_role"] == "REVERSAL"]
    for reversal_index, reversal in reversals.iterrows():
        if not bool(reversal.get("eligible", False)):
            result.at[reversal_index, "reversal_resolution_status"] = "INELIGIBLE_REVERSAL"
            result.at[reversal_index, "is_unusual"] = True
            continue
        candidates = originals[
            (originals["cluster_id"] == reversal["cluster_id"])
            & (originals["transaction_id"] == reversal["transaction_id"])
        ]
        eligible_candidates = candidates[candidates["eligible"]]
        if candidates.empty:
            status = "UNRESOLVED_MISSING_ORIGINAL"
            original_index = None
        elif eligible_candidates.empty:
            status = "UNRESOLVED_INELIGIBLE_ORIGINAL"
            original_index = None
        else:
            candidates = eligible_candidates
            prior = candidates[candidates["event_timestamp"] < reversal["event_timestamp"]]
            if prior.empty:
                status = "INVALID_CHRONOLOGY"
                original_index = None
            elif len(prior) > 1:
                status = "AMBIGUOUS_MULTIPLE_ORIGINALS"
                original_index = None
            else:
                original_index = prior.index[0]
                original = prior.loc[original_index]
                if abs(float(original["signed_amount"]) + float(reversal["signed_amount"])) > 0.01:
                    status = "AMOUNT_MISMATCH"
                    original_index = None
                else:
                    status = "COMPLETE"
        result.at[reversal_index, "reversal_resolution_status"] = status
        result.at[reversal_index, "is_unusual"] = status != "COMPLETE"
        if original_index is not None:
            result.at[reversal_index, "original_event_id"] = result.at[original_index, "event_id"]
            result.at[original_index, "reversal_count"] = int(result.at[original_index, "reversal_count"]) + 1

    for original_index in originals.index:
        count = int(result.at[original_index, "reversal_count"])
        if count > 1:
            result.at[original_index, "reversal_resolution_status"] = "MULTIPLE_REVERSALS"
            result.at[original_index, "is_unusual"] = True
        elif count == 1:
            result.at[original_index, "reversal_resolution_status"] = "COMPLETE"
        result.at[original_index, "active_at_cutoff"] = count == 0
    return result


def build_finpay_monthly_preview_from_dataframe(
    events: pd.DataFrame,
    *,
    cluster_id: str,
    report_month: str | date,
    calculation_cutoff: str | datetime,
    expected_source_dates: Iterable[str | date] | None = None,
    source_fingerprint: str | None = None,
    loaded_source_dates: Iterable[str | date] | None = None,
) -> dict:
    """Build a deterministic monthly preview without database side effects."""
    month_start, month_end = _month_bounds(report_month)
    cutoff = _cutoff_timestamp(calculation_cutoff)
    source = _as_monthly_input(events)
    source = source[source["event_timestamp"] < cutoff].copy()
    source = preprocess_transaction_labels(source)
    summary_ready, unusual = prepare_reversal_summary_transactions(source)
    eligible_ids = set(summary_ready.get("__monthly_row_id", pd.Series(dtype=int)))
    source["__eligible"] = source["__monthly_row_id"].isin(eligible_ids)
    source, duplicate_source_count = _deduplicate_monthly_rows(source)
    grouped = _group_events(source, cluster_id=cluster_id)
    grouped = _resolve_reversals(grouped)

    month_originals = grouped[
        (grouped["event_role"] == "ORIGINAL")
        & grouped["settlement_date"].notna()
        & (grouped["settlement_date"] >= month_start)
        & (grouped["settlement_date"] < month_end)
    ].copy()
    month_originals["active_at_cutoff"] = (
        month_originals["active_at_cutoff"]
        & month_originals["eligible"]
    )
    if month_originals.empty:
        month_originals["gross_amount"] = pd.Series(dtype=float)
        month_originals["reversed_amount"] = pd.Series(dtype=float)
        month_originals["net_payable"] = pd.Series(dtype=float)
    else:
        month_originals["gross_amount"] = month_originals["signed_amount"]
        month_originals["reversed_amount"] = month_originals.apply(
            lambda row: row["signed_amount"] if int(row["reversal_count"]) > 0 else 0.0,
            axis=1,
        )
        month_originals["net_payable"] = month_originals.apply(
            lambda row: row["signed_amount"] if row["active_at_cutoff"] else 0.0,
            axis=1,
        )

    expected_dates = {
        pd.to_datetime(value, errors="raise").date()
        for value in (expected_source_dates or [])
    }
    complete_month_dates = _calendar_dates(month_start, month_end)
    expected_period_missing_dates = sorted(complete_month_dates - expected_dates)
    month_source = source[
        (source["event_timestamp"].dt.date >= month_start)
        & (source["event_timestamp"].dt.date < month_end)
    ]
    if loaded_source_dates is None:
        loaded_dates = set(month_source["event_timestamp"].dt.date)
    else:
        loaded_dates = {
            pd.to_datetime(value, errors="raise").date()
            for value in loaded_source_dates
        }
    missing_dates = sorted(expected_dates - loaded_dates) if expected_dates else []
    report_source_dates = (
        expected_dates.intersection(loaded_dates)
        if expected_dates
        else set(month_source["event_timestamp"].dt.date)
    )
    coverage_verified = bool(expected_dates)
    missing_source_count = len(set(missing_dates) | set(expected_period_missing_dates))

    relevant_reversals = grouped[
        (grouped["event_role"] == "REVERSAL")
        & (grouped["event_timestamp"] < cutoff)
        & grouped["original_event_id"].notna()
    ]
    month_event_ids = set(month_originals["event_id"])
    relevant_reversals = relevant_reversals[
        relevant_reversals["original_event_id"].isin(month_event_ids)
    ]
    unresolved = grouped[
        (grouped["event_role"] == "REVERSAL")
        & (grouped["event_timestamp"] < cutoff)
        & grouped["is_unusual"]
    ]
    unresolved = unresolved[
        (
            unresolved["event_timestamp"].dt.date.ge(month_start)
            & unresolved["event_timestamp"].dt.date.lt(month_end)
        )
        | unresolved["transaction_id"].isin(set(month_originals["transaction_id"]))
        | unresolved["original_event_id"].notna()
    ]
    multiple_reversal_originals = grouped[
        (grouped["event_role"] == "ORIGINAL")
        & grouped["reversal_resolution_status"].eq("MULTIPLE_REVERSALS")
        & (
            grouped["event_timestamp"].dt.date.ge(month_start)
            & grouped["event_timestamp"].dt.date.lt(month_end)
        )
    ]

    all_month_qris = grouped[
        (grouped["event_role"] == "ORIGINAL")
        & (grouped["event_timestamp"].dt.date >= month_start)
        & (grouped["event_timestamp"].dt.date < month_end)
        & grouped["category"].eq("QRISDUWIT")
    ]
    qris_missing_dates = int(
        all_month_qris["disbursement_dates"]
        .map(lambda values: not values or any(not value for value in values))
        .sum()
    ) if not all_month_qris.empty else 0
    blocking_count = int(
        len(unresolved)
        + len(multiple_reversal_originals)
        + missing_source_count
        + qris_missing_dates
    )
    if not coverage_verified or missing_source_count:
        status = MONTHLY_STATUS_INCOMPLETE_SOURCE
    elif unresolved.empty and multiple_reversal_originals.empty and qris_missing_dates == 0:
        status = MONTHLY_STATUS_READY
    else:
        status = MONTHLY_STATUS_INCOMPLETE_REVERSALS

    category_rows = []
    if not month_originals.empty:
        eligible_originals = month_originals[month_originals["eligible"]]
        for category, category_df in eligible_originals.groupby("category", sort=True):
            category_rows.append({
                "platform": "FINPAY",
                "cluster_id": cluster_id,
                "report_month": month_start.isoformat(),
                "settlement_category": category,
                "gross_transaction_count": int(len(category_df)),
                "reversed_transaction_count": int((category_df["reversal_count"] > 0).sum()),
                "active_transaction_count": int(category_df["active_at_cutoff"].sum()),
                "gross_amount": float(category_df["gross_amount"].sum()),
                "reversed_amount": float(category_df["reversed_amount"].sum()),
                "net_payable": float(category_df["net_payable"].sum()),
                "unresolved_count": int(len(unresolved[unresolved["category"] == category])),
                "calculation_status": status,
            })

    reversal_evidence = grouped[
        (grouped["event_role"] == "REVERSAL")
        & (
            (
                grouped["event_timestamp"].dt.date.ge(month_start)
                & grouped["event_timestamp"].dt.date.lt(month_end)
            )
            | grouped["original_event_id"].isin(set(month_originals["event_id"]))
        )
    ].copy()
    if not reversal_evidence.empty:
        reversal_evidence["gross_amount"] = 0.0
        reversal_evidence["reversed_amount"] = 0.0
        reversal_evidence["net_payable"] = 0.0
        reversal_evidence["active_at_cutoff"] = False
    transaction_rows = month_originals.to_dict(orient="records") + (
        reversal_evidence.to_dict(orient="records")
        if not reversal_evidence.empty
        else []
    )
    return {
        "platform": "FINPAY",
        "cluster_id": cluster_id,
        "report_month": month_start.isoformat(),
        "calculation_cutoff": cutoff.isoformat(),
        "source_fingerprint": source_fingerprint,
        "status": status,
        "release_ready": status == MONTHLY_STATUS_READY,
        "restatement_required": False,
        "published": False,
        "source_day_count": len(report_source_dates),
        "evidence_source_day_count": len(loaded_dates),
        "missing_source_day_count": missing_source_count,
        "missing_source_dates": [value.isoformat() for value in missing_dates],
        "expected_period_missing_dates": [
            value.isoformat() for value in expected_period_missing_dates
        ],
        "expected_period_complete": not expected_period_missing_dates,
        "unresolved_reversal_count": int(len(unresolved)),
        "multiple_reversal_count": int(len(multiple_reversal_originals)),
        "duplicate_source_row_count": duplicate_source_count,
        "unresolved_exposure": float(unresolved["signed_amount"].abs().sum()) if not unresolved.empty else 0.0,
        "blocking_exception_count": blocking_count,
        "qris_missing_disbursement_count": qris_missing_dates,
        "transaction_rows": transaction_rows,
        "category_rows": category_rows,
        "unusual_rows": unusual.to_dict(orient="records"),
    }
    result["calculation_fingerprint"] = _calculation_fingerprint(result)
    return result


def _fetch_dataframe(cur, query: str, params: tuple) -> pd.DataFrame:
    cur.execute(query, params)
    rows = cur.fetchall()
    columns = [column.name for column in cur.description]
    return pd.DataFrame(rows, columns=columns)


def _source_fingerprint(loads: pd.DataFrame) -> str:
    records = loads.fillna("").sort_values(["load_id"]).to_dict(orient="records")
    encoded = json.dumps(records, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _calculation_fingerprint(result: dict) -> str:
    payload = {
        "source_fingerprint": result.get("source_fingerprint"),
        "cluster_id": result.get("cluster_id"),
        "report_month": result.get("report_month"),
        "calculation_cutoff": result.get("calculation_cutoff"),
        "report_source_day_count": result.get("source_day_count", 0),
        "evidence_source_day_count": result.get("evidence_source_day_count", 0),
        "expected_period_missing_dates": result.get("expected_period_missing_dates", []),
        "category_rows": result.get("category_rows", []),
        "unresolved_reversal_count": result.get("unresolved_reversal_count", 0),
        "multiple_reversal_count": result.get("multiple_reversal_count", 0),
        "duplicate_source_row_count": result.get("duplicate_source_row_count", 0),
        "classification_version": CLASSIFICATION_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_finpay_monthly_preview(
    dsn: str,
    *,
    cluster_id: str,
    report_month: str | date,
    calculation_cutoff: str | datetime,
    expected_source_dates: Iterable[str | date] | None = None,
) -> dict:
    """Build a preview from current append-only evidence without publishing."""
    month_start, month_end = _month_bounds(report_month)
    cutoff = _cutoff_timestamp(calculation_cutoff)
    ensure_finpay_monthly_model(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            events = _fetch_dataframe(
                cur,
                """
                SELECT *
                FROM finpay_current_ledger_events_v
                WHERE cluster_id = %s
                  AND transaction_date < %s
                ORDER BY transaction_date, source_row_number
                """,
                (cluster_id, cutoff.to_pydatetime()),
            )
            loads = _fetch_dataframe(
                cur,
                """
                SELECT load_id, source_sha256, report_date, source_start_date,
                       source_end_date, source_file, row_count
                FROM finpay_source_loads
                WHERE cluster_id = %s
                  AND is_current
                  AND COALESCE(source_end_date, report_date) >= %s
                  AND COALESCE(source_start_date, report_date) < %s
                """,
                (cluster_id, month_start, cutoff.date()),
            )
            if expected_source_dates:
                cur.executemany(
                    """
                    INSERT INTO finpay_monthly_source_expectations (
                        cluster_id, report_month, expected_source_date, expected_file_key
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cluster_id, report_month, expected_source_date)
                    DO UPDATE SET expected_file_key = EXCLUDED.expected_file_key,
                                  required = TRUE
                    """,
                    [
                        (
                            cluster_id,
                            month_start,
                            pd.to_datetime(value, errors="raise").date(),
                            f"{cluster_id}:{value}",
                        )
                        for value in expected_source_dates
                    ],
                )
        loaded_source_dates = set()
        for row in loads.itertuples(index=False):
            start_value = getattr(row, "source_start_date", None) or row.report_date
            end_value = getattr(row, "source_end_date", None) or row.report_date
            start_value = max(pd.to_datetime(start_value).date(), month_start)
            end_value = min(pd.to_datetime(end_value).date(), cutoff.date())
            if start_value <= end_value:
                loaded_source_dates.update(
                    start_value + timedelta(days=offset)
                    for offset in range((end_value - start_value).days + 1)
                )
    result = build_finpay_monthly_preview_from_dataframe(
        events,
        cluster_id=cluster_id,
        report_month=month_start,
        calculation_cutoff=cutoff,
        expected_source_dates=expected_source_dates,
        source_fingerprint=_source_fingerprint(loads),
        loaded_source_dates=loaded_source_dates,
    )
    result["calculation_fingerprint"] = _calculation_fingerprint(result)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_fingerprint
                FROM finpay_monthly_publications
                WHERE cluster_id = %s
                  AND report_month = %s
                  AND status = %s
                ORDER BY published_at DESC
                LIMIT 1
                """,
                (cluster_id, month_start, MONTHLY_STATUS_FINAL),
            )
            final_publication = cur.fetchone()
    if final_publication and final_publication[0] != result["source_fingerprint"]:
        result["restatement_required"] = True
        if result["status"] == MONTHLY_STATUS_READY:
            result["status"] = MONTHLY_STATUS_RESTATEMENT_REQUIRED
            result["release_ready"] = True
    return result


def record_finpay_monthly_preview(
    dsn: str,
    result: dict,
    *,
    preview_id: str,
) -> dict:
    """Persist preview metadata without mutating the frozen monthly snapshot."""
    ensure_finpay_monthly_model(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finpay_monthly_previews (
                    preview_id, cluster_id, report_month, calculation_cutoff,
                    source_fingerprint, calculation_fingerprint, status,
                    expected_period_complete, missing_source_day_count,
                    unresolved_reversal_count, multiple_reversal_count,
                    duplicate_source_row_count, blocking_exception_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (preview_id) DO UPDATE SET
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    calculation_fingerprint = EXCLUDED.calculation_fingerprint,
                    status = EXCLUDED.status,
                    expected_period_complete = EXCLUDED.expected_period_complete,
                    missing_source_day_count = EXCLUDED.missing_source_day_count,
                    unresolved_reversal_count = EXCLUDED.unresolved_reversal_count,
                    multiple_reversal_count = EXCLUDED.multiple_reversal_count,
                    duplicate_source_row_count = EXCLUDED.duplicate_source_row_count,
                    blocking_exception_count = EXCLUDED.blocking_exception_count,
                    previewed_at = CURRENT_TIMESTAMP
                """,
                (
                    preview_id,
                    result["cluster_id"],
                    result["report_month"],
                    result["calculation_cutoff"],
                    result["source_fingerprint"],
                    result["calculation_fingerprint"],
                    result["status"],
                    result["expected_period_complete"],
                    result["missing_source_day_count"],
                    result["unresolved_reversal_count"],
                    result["multiple_reversal_count"],
                    result["duplicate_source_row_count"],
                    result["blocking_exception_count"],
                ),
            )
        conn.commit()
    result["preview_id"] = preview_id
    return result


def publish_finpay_monthly_snapshot(
    dsn: str,
    result: dict,
    *,
    publication_id: str,
    approved_by: str | None = None,
    expected_source_fingerprint: str | None = None,
) -> dict:
    """Publish one ready preview as an atomic cluster-month snapshot."""
    publishable_status = result.get("status") in {
        MONTHLY_STATUS_READY,
        MONTHLY_STATUS_RESTATEMENT_REQUIRED,
    }
    blocking_exceptions = any(int(result.get(key, 0) or 0) > 0 for key in (
        "blocking_exception_count",
        "missing_source_day_count",
        "unresolved_reversal_count",
        "multiple_reversal_count",
        "qris_missing_disbursement_count",
    ))
    if not publishable_status or not result.get("expected_period_complete") or blocking_exceptions:
        raise ValueError(
            "Monthly result is not ready for publication: "
            f"status={result.get('status')} blockers={result.get('blocking_exception_count')}"
        )
    if expected_source_fingerprint is None:
        raise ValueError("expected_source_fingerprint is required for publication")
    if result.get("source_fingerprint") != expected_source_fingerprint:
        raise ValueError("Monthly preview source fingerprint changed before publication")

    ensure_finpay_monthly_model(dsn)
    month_start, month_end = _month_bounds(result["report_month"])
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"finpay-monthly:{result['cluster_id']}:{result['report_month']}",),
            )
            cur.execute(
                """
                SELECT load_id, source_sha256, report_date, source_start_date,
                       source_end_date, source_file, row_count
                FROM finpay_source_loads
                WHERE cluster_id = %s
                  AND is_current
                  AND COALESCE(source_end_date, report_date) >= %s
                  AND COALESCE(source_start_date, report_date) < %s
                """,
                (
                    result["cluster_id"],
                    month_start,
                    pd.to_datetime(result["calculation_cutoff"]).date(),
                ),
            )
            load_rows = cur.fetchall()
            load_columns = [column.name for column in cur.description]
            current_fingerprint = _source_fingerprint(
                pd.DataFrame(load_rows, columns=load_columns)
            )
            if current_fingerprint != expected_source_fingerprint:
                raise ValueError(
                    "Monthly source fingerprint changed during publication lock"
                )
            if result.get("preview_id"):
                cur.execute(
                    """
                    SELECT source_fingerprint, calculation_fingerprint, status
                    FROM finpay_monthly_previews
                    WHERE preview_id = %s
                    """,
                    (result["preview_id"],),
                )
                preview = cur.fetchone()
                if (
                    not preview
                    or preview[0] != result["source_fingerprint"]
                    or preview[1] != result["calculation_fingerprint"]
                ):
                    raise ValueError("Monthly preview metadata no longer matches publication")
            cur.execute(
                """
                UPDATE finpay_monthly_publications
                SET status = %s
                WHERE cluster_id = %s
                  AND report_month = %s
                  AND status = %s
                """,
                (
                    MONTHLY_STATUS_SUPERSEDED,
                    result["cluster_id"],
                    result["report_month"],
                    MONTHLY_STATUS_FINAL,
                ),
            )
            cur.execute(
                """
                DELETE FROM finpay_monthly_transactions
                WHERE cluster_id = %s AND report_month = %s
                """,
                (result["cluster_id"], result["report_month"]),
            )
            rows = []
            for row in result.get("transaction_rows", []):
                rows.append((
                    result["cluster_id"],
                    result["report_month"],
                    result["calculation_cutoff"],
                    publication_id,
                    row["event_id"],
                    row["transaction_id"],
                    row["event_role"],
                    row.get("original_event_id"),
                    row["event_timestamp"],
                    row.get("settlement_date"),
                    row["category"],
                    row["signed_amount"],
                    row["gross_amount"],
                    row["reversed_amount"],
                    row["net_payable"],
                    row["active_at_cutoff"],
                    row["reversal_resolution_status"],
                    row["reversal_count"],
                    row["eligible"],
                    result["source_fingerprint"],
                    CLASSIFICATION_VERSION,
                ))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO finpay_monthly_transactions (
                        cluster_id, report_month, calculation_cutoff,
                        publication_id, event_id, transaction_id, event_role,
                        linked_original_event_id, event_timestamp, settlement_date,
                        settlement_category,
                        signed_amount,
                        gross_amount, reversed_amount, net_payable,
                        active_at_cutoff, reversal_resolution_status,
                        reversal_count, eligible, source_fingerprint, classification_version
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
            cur.execute(
                """
                INSERT INTO finpay_monthly_publications (
                    publication_id, cluster_id, report_month, calculation_cutoff,
                    source_fingerprint, source_day_count, missing_source_day_count,
                    unresolved_reversal_count, multiple_reversal_count,
                    duplicate_source_row_count, blocking_exception_count,
                    expected_period_complete, database_status, google_status,
                    preview_fingerprint, calculation_fingerprint, status,
                    approved_by, published_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (publication_id) DO UPDATE SET
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    multiple_reversal_count = EXCLUDED.multiple_reversal_count,
                    duplicate_source_row_count = EXCLUDED.duplicate_source_row_count,
                    expected_period_complete = EXCLUDED.expected_period_complete,
                    database_status = EXCLUDED.database_status,
                    google_status = EXCLUDED.google_status,
                    preview_fingerprint = EXCLUDED.preview_fingerprint,
                    calculation_fingerprint = EXCLUDED.calculation_fingerprint,
                    status = EXCLUDED.status,
                    approved_by = EXCLUDED.approved_by,
                    published_at = EXCLUDED.published_at
                """,
                (
                    publication_id,
                    result["cluster_id"],
                    result["report_month"],
                    result["calculation_cutoff"],
                    result["source_fingerprint"],
                    result["source_day_count"],
                    result["missing_source_day_count"],
                    result["unresolved_reversal_count"],
                    result["multiple_reversal_count"],
                    result["duplicate_source_row_count"],
                    result["blocking_exception_count"],
                    result["expected_period_complete"],
                    "FINAL",
                    "PENDING",
                    result["source_fingerprint"],
                    result["calculation_fingerprint"],
                    MONTHLY_STATUS_FINAL,
                    approved_by,
                ),
            )
        conn.commit()
    result = dict(result)
    result["published"] = True
    result["status"] = MONTHLY_STATUS_FINAL
    result["publication_id"] = publication_id
    return result


def mark_finpay_google_publication_status(
    dsn: str,
    publication_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Record Google rendering independently from the database snapshot."""
    if status not in {"PENDING", "COMPLETE", "FAILED", "SKIPPED"}:
        raise ValueError(f"Unsupported Google publication status: {status}")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE finpay_monthly_publications
                SET google_status = %s,
                    google_error = %s
                WHERE publication_id = %s
                """,
                (status, error, publication_id),
            )
        conn.commit()


def write_finpay_monthly_to_gsheet(
    gspread_client,
    *,
    spreadsheet_title: str,
    worksheet_title: str,
    result: dict,
) -> dict:
    """Write a frozen monthly result using the daily monitoring visual system."""
    spreadsheet = open_or_create_finpay_spreadsheet(gspread_client, spreadsheet_title)
    try:
        worksheet = spreadsheet.worksheet(worksheet_title)
    except Exception:
        worksheet = spreadsheet.add_worksheet(title=worksheet_title, rows=500, cols=8)

    navy = {"red": 0.122, "green": 0.306, "blue": 0.471}
    white = {"red": 1, "green": 1, "blue": 1}
    cash_header = {"red": 0.820, "green": 0.910, "blue": 0.800}
    cash_body = {"red": 0.925, "green": 0.973, "blue": 0.910}
    stripe = {"red": 0.965, "green": 0.980, "blue": 0.992}
    status_header = {"red": 0.850, "green": 0.765, "blue": 0.890}
    status_body = {"red": 0.965, "green": 0.930, "blue": 0.990}
    border = {"red": 0.650, "green": 0.700, "blue": 0.750}
    text = {"red": 0, "green": 0, "blue": 0}
    muted = {"red": 0.420, "green": 0.420, "blue": 0.420}

    category_header_row = 6
    category_rows = result.get("category_rows", [])
    category_start_row = category_header_row + 1
    category_end_row = category_start_row + len(category_rows) - 1
    exception_title_row = max(category_end_row, category_header_row) + 2
    exception_header_row = exception_title_row + 1
    exception_rows = [
        ["UNRESOLVED REVERSALS", result.get("unresolved_reversal_count", 0), ""],
        ["MULTIPLE REVERSALS", result.get("multiple_reversal_count", 0), "Blocking"],
        ["DUPLICATE SOURCE ROWS", result.get("duplicate_source_row_count", 0), "Deduplicated before grouping"],
        ["UNRESOLVED EXPOSURE", result.get("unresolved_exposure", 0), ""],
        [
            "MISSING SOURCE DAYS",
            result.get("missing_source_day_count", 0),
            ", ".join(result.get("missing_source_dates", [])),
        ],
        [
            "QRIS MISSING DISBURSEMENT",
            result.get("qris_missing_disbursement_count", 0),
            "Required before final release",
        ],
        [
            "EXPECTED PERIOD COMPLETE",
            str(result.get("expected_period_complete", False)),
            "Required before final release",
        ],
    ]
    exception_start_row = exception_header_row + 1
    exception_end_row = exception_start_row + len(exception_rows) - 1

    def row8(values):
        return list(values) + [""] * (8 - len(values))

    values = [
        row8(["FINPAY MONTHLY SETTLEMENT"]),
        row8([
            "CLUSTER", result.get("cluster_id", ""),
            "REPORT MONTH", result.get("report_month", ""),
            "STATUS", result.get("status", ""),
            "PUBLISHED", str(result.get("published", False)),
        ]),
        row8([
            "CALCULATION CUTOFF", result.get("calculation_cutoff", ""),
            "PUBLICATION ID", result.get("publication_id", ""),
            "SOURCE FINGERPRINT", result.get("source_fingerprint", ""),
        ]),
        row8([
            "REPORT SOURCE DAYS", result.get("source_day_count", 0),
            "EVIDENCE WINDOW DAYS", result.get("evidence_source_day_count", 0),
            "MISSING SOURCE DAYS", result.get("missing_source_day_count", 0),
            "UNRESOLVED REVERSALS", result.get("unresolved_reversal_count", 0),
        ]),
        row8([
            "BLOCKING EXCEPTIONS", result.get("blocking_exception_count", 0),
            "MULTIPLE REVERSALS", result.get("multiple_reversal_count", 0),
            "DUPLICATE SOURCE ROWS", result.get("duplicate_source_row_count", 0),
            "QRIS MISSING DATES", result.get("qris_missing_disbursement_count", 0),
        ]),
        row8([
            "CATEGORY", "GROSS COUNT", "REVERSED COUNT", "ACTIVE COUNT",
            "GROSS AMOUNT", "REVERSED AMOUNT", "NET PAYABLE", "STATUS",
        ]),
    ]
    values.extend(row8([
        row.get("settlement_category", ""),
        row.get("gross_transaction_count", 0),
        row.get("reversed_transaction_count", 0),
        row.get("active_transaction_count", 0),
        row.get("gross_amount", 0),
        row.get("reversed_amount", 0),
        row.get("net_payable", 0),
        row.get("calculation_status", ""),
    ]) for row in category_rows)
    while len(values) < exception_title_row - 1:
        values.append(row8([]))
    values.append(row8(["EXCEPTIONS"]))
    values.append(row8(["EXCEPTION", "VALUE", "DETAIL"]))
    values.extend(row8(row) for row in exception_rows)

    metadata = spreadsheet.fetch_sheet_metadata({
        "fields": "sheets(properties(sheetId),merges)",
    })
    prewrite_requests = [
        *_delete_all_banded_range_requests(spreadsheet, worksheet),
        *_delete_all_protected_range_requests(spreadsheet, worksheet),
    ]
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == worksheet.id:
            prewrite_requests.extend(
                {"unmergeCells": {"range": merge}}
                for merge in sheet.get("merges", [])
            )
    if prewrite_requests:
        spreadsheet.batch_update({"requests": prewrite_requests})

    worksheet.clear()
    ensure_row_capacity(
        spreadsheet,
        worksheet,
        len(values),
        buffer_rows=100,
        label="monthly worksheet",
    )
    worksheet.update(
        "A1",
        uppercase_sheet_rows(values),
        value_input_option="USER_ENTERED",
    )

    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {
                        "frozenRowCount": category_header_row,
                        "rowCount": max(len(values) + 100, 500),
                        "columnCount": 8,
                    },
                },
                "fields": "gridProperties(frozenRowCount,rowCount,columnCount)",
            }
        },
        {
            "mergeCells": {
                "range": _grid_range(worksheet, 1, 1, 1, 8),
                "mergeType": "MERGE_ALL",
            }
        },
    ]
    widths = [220, 110, 120, 110, 135, 135, 135, 150]
    for column_index, width in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": worksheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": column_index,
                    "endIndex": column_index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    def repeat(start_row, end_row, start_col, end_col, fmt, fields):
        if start_row <= end_row:
            requests.append({
                "repeatCell": {
                    "range": _grid_range(worksheet, start_row, end_row, start_col, end_col),
                    "cell": {"userEnteredFormat": fmt},
                    "fields": fields,
                }
            })

    repeat(
        1, 1, 1, 8,
        {"backgroundColor": navy, "textFormat": {"bold": True, "foregroundColor": white, "fontSize": 12},
         "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"},
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
    )
    repeat(
        2, 5, 1, 8,
        {"backgroundColor": cash_body, "textFormat": {"foregroundColor": text}, "verticalAlignment": "MIDDLE"},
        "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    )
    for column in (1, 3, 5, 7):
        repeat(
            2, 5, column, column,
            {"backgroundColor": cash_header, "textFormat": {"bold": True, "foregroundColor": text}},
            "userEnteredFormat(backgroundColor,textFormat)",
        )
    repeat(
        category_header_row, category_header_row, 1, 8,
        {"backgroundColor": cash_header, "textFormat": {"bold": True, "foregroundColor": text},
         "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"},
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
    )
    repeat(
        category_start_row, category_end_row, 1, 8,
        {"backgroundColor": cash_body, "textFormat": {"foregroundColor": text}},
        "userEnteredFormat(backgroundColor,textFormat)",
    )
    for row_number in range(category_start_row, category_end_row + 1, 2):
        repeat(row_number, row_number, 1, 8, {"backgroundColor": stripe}, "userEnteredFormat.backgroundColor")
    repeat(
        category_start_row, category_end_row, 2, 4,
        {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}, "horizontalAlignment": "RIGHT"},
        "userEnteredFormat(numberFormat,horizontalAlignment)",
    )
    repeat(
        category_start_row, category_end_row, 5, 7,
        {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}, "horizontalAlignment": "RIGHT"},
        "userEnteredFormat(numberFormat,horizontalAlignment)",
    )
    repeat(
        exception_title_row, exception_title_row, 1, 8,
        {"backgroundColor": status_header, "textFormat": {"bold": True, "foregroundColor": text},
         "horizontalAlignment": "CENTER"},
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
    )
    repeat(
        exception_header_row, exception_header_row, 1, 3,
        {"backgroundColor": status_header, "textFormat": {"bold": True, "foregroundColor": text},
         "horizontalAlignment": "CENTER"},
        "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
    )
    repeat(
        exception_start_row, exception_end_row, 1, 3,
        {"backgroundColor": status_body, "textFormat": {"foregroundColor": muted}},
        "userEnteredFormat(backgroundColor,textFormat)",
    )
    repeat(
        exception_start_row, exception_end_row, 2, 2,
        {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}, "horizontalAlignment": "RIGHT"},
        "userEnteredFormat(numberFormat,horizontalAlignment)",
    )
    for start_row, end_row in ((1, 1), (category_header_row, category_end_row), (exception_title_row, exception_end_row)):
        requests.extend([
            {
                "repeatCell": {
                    "range": _grid_range(worksheet, start_row, end_row, 1, 8),
                    "cell": {"userEnteredFormat": {"borders": {"top": {"style": "SOLID_MEDIUM", "color": border}, "bottom": {"style": "SOLID", "color": border}}}},
                    "fields": "userEnteredFormat.borders",
                }
            }
        ])
    protected = _add_protected_sheet_request(
        gspread_client,
        worksheet,
        "FinPay monthly settlement output",
    )
    if protected:
        requests.append(protected)
    spreadsheet.batch_update({"requests": requests})
    return {
        "spreadsheet": spreadsheet_title,
        "worksheet": worksheet_title,
        "rows": len(values),
        "category_rows": len(category_rows),
        "exception_rows": len(exception_rows),
    }
