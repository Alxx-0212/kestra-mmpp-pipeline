"""Code-owned cell roster and fail-closed runtime target settings."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from ..cell_calculation import CellReportConfig
from ..cell_contracts import OBSERVED_CELL_ROSTER, business_date
from ..cell_database import CellDatabaseTarget
from ..cell_publication import CellWorksheetTarget
from ..cell_publication_v2 import ROLES, cell_worksheet_title, validate_output_targets

# Promoted as the cell-format acquisition roster; DigiPOS is checked against it.
ROSTER_VERSION = "finpay-cell-roster-observed-2026-10-19-v1"
CELL_ROSTER = {account: tuple(codes) for account, codes in OBSERVED_CELL_ROSTER.items()}
REPORTING_START_DATE = "2026-10-01"
MAX_CELL_RUNS = 100_000

# The Finance-provided workbook/tab IDs and editor identities are not available.
# Leave these empty rather than reusing synthetic or previous-format targets.
CELL_FORMAT_SPREADSHEET_ID = ""
CELL_FORMAT_SPREADSHEET_NAME = ""
LEGACY_SPREADSHEET_ID = ""
CELL_FORMAT_LEGACY_SPREADSHEET_ID = ""
CELL_FORMAT_WRITER_EMAIL = ""
CELL_FORMAT_FINANCE_EMAILS: tuple[str, ...] = ()
CELL_FORMAT_WORKSHEET_IDS: dict[str, dict[str, int]] = {}
CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING: bool | None = None
CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING: bool = True

DATABASE_SCHEMA = "public"
CELL_FORMAT_NAMING_SCHEME = "cluster_alias"


@dataclass(frozen=True)
class CellSheetConfiguration:
    spreadsheet_id: str | None
    spreadsheet_name: str | None
    legacy_spreadsheet_id: str
    writer_email: str
    finance_emails: tuple[str, ...]
    create_spreadsheet_if_missing: bool
    create_worksheets_if_missing: bool
    worksheet_ids: dict[str, dict[str, int]] | None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key in cell worksheet ID configuration")
        value[key] = item
    return value


def _setting(environment, name, fallback):
    return environment[name] if name in environment else fallback


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required for cell-format Sheet publication")
    return value.strip()


def _parse_emails(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                value = json.loads(stripped, object_pairs_hook=_unique_object)
            except ValueError as exc:
                raise ValueError("CELL_FORMAT_FINANCE_EMAILS must be comma-separated or a JSON string array") from exc
        else:
            value = stripped.split(",")
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("CELL_FORMAT_FINANCE_EMAILS must be comma-separated or a JSON string array")
    emails = tuple(item.strip() for item in value)
    if (not emails or any(not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", item) for item in emails)
            or len({item.casefold() for item in emails}) != len(emails)):
        raise ValueError("CELL_FORMAT_FINANCE_EMAILS must contain distinct valid email addresses")
    return emails


def _parse_boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in ("true", "false"):
            return normalized == "true"
    raise ValueError(f"{name} must be the string true or false")


def _parse_worksheet_ids(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("CELL_FORMAT_WORKSHEET_IDS must be a JSON account-to-role ID map") from exc
    if not isinstance(value, Mapping):
        raise ValueError("CELL_FORMAT_WORKSHEET_IDS must be an account-to-role ID map")
    configured, seen_ids, seen_titles = {}, set(), set()
    for account, roles in value.items():
        if account not in CELL_ROSTER or not isinstance(roles, Mapping) or set(roles) != set(ROLES):
            raise ValueError("CELL_FORMAT_WORKSHEET_IDS must map configured accounts to all four roles")
        configured[account] = {}
        for role, worksheet_id in roles.items():
            title = cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME)
            if type(worksheet_id) is not int or not 0 <= worksheet_id <= 2_147_483_647:
                raise ValueError("cell-format worksheet IDs must be nonnegative 32-bit integers")
            if worksheet_id in seen_ids or title in seen_titles:
                raise ValueError("cell-format worksheet IDs/titles must be unique across clusters")
            seen_ids.add(worksheet_id)
            seen_titles.add(title)
            configured[account][role] = worksheet_id
    return configured or None


def sheet_configuration(environment=None) -> CellSheetConfiguration:
    """Resolve and validate target settings without constructing Google clients."""
    environment = os.environ if environment is None else environment
    spreadsheet_id = _setting(environment, "CELL_FORMAT_SPREADSHEET_ID", CELL_FORMAT_SPREADSHEET_ID)
    spreadsheet_name = _setting(environment, "CELL_FORMAT_SPREADSHEET_NAME", CELL_FORMAT_SPREADSHEET_NAME)
    if not isinstance(spreadsheet_id, str):
        raise ValueError("CELL_FORMAT_SPREADSHEET_ID must be a string")
    spreadsheet_id = spreadsheet_id.strip()
    if not isinstance(spreadsheet_name, str):
        raise ValueError("CELL_FORMAT_SPREADSHEET_NAME must be a string")
    if spreadsheet_name and spreadsheet_name != spreadsheet_name.strip():
        raise ValueError("CELL_FORMAT_SPREADSHEET_NAME must not have surrounding whitespace")
    if not spreadsheet_id and not spreadsheet_name:
        raise ValueError("CELL_FORMAT_SPREADSHEET_ID or CELL_FORMAT_SPREADSHEET_NAME is required")

    legacy_fallback = CELL_FORMAT_LEGACY_SPREADSHEET_ID or LEGACY_SPREADSHEET_ID
    legacy_value = _setting(environment, "CELL_FORMAT_LEGACY_SPREADSHEET_ID", legacy_fallback)
    legacy_id = _required_text(legacy_value, "CELL_FORMAT_LEGACY_SPREADSHEET_ID")
    if spreadsheet_id and spreadsheet_id == legacy_id:
        raise ValueError("cell-format workbook must be distinct from the legacy workbook")
    writer_email = _required_text(
        _setting(environment, "CELL_FORMAT_WRITER_EMAIL", CELL_FORMAT_WRITER_EMAIL),
        "CELL_FORMAT_WRITER_EMAIL",
    )
    if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", writer_email):
        raise ValueError("CELL_FORMAT_WRITER_EMAIL must be a valid service-account email")
    finance_emails = _parse_emails(
        _setting(environment, "CELL_FORMAT_FINANCE_EMAILS", CELL_FORMAT_FINANCE_EMAILS),
    )
    create_value = _setting(
        environment, "CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING",
        CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING,
    )
    if create_value in (None, ""):
        create = bool(spreadsheet_name)
    else:
        create = _parse_boolean(create_value, "CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING")
    if create and not spreadsheet_name:
        raise ValueError("CELL_FORMAT_SPREADSHEET_NAME is required to create a workbook")
    create_worksheets = _parse_boolean(_setting(
        environment, "CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING", CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING,
    ), "CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING")
    if create and not create_worksheets:
        raise ValueError("creating a workbook requires alias worksheet initialization to be enabled")
    worksheet_ids = _parse_worksheet_ids(
        _setting(environment, "CELL_FORMAT_WORKSHEET_IDS", CELL_FORMAT_WORKSHEET_IDS),
    )
    return CellSheetConfiguration(
        spreadsheet_id or None, spreadsheet_name or None, legacy_id, writer_email,
        finance_emails, create, create_worksheets, worksheet_ids,
    )


def selected_clusters(cluster_id: str) -> tuple[str, ...]:
    clusters = {account[:-2] for account in CELL_ROSTER}
    if not isinstance(cluster_id, str):
        raise ValueError("cluster selector must be ALL or a comma-separated list of configured clusters")
    value = cluster_id.strip()
    if value == "ALL":
        return tuple(sorted(clusters, key=int))
    selected = tuple(part.strip() for part in value.split(","))
    if (not selected or any(part not in clusters for part in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("cluster_id must be ALL or a comma-separated list of distinct configured clusters")
    return tuple(sorted(selected, key=int))


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
    selected = selected_clusters(cluster_id)
    if len(selected) != 1:
        raise ValueError("account_settings requires exactly one cluster")
    account = selected[0] + "_B"
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


def validate_sheet_target_group(cluster_ids, configuration=None, environment=None) -> CellSheetConfiguration:
    """Validate all selected targets without requiring tab IDs or Google clients."""
    clusters = tuple(cluster_ids)
    configured_clusters = {account[:-2] for account in CELL_ROSTER}
    if not clusters or len(set(clusters)) != len(clusters) or any(cluster not in configured_clusters for cluster in clusters):
        raise ValueError("selected cell cluster group must contain distinct configured clusters")
    configuration = configuration or sheet_configuration(environment)
    accounts, targets, titles = {}, {}, set()
    for cluster in sorted(clusters, key=int):
        account, _ = account_settings(cluster)
        report = report_config(cluster, REPORTING_START_DATE)
        accounts[account] = report
        expected_titles = {
            role: cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME)
            for role in ROLES
        }
        if titles.intersection(expected_titles.values()):
            raise ValueError("cell worksheet role titles must be unique across selected clusters")
        titles.update(expected_titles.values())
        if configuration.worksheet_ids is not None:
            role_ids = configuration.worksheet_ids.get(account)
            if role_ids is None:
                raise ValueError("CELL_FORMAT_WORKSHEET_IDS is missing a selected account")
            if configuration.spreadsheet_id:
                targets[account] = sheet_targets(cluster, configuration=configuration)[2]
    if targets and configuration.spreadsheet_id:
        validate_output_targets(
            configuration.spreadsheet_id, configuration.legacy_spreadsheet_id,
            accounts, targets,
        )
    return configuration


def sheet_targets(cluster_id: str, worksheet_ids=None, configuration=None, *,
                  spreadsheet_id=None, environment=None) -> tuple[str, str, dict[str, CellWorksheetTarget]]:
    configuration = configuration or sheet_configuration(environment)
    account, _ = account_settings(cluster_id)
    workbook_id = spreadsheet_id or configuration.spreadsheet_id
    roles = worksheet_ids
    if roles is None:
        roles = (configuration.worksheet_ids or {}).get(account)
    if not workbook_id:
        raise ValueError("resolved cell-format spreadsheet ID is required for worksheet targets")
    if workbook_id == configuration.legacy_spreadsheet_id:
        raise ValueError("cell-format workbook must be distinct from the legacy workbook")
    if not isinstance(roles, Mapping) or set(roles) != set(ROLES):
        raise ValueError("four resolved worksheet IDs are required for each selected cluster")
    if any(type(value) is not int or not 0 <= value <= 2_147_483_647 for value in roles.values()):
        raise ValueError("cell-format worksheet IDs must be nonnegative 32-bit integers")
    if len(set(roles.values())) != len(ROLES):
        raise ValueError("cell-format worksheet IDs must be unique within a cluster")
    targets = {
        role: CellWorksheetTarget(
            role, workbook_id, roles[role],
            cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME), configuration.writer_email,
            tuple(configuration.finance_emails), CELL_FORMAT_NAMING_SCHEME,
        )
        for role in ROLES
    }
    return workbook_id, configuration.legacy_spreadsheet_id, targets
