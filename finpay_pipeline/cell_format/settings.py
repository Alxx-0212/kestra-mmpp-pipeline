"""Code-owned cell roster and fail-closed runtime target settings."""
from __future__ import annotations

import os
from datetime import timedelta

from ..cell_calculation import CellReportConfig
from ..cell_contracts import OBSERVED_CELL_ROSTER, business_date
from ..cell_database import CellDatabaseTarget
from ..cell_publication import CellWorksheetTarget
from ..cell_publication_v2 import ROLES, cell_worksheet_title

# Promoted as the cell-format acquisition roster; DigiPOS is checked against it.
ROSTER_VERSION = "finpay-cell-roster-observed-2026-10-19-v1"
CELL_ROSTER = {account: tuple(codes) for account, codes in OBSERVED_CELL_ROSTER.items()}
REPORTING_START_DATE = "2026-10-01"
MAX_CELL_RUNS = 100_000

# The Finance-provided workbook/tab IDs and editor identities are not available.
# Leave these empty rather than reusing synthetic or previous-format targets.
CELL_FORMAT_SPREADSHEET_ID = ""
LEGACY_SPREADSHEET_ID = ""
CELL_FORMAT_WRITER_EMAIL = ""
CELL_FORMAT_FINANCE_EMAILS: tuple[str, ...] = ()
CELL_FORMAT_WORKSHEET_IDS: dict[str, dict[str, int]] = {}

DATABASE_SCHEMA = "public"
CELL_FORMAT_NAMING_SCHEME = "cluster_alias"


def selected_clusters(cluster_id: str) -> tuple[str, ...]:
    clusters = tuple(sorted(account[:-2] for account in CELL_ROSTER))
    if cluster_id == "ALL":
        return clusters
    if cluster_id not in clusters:
        raise ValueError("cluster_id must be a configured cell cluster or ALL")
    return (cluster_id,)


def expand_cell_range(start_date: str, end_date: str, cluster_id: str) -> list[dict[str, str]]:
    start = business_date(validate_reporting_date(start_date))
    end = business_date(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    clusters = selected_clusters(cluster_id)
    if ((end - start).days + 1) * len(clusters) > MAX_CELL_RUNS:
        raise ValueError("cell range exceeds the bounded entry limit")
    runs = []
    day = start
    while day <= end:
        runs.extend({"report_date": day.isoformat(), "cluster_id": cluster} for cluster in clusters)
        day += timedelta(days=1)
    return runs


def validate_reporting_date(value: str) -> str:
    day = business_date(value).isoformat()
    if day < REPORTING_START_DATE:
        raise ValueError(f"cell-format reporting starts on {REPORTING_START_DATE}")
    return day


def account_settings(cluster_id: str) -> tuple[str, tuple[str, ...]]:
    selected_clusters(cluster_id)
    account = cluster_id + "_B"
    return account, CELL_ROSTER[account]


def report_config(cluster_id: str, first_report_date: str) -> CellReportConfig:
    account, codes = account_settings(cluster_id)
    day = business_date(first_report_date).isoformat()
    return CellReportConfig(
        account, codes, day, day + "T00:00:00+08:00",
        {code: 0 for code in codes}, ROSTER_VERSION,
    )


def database_target(environment=None) -> CellDatabaseTarget:
    environment = os.environ if environment is None else environment
    names = ("FINPAY_DB_HOST", "FINPAY_DB_PORT", "FINPAY_DB_NAME", "FINPAY_DB_USER")
    missing = [name for name in names if not environment.get(name, "").strip()]
    if missing:
        raise ValueError("required FinPay database target settings are missing: " + ", ".join(missing))
    try:
        port = int(environment["FINPAY_DB_PORT"])
    except ValueError as exc:
        raise ValueError("FINPAY_DB_PORT must be an integer") from exc
    return CellDatabaseTarget(
        environment["FINPAY_DB_HOST"].strip(), port,
        environment["FINPAY_DB_NAME"].strip(), DATABASE_SCHEMA,
        environment["FINPAY_DB_USER"].strip(),
    )


def sheet_targets(cluster_id: str) -> tuple[str, str, dict[str, CellWorksheetTarget]]:
    account, _ = account_settings(cluster_id)
    roles = CELL_FORMAT_WORKSHEET_IDS.get(account)
    if (
        not CELL_FORMAT_SPREADSHEET_ID or not LEGACY_SPREADSHEET_ID
        or CELL_FORMAT_SPREADSHEET_ID == LEGACY_SPREADSHEET_ID
        or not CELL_FORMAT_WRITER_EMAIL or not CELL_FORMAT_FINANCE_EMAILS
        or not isinstance(roles, dict) or set(roles) != set(ROLES)
    ):
        raise ValueError(
            "cell-format Sheet settings are incomplete; set distinct new/legacy workbook IDs, "
            "four worksheet IDs per cluster, and approved writer/Finance emails"
        )
    if len(set(roles.values())) != len(ROLES):
        raise ValueError("cell-format worksheet IDs must be unique within a cluster")
    targets = {
        role: CellWorksheetTarget(
            role, CELL_FORMAT_SPREADSHEET_ID, roles[role],
            cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME), CELL_FORMAT_WRITER_EMAIL,
            tuple(CELL_FORMAT_FINANCE_EMAILS), CELL_FORMAT_NAMING_SCHEME,
        )
        for role in ROLES
    }
    return CELL_FORMAT_SPREADSHEET_ID, LEGACY_SPREADSHEET_ID, targets
