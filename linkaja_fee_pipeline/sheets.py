"""Google Sheets renderers for daily and monthly LinkAja reporting."""

from __future__ import annotations

import os
import re
import requests
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .database import (
    DETAIL_HEADERS,
    DETAIL_REPORT_DATE_HEADER,
    MEASURE_COMPANY_CREDIT_HEADER,
    MEASURE_COMPANY_DEBIT_HEADER,
    MEASURE_EXPECTED_FEE_HEADER,
    MEASURE_IN_CLUSTER_FEE_HEADER,
    MEASURE_REVERSAL_COUNT_HEADER,
    MEASURE_REVERSAL_FEE_HEADER,
    MEASURE_SCENARIO_HEADER,
    MEASURE_SOURCE_FEE_HEADER,
    MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER,
    MEASURE_UNRESOLVED_COUNT_HEADER,
    UNRESOLVED_REVERSAL_HEADERS,
)
from .processing import (
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    PPOB_SCENARIO,
    REPORT_DATE_HEADER,
    TOTAL_FEE_HEADER,
    date_sort_key,
)


LEGACY_DETAIL_HEADERS = [
    "REPORT DATE",
    "TRANSACTION SCENARIO",
    "TRANSACTION COUNT",
    "DEBET",
    "KREDIT",
    "SOURCE FEE",
    "EXPECTED FEE",
    "REVERSAL IN CLUSTER COUNT",
    "REVERSAL IN CLUSTER FEE",
]
PREVIOUS_DETAIL_HEADERS = [
    "REPORT DATE",
    "KETERANGAN",
    "DEBET",
    "KREDIT",
    "OTHER",
]
DETAIL_SECTION_LABEL = "DETAIL"
MANDIRI_SECTION_LABEL = "MANDIRI"
CASH_SECTION_LABEL = "Cash"
TOTAL_EXPECTED_MANDIRI_LABEL = "TOTAL EXPECTED MANDIRI"
SELISIH_LABEL = "SELISIH"
WITHDRAWAL_SCENARIO = "Organization Withdraw of Funds with Next Working Day Payment"
OUT_CLUSTER_SCENARIO = "Digipos B2B Transfer"
IN_CLUSTER_SCENARIO = "Digipos B2B Transfer In Cluster"
POSTED_FEE_SCENARIO = "Digipos B2B Transfer Fee"
REVERSAL_SCENARIO = "Buy Goods Reversal for General Merchant"
COMPLETE_REVERSAL_PREFIX = "Complete Reversal - "
COMPLETE_OUT_CLUSTER_REVERSAL_LABEL = (
    f"{COMPLETE_REVERSAL_PREFIX}{OUT_CLUSTER_SCENARIO}"
)
COMPLETE_IN_CLUSTER_REVERSAL_LABEL = (
    f"{COMPLETE_REVERSAL_PREFIX}{IN_CLUSTER_SCENARIO}"
)
UNRESOLVED_REVERSAL_LABEL = "Unusual - Unresolved Reversal"
EXPECTED_FEE_LEDGER_LABEL = "Expected Recharge Out Cluster Fee"
IN_CLUSTER_FEE_LEDGER_LABEL = "Digipos B2B Transfer In Cluster Fee"
LEGACY_REVERSAL_COUNT_LEDGER_LABEL = "Reversal In Cluster Count"
LEGACY_REVERSAL_FEE_LEDGER_LABEL = "Reversal In Cluster Fee"
REVERSAL_COUNT_LEDGER_LABEL = "Complete Reversal In Cluster Count"
REVERSAL_FEE_LEDGER_LABEL = "Complete Reversal In Cluster Fee"
UNRESOLVED_COUNT_LEDGER_LABEL = "Unresolved Reversal Count"
KNOWN_SCENARIO_ORDER = [
    WITHDRAWAL_SCENARIO,
    OUT_CLUSTER_SCENARIO,
    IN_CLUSTER_SCENARIO,
    POSTED_FEE_SCENARIO,
    PPOB_SCENARIO,
    COMPLETE_OUT_CLUSTER_REVERSAL_LABEL,
    COMPLETE_IN_CLUSTER_REVERSAL_LABEL,
    UNRESOLVED_REVERSAL_LABEL,
    REVERSAL_SCENARIO,
]
MANDIRI_COMPONENTS = [
    OUT_CLUSTER_SCENARIO,
    IN_CLUSTER_SCENARIO,
    COMPLETE_OUT_CLUSTER_REVERSAL_LABEL,
    COMPLETE_IN_CLUSTER_REVERSAL_LABEL,
    UNRESOLVED_REVERSAL_LABEL,
    IN_CLUSTER_FEE_LEDGER_LABEL,
    REVERSAL_FEE_LEDGER_LABEL,
]
MANDIRI_PRINCIPAL_COMPONENTS = {
    OUT_CLUSTER_SCENARIO,
    IN_CLUSTER_SCENARIO,
    COMPLETE_OUT_CLUSTER_REVERSAL_LABEL,
    COMPLETE_IN_CLUSTER_REVERSAL_LABEL,
    UNRESOLVED_REVERSAL_LABEL,
    REVERSAL_SCENARIO,
}

DEFAULT_ROW_BUFFER = 100
DEFAULT_COL_BUFFER = 5
HEADER_ROW_COUNT = 2
DATA_START_ROW = HEADER_ROW_COUNT + 1
TABLE_HEADERS = [
    REPORT_DATE_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    TOTAL_FEE_HEADER,
]
TABLE_WIDTH = len(TABLE_HEADERS)
SPACER_WIDTH = 1
BLOCK_WIDTH = TABLE_WIDTH + SPACER_WIDTH
KNOWN_CLUSTER_ORDER = ["421306", "421307", "411311", "421315", "421318", "421320"]
INITIAL_CLUSTER_COUNT = len(KNOWN_CLUSTER_ORDER)
STATIC_SHEET_COL_COUNT = INITIAL_CLUSTER_COUNT * BLOCK_WIDTH - SPACER_WIDTH
CLUSTER_START_COLUMNS = {
    cluster_id: index * BLOCK_WIDTH
    for index, cluster_id in enumerate(KNOWN_CLUSTER_ORDER)
}
CLUSTER_DISPLAY_NAMES = {
    "421306": "MRT",
    "421307": "TDR",
    "411311": "PKY",
    "421315": "BGI",
    "421318": "MRW",
    "421320": "TNT",
}
DETAIL_WORKSHEET_SUFFIX = " - LinkAja Detail"
# C fits the mandatory withdrawal label; F fits the unresolved-reversal status.
DETAIL_COLUMN_WIDTHS = [110, 130, 500, 140, 140, 280]
REPORT_DATE_COLUMN_WIDTH = 110
EXPECTED_FEE_COLUMN_WIDTH = 285
IN_CLUSTER_FEE_COLUMN_WIDTH = 300
TOTAL_FEE_COLUMN_WIDTH = 120
SPACER_COLUMN_WIDTH = 24

WHITE_COLOR = {"red": 1, "green": 1, "blue": 1}
BORDER_COLOR = {"red": 0.650, "green": 0.700, "blue": 0.750}
CLUSTER_PALETTES = [
    {
        "title": {"red": 0.122, "green": 0.306, "blue": 0.471},
        "header": {"red": 0.733, "green": 0.835, "blue": 0.922},
        "body": {"red": 0.925, "green": 0.965, "blue": 0.988},
    },
    {
        "title": {"red": 0.220, "green": 0.424, "blue": 0.204},
        "header": {"red": 0.765, "green": 0.890, "blue": 0.741},
        "body": {"red": 0.925, "green": 0.973, "blue": 0.910},
    },
    {
        "title": {"red": 0.494, "green": 0.184, "blue": 0.556},
        "header": {"red": 0.850, "green": 0.765, "blue": 0.890},
        "body": {"red": 0.965, "green": 0.930, "blue": 0.990},
    },
    {
        "title": {"red": 0.749, "green": 0.341, "blue": 0.000},
        "header": {"red": 0.980, "green": 0.820, "blue": 0.604},
        "body": {"red": 1.000, "green": 0.948, "blue": 0.882},
    },
    {
        "title": {"red": 0.525, "green": 0.082, "blue": 0.082},
        "header": {"red": 0.925, "green": 0.690, "blue": 0.690},
        "body": {"red": 0.992, "green": 0.925, "blue": 0.925},
    },
    {
        "title": {"red": 0.000, "green": 0.376, "blue": 0.455},
        "header": {"red": 0.694, "green": 0.867, "blue": 0.902},
        "body": {"red": 0.910, "green": 0.973, "blue": 0.984},
    },
]


def make_gspread_client(sa_key_path: str):
    from google.oauth2.service_account import Credentials
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_file(sa_key_path, scopes=scopes)
    timeout = (
        float(os.environ.get("LINKAJA_GOOGLE_CONNECT_TIMEOUT", "10")),
        float(os.environ.get("LINKAJA_GOOGLE_READ_TIMEOUT", "45")),
    )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
        raise_on_status=False,
    )
    session = AuthorizedSession(credentials)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    original_request = session.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    discovery_url = "https://sheets.googleapis.com/$discovery/rest?version=v4"
    try:
        response = requests.get(discovery_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            "LinkAja Google API preflight failed; check outbound HTTPS access "
            f"to {discovery_url}: {exc}"
        ) from exc
    client = gspread.Client(auth=credentials, session=session)
    client.auth = credentials
    client.http_client.set_timeout(timeout)
    return client


def uppercase_sheet_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("="):
        return value
    if value.startswith("'"):
        return "'" + value[1:].upper()
    return value.upper()


def uppercase_sheet_rows(rows: list[list[Any]]) -> list[list[Any]]:
    return [[uppercase_sheet_value(value) for value in row] for row in rows]


def a1_cell(row: int, column: int) -> str:
    column_letter = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        column_letter = chr(65 + remainder) + column_letter
    return f"{column_letter}{row}"


def sheet_range(start_row: int, end_row: int, start_col: int, end_col: int) -> str:
    return f"{a1_cell(start_row, start_col)}:{a1_cell(end_row, end_col)}"


def matching_date_cluster_rows(
    existing_rows: list[list[str]],
    headers: list[str],
    new_rows: list[dict[str, Any]],
) -> list[int]:
    """Return matching rows for the legacy single-table sheet layout."""
    if REPORT_DATE_HEADER not in headers or CLUSTER_ID_HEADER not in headers:
        return []

    report_date_column = headers.index(REPORT_DATE_HEADER)
    cluster_id_column = headers.index(CLUSTER_ID_HEADER)
    replacement_keys = {
        (
            str(row.get(REPORT_DATE_HEADER, "")).strip(),
            str(row.get(CLUSTER_ID_HEADER, "")).strip(),
        )
        for row in new_rows
    }

    return [
        row_number
        for row_number, row in enumerate(existing_rows[1:], start=2)
        if (
            len(row) > max(report_date_column, cluster_id_column)
            and (
                str(row[report_date_column]).strip(),
                str(row[cluster_id_column]).strip(),
            ) in replacement_keys
        )
    ]


def process_linkaja_fee_sheet_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    rows: list[dict[str, Any]],
    gspread_client,
) -> dict[str, Any]:
    spreadsheet = _open_or_create_spreadsheet(gspread_client, target_spreadsheet)
    worksheet = _open_or_create_worksheet(spreadsheet, target_worksheet)
    existing_values = worksheet.get_all_values()
    headers_initialized = setup_linkaja_fee_headers(
        gspread_client,
        spreadsheet,
        worksheet,
        existing_values=existing_values,
    )
    if headers_initialized:
        existing_values = worksheet.get_all_values()

    if not rows:
        return {
            "status": "no_rows",
            "worksheet": target_worksheet,
            "headers_initialized": headers_initialized,
            "replaced_rows": 0,
            "insert_row": None,
            "block_end": None,
            "rows": 0,
            "clusters": [],
        }

    existing_tables = _extract_cluster_tables(existing_values)
    merged_tables, replaced_rows = _merge_replacement_rows(existing_tables, rows)
    changed_clusters = _changed_clusters(rows)

    existing_row_count = len(existing_values)
    row_count = max(
        HEADER_ROW_COUNT,
        existing_row_count,
        max((HEADER_ROW_COUNT + len(merged_tables[cluster_id]) for cluster_id in changed_clusters), default=HEADER_ROW_COUNT),
    )
    _ensure_grid_capacity(spreadsheet, worksheet, row_count, STATIC_SHEET_COL_COUNT)
    _write_cluster_data_blocks(
        worksheet,
        merged_tables,
        changed_clusters,
        row_count,
    )
    _format_linkaja_sheet(
        gspread_client,
        spreadsheet,
        worksheet,
        list(KNOWN_CLUSTER_ORDER),
        row_count,
        STATIC_SHEET_COL_COUNT,
    )

    return {
        "status": "success",
        "worksheet": target_worksheet,
        "headers_initialized": headers_initialized,
        "replaced_rows": replaced_rows,
        "insert_row": DATA_START_ROW,
        "block_end": row_count,
        "rows": len(rows),
        "clusters": changed_clusters,
    }


def setup_linkaja_fee_headers(
    gspread_client,
    spreadsheet,
    worksheet,
    existing_values: list[list[str]] | None = None,
) -> bool:
    existing_values = worksheet.get_all_values() if existing_values is None else existing_values
    if not _worksheet_is_empty(existing_values):
        return False

    _unmerge_all_cells(spreadsheet, worksheet)
    _ensure_grid_capacity(
        spreadsheet,
        worksheet,
        HEADER_ROW_COUNT,
        STATIC_SHEET_COL_COUNT,
    )
    header_values = _build_static_header_values()
    worksheet.update(
        values=uppercase_sheet_rows(header_values),
        range_name=sheet_range(
            1,
            HEADER_ROW_COUNT,
            1,
            STATIC_SHEET_COL_COUNT,
        ),
        value_input_option="USER_ENTERED",
    )
    _format_linkaja_sheet(
        gspread_client,
        spreadsheet,
        worksheet,
        list(KNOWN_CLUSTER_ORDER),
        HEADER_ROW_COUNT,
        STATIC_SHEET_COL_COUNT,
    )
    return True


def detail_worksheet_for_cluster(cluster_id: str) -> str:
    _validate_known_cluster_id(cluster_id)
    return f"{CLUSTER_DISPLAY_NAMES[cluster_id]}{DETAIL_WORKSHEET_SUFFIX}"


def _normalized_label(value: Any) -> str:
    return str(value or "").strip().upper()


def _sheet_number(value: Any) -> int | float:
    if value is None or str(value).strip() == "":
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(
            f"Cannot parse LinkAja summary value {value!r} as a number"
        ) from exc
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _scenario_sort_key(label: str) -> tuple[int, int | str]:
    normalized = _normalized_label(label)
    known = {
        _normalized_label(value): index
        for index, value in enumerate(KNOWN_SCENARIO_ORDER)
    }
    if normalized in known:
        return (0, known[normalized])
    return (1, normalized)


def _measure_rows_by_date(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        report_date = str(row.get(DETAIL_REPORT_DATE_HEADER, "")).strip()
        if not report_date:
            raise ValueError("LinkAja ledger measure row is missing REPORT DATE")
        grouped.setdefault(report_date, []).append(row)
    return grouped


def _build_detail_rows_from_measures(
    measure_rows: list[dict[str, Any]],
) -> list[list[Any]]:
    scenario_totals: dict[str, dict[str, Any]] = {}
    expected_fee = Decimal("0")
    in_cluster_fee = Decimal("0")
    reversal_count = 0
    reversal_fee = Decimal("0")
    unresolved_count = 0

    for row in measure_rows:
        scenario = str(row.get(MEASURE_SCENARIO_HEADER, "")).strip()
        if not scenario:
            raise ValueError("LinkAja ledger measure row is missing TRANSACTION SCENARIO")
        totals = scenario_totals.setdefault(
            scenario,
            {
                "company_debit": Decimal("0"),
                "company_credit": Decimal("0"),
                "source_fee": Decimal("0"),
                "source_fee_missing_count": 0,
            },
        )
        company_debit = Decimal(
            str(_sheet_number(row.get(MEASURE_COMPANY_DEBIT_HEADER)))
        )
        company_credit = Decimal(
            str(_sheet_number(row.get(MEASURE_COMPANY_CREDIT_HEADER)))
        )
        source_fee = Decimal(str(_sheet_number(row.get(MEASURE_SOURCE_FEE_HEADER))))
        totals["company_debit"] += company_debit
        totals["company_credit"] += company_credit
        totals["source_fee"] += source_fee
        totals["source_fee_missing_count"] += int(
            _sheet_number(row.get(MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER))
        )
        expected_fee += Decimal(
            str(_sheet_number(row.get(MEASURE_EXPECTED_FEE_HEADER)))
        )
        reversal_count += int(_sheet_number(row.get(MEASURE_REVERSAL_COUNT_HEADER)))
        reversal_fee += Decimal(
            str(_sheet_number(row.get(MEASURE_REVERSAL_FEE_HEADER)))
        )
        unresolved_count += int(
            _sheet_number(row.get(MEASURE_UNRESOLVED_COUNT_HEADER))
        )
        if MEASURE_IN_CLUSTER_FEE_HEADER in row:
            in_cluster_fee += Decimal(
                str(_sheet_number(row.get(MEASURE_IN_CLUSTER_FEE_HEADER)))
            )
        elif _normalized_label(scenario) == _normalized_label(IN_CLUSTER_SCENARIO):
            in_cluster_fee += source_fee

    ordered_scenarios = sorted(scenario_totals, key=_scenario_sort_key)
    if not any(
        _normalized_label(scenario) == _normalized_label(WITHDRAWAL_SCENARIO)
        for scenario in ordered_scenarios
    ):
        # Match the FinPay daily layout: every date begins with the observed
        # next-working-day payment row, even when this date has no withdrawal.
        ordered_scenarios.insert(0, WITHDRAWAL_SCENARIO)

    detail_rows: list[list[Any]] = []
    for scenario in ordered_scenarios:
        if scenario not in scenario_totals:
            detail_rows.append([WITHDRAWAL_SCENARIO, "", "", ""])
            continue
        totals = scenario_totals[scenario]
        company_debit = totals["company_debit"]
        company_credit = totals["company_credit"]
        source_fee = totals["source_fee"]
        source_fee_missing_count = totals["source_fee_missing_count"]
        debet: Any = ""
        kredit: Any = ""
        other: Any = ""
        normalized = _normalized_label(scenario)

        if normalized == _normalized_label(WITHDRAWAL_SCENARIO):
            debet = _decimal_sheet_value(company_debit)
        elif normalized == _normalized_label(POSTED_FEE_SCENARIO):
            debet = _decimal_sheet_value(company_credit)
        elif normalized == _normalized_label(PPOB_SCENARIO):
            if source_fee_missing_count == 0:
                debet = _decimal_sheet_value(source_fee)
        elif normalized in {
            _normalized_label(OUT_CLUSTER_SCENARIO),
            _normalized_label(IN_CLUSTER_SCENARIO),
            _normalized_label(REVERSAL_SCENARIO),
            _normalized_label(UNRESOLVED_REVERSAL_LABEL),
        } or normalized.startswith(_normalized_label(COMPLETE_REVERSAL_PREFIX)):
            debet = _decimal_sheet_value(company_credit)
            kredit = _decimal_sheet_value(company_debit)
        else:
            other = _decimal_sheet_value(company_credit + company_debit)

        detail_rows.append([scenario, debet, kredit, other])

    expected_fee_value = _decimal_sheet_value(expected_fee)
    detail_rows.extend([
        [
            EXPECTED_FEE_LEDGER_LABEL,
            "",
            "",
            expected_fee_value,
        ],
        [
            IN_CLUSTER_FEE_LEDGER_LABEL,
            "",
            _decimal_sheet_value(in_cluster_fee),
            "",
        ],
        [
            REVERSAL_COUNT_LEDGER_LABEL,
            "",
            "",
            reversal_count,
        ],
        [
            REVERSAL_FEE_LEDGER_LABEL,
            _decimal_sheet_value(reversal_fee),
            "",
            "",
        ],
        [
            UNRESOLVED_COUNT_LEDGER_LABEL,
            "",
            "",
            unresolved_count,
        ],
    ])
    return detail_rows


def _decimal_sheet_value(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _detail_other_value(
    label: Any,
    debet: Any,
    kredit: Any,
    other: Any,
) -> Any:
    """Keep OTHER exclusive to unclassified or explicitly derived measures."""
    if _normalized_label(label) == _normalized_label(EXPECTED_FEE_LEDGER_LABEL):
        return other
    if any(value is not None and str(value).strip() != "" for value in (debet, kredit)):
        return ""
    return other


def _normalize_detail_placement(
    label: Any,
    debet: Any,
    kredit: Any,
    other: Any,
) -> tuple[Any, Any, Any]:
    """Normalize historical rows to the current expected-fee placement."""
    if _normalized_label(label) == _normalized_label(EXPECTED_FEE_LEDGER_LABEL):
        return "", "", other
    return debet, kredit, _detail_other_value(label, debet, kredit, other)


def _extract_existing_detail_rows(
    existing_values: list[list[Any]],
) -> dict[str, list[list[Any]]]:
    if _worksheet_is_empty(existing_values):
        return {}

    headers = [_normalized_label(value) for value in existing_values[0]]
    if headers == DETAIL_HEADERS:
        return _extract_six_column_detail_rows(existing_values)
    if headers == PREVIOUS_DETAIL_HEADERS:
        return _extract_five_column_detail_rows(existing_values)
    if headers == LEGACY_DETAIL_HEADERS:
        return _extract_legacy_detail_rows(existing_values)
    raise ValueError(
        "Existing LinkAja detail worksheet header does not match the current "
        "six-column, previous five-column, or legacy nine-column layout"
    )


def _extract_six_column_detail_rows(
    existing_values: list[list[Any]],
) -> dict[str, list[list[Any]]]:
    rows_by_date: dict[str, list[list[Any]]] = {}
    for raw_row in existing_values[1:]:
        row = list(raw_row[:len(DETAIL_HEADERS)])
        row.extend([""] * (len(DETAIL_HEADERS) - len(row)))
        report_date = str(row[0]).strip()
        section = _normalized_label(row[1])
        label = _current_detail_label(row[2])
        if not report_date or section != DETAIL_SECTION_LABEL or not label:
            continue
        debet, kredit, other = _normalize_detail_placement(
            label,
            row[3],
            row[4],
            row[5],
        )
        rows_by_date.setdefault(report_date, []).append([
            label,
            debet,
            kredit,
            other,
        ])
    return rows_by_date


def _extract_five_column_detail_rows(
    existing_values: list[list[Any]],
) -> dict[str, list[list[Any]]]:
    rows_by_date: dict[str, list[list[Any]]] = {}
    detail_dates: set[str] = set()
    for raw_row in existing_values[1:]:
        row = list(raw_row[:len(PREVIOUS_DETAIL_HEADERS)])
        row.extend([""] * (len(PREVIOUS_DETAIL_HEADERS) - len(row)))
        report_date = str(row[0]).strip()
        label = _normalized_label(row[1])
        if not report_date or not label:
            continue
        if label == DETAIL_SECTION_LABEL:
            detail_dates.add(report_date)
            rows_by_date.setdefault(report_date, [])
            continue
        if label == MANDIRI_SECTION_LABEL:
            detail_dates.discard(report_date)
            continue
        if report_date in detail_dates:
            current_label = _current_detail_label(row[1])
            debet, kredit, other = _normalize_detail_placement(
                current_label,
                row[2],
                row[3],
                row[4],
            )
            rows_by_date.setdefault(report_date, []).append([
                current_label,
                debet,
                kredit,
                other,
            ])
    return rows_by_date


def _current_detail_label(value: Any) -> str:
    label = str(value or "").strip()
    normalized = _normalized_label(label)
    if normalized == _normalized_label(LEGACY_REVERSAL_COUNT_LEDGER_LABEL):
        return REVERSAL_COUNT_LEDGER_LABEL
    if normalized == _normalized_label(LEGACY_REVERSAL_FEE_LEDGER_LABEL):
        return REVERSAL_FEE_LEDGER_LABEL
    return label


def _extract_legacy_detail_rows(
    existing_values: list[list[Any]],
) -> dict[str, list[list[Any]]]:
    headers = [_normalized_label(value) for value in existing_values[0]]
    columns = {header: headers.index(header) for header in LEGACY_DETAIL_HEADERS}
    measures = []
    for row in existing_values[1:]:
        report_date = _cell_value(row, columns["REPORT DATE"])
        scenario = _cell_value(row, columns["TRANSACTION SCENARIO"])
        if not report_date or not scenario:
            continue
        measures.append({
            DETAIL_REPORT_DATE_HEADER: report_date,
            MEASURE_SCENARIO_HEADER: scenario,
            MEASURE_COMPANY_DEBIT_HEADER: _cell_value(row, columns["DEBET"]),
            MEASURE_COMPANY_CREDIT_HEADER: _cell_value(row, columns["KREDIT"]),
            MEASURE_SOURCE_FEE_HEADER: _cell_value(row, columns["SOURCE FEE"]),
            MEASURE_EXPECTED_FEE_HEADER: _cell_value(row, columns["EXPECTED FEE"]),
            MEASURE_REVERSAL_COUNT_HEADER: _cell_value(
                row,
                columns["REVERSAL IN CLUSTER COUNT"],
            ),
            MEASURE_REVERSAL_FEE_HEADER: _cell_value(
                row,
                columns["REVERSAL IN CLUSTER FEE"],
            ),
        })
    return {
        report_date: _build_detail_rows_from_measures(date_rows)
        for report_date, date_rows in _measure_rows_by_date(measures).items()
    }


def _detail_row_formula(
    detail_row_by_label: dict[str, int],
    label: str,
    column: str,
) -> str | int:
    row_number = detail_row_by_label.get(_normalized_label(label))
    return f"={column}{row_number}" if row_number else 0


def _next_withdrawal_formula() -> str:
    next_debet = 'INDIRECT("D"&ROW()+2&":D")'
    next_label = 'INDIRECT("C"&ROW()+2&":C")'
    return (
        f'=IFERROR(INDEX(FILTER({next_debet},'
        f'TRIM({next_label})="{WITHDRAWAL_SCENARIO.upper()}",'
        f'{next_debet}<>""),1),0)'
    )


def _build_ledger_values(
    detail_rows_by_date: dict[str, list[list[Any]]],
) -> list[list[Any]]:
    values: list[list[Any]] = [list(DETAIL_HEADERS)]
    for report_date in sorted(detail_rows_by_date, key=date_sort_key):
        detail_row_by_label: dict[str, int] = {}
        for detail_row in detail_rows_by_date[report_date]:
            values.append([
                report_date,
                DETAIL_SECTION_LABEL,
                *detail_row,
            ])
            detail_row_by_label[_normalized_label(detail_row[0])] = len(values)

        component_start = len(values) + 1
        mandiri_components = list(MANDIRI_COMPONENTS)
        if _normalized_label(REVERSAL_SCENARIO) in detail_row_by_label:
            mandiri_components.insert(2, REVERSAL_SCENARIO)
        for label in mandiri_components:
            debet_formula: str | int | Any = ""
            kredit_formula: str | int | Any = ""
            if _normalized_label(label) in {
                _normalized_label(value)
                for value in MANDIRI_PRINCIPAL_COMPONENTS
            }:
                debet_formula = _detail_row_formula(
                    detail_row_by_label,
                    label,
                    "D",
                )
                kredit_formula = _detail_row_formula(
                    detail_row_by_label,
                    label,
                    "E",
                )
            elif _normalized_label(label) == _normalized_label(
                REVERSAL_FEE_LEDGER_LABEL
            ):
                debet_formula = _detail_row_formula(
                    detail_row_by_label,
                    label,
                    "D",
                )
            else:
                kredit_formula = _detail_row_formula(
                    detail_row_by_label,
                    label,
                    "E",
                )
            values.append([
                report_date,
                MANDIRI_SECTION_LABEL,
                label,
                debet_formula,
                kredit_formula,
                "",
            ])
        component_end = len(values)

        total_row = len(values) + 1
        values.append([
            report_date,
            MANDIRI_SECTION_LABEL,
            TOTAL_EXPECTED_MANDIRI_LABEL,
            f"=SUM(D{component_start}:D{component_end})"
            f"-SUM(E{component_start}:E{component_end})",
            "",
            "",
        ])

        mandiri_row = len(values) + 1
        values.append([
            report_date,
            CASH_SECTION_LABEL,
            MANDIRI_SECTION_LABEL,
            _next_withdrawal_formula(),
            "",
            "",
        ])

        selisih_row = len(values) + 1
        unresolved_count_row = detail_row_by_label.get(
            _normalized_label(UNRESOLVED_COUNT_LEDGER_LABEL)
        )
        unresolved_count_reference = (
            f"F{unresolved_count_row}"
            if unresolved_count_row
            else "0"
        )
        values.append([
            report_date,
            CASH_SECTION_LABEL,
            SELISIH_LABEL,
            f'=IF(OR(D{mandiri_row}="",D{mandiri_row}=0),'
            f'"",D{mandiri_row}-D{total_row})',
            "",
            f'=IF(OR(D{mandiri_row}="",D{mandiri_row}=0),'
            f'"PENDING WITHDRAWAL",IF({unresolved_count_reference}>0,'
            f'"UNUSUAL - UNRESOLVED REVERSAL",'
            f'IF(D{selisih_row}=0,"SESUAI",'
            f'IF(D{selisih_row}>0,"LEBIH BAYAR","KURANG BAYAR"))))',
        ])
    return values


def _merge_detail_summary_values(
    existing_values: list[list[Any]],
    new_rows: list[dict[str, Any]],
    report_dates: list[str],
) -> tuple[list[list[Any]], int]:
    """Replace complete daily ledger blocks and regenerate every formula."""
    replacement_dates = {
        str(value).strip()
        for value in report_dates
        if str(value).strip()
    }
    detail_rows_by_date = _extract_existing_detail_rows(existing_values)
    replaced_rows = sum(
        1
        for row in existing_values[1:]
        if row and str(row[0]).strip() in replacement_dates
    )
    for report_date in replacement_dates:
        detail_rows_by_date.pop(report_date, None)

    incoming_by_date = _measure_rows_by_date(new_rows)
    for report_date in replacement_dates:
        detail_rows_by_date[report_date] = _build_detail_rows_from_measures(
            incoming_by_date.get(report_date, [])
        )

    return _build_ledger_values(detail_rows_by_date), replaced_rows


def process_linkaja_detail_sheet_upload(
    target_spreadsheet: str,
    cluster_id: str,
    rows: list[dict[str, Any]],
    report_dates: list[str],
    gspread_client,
) -> dict[str, Any]:
    """Write per-cluster LinkAja DETAIL and MANDIRI daily ledger blocks."""
    worksheet_title = detail_worksheet_for_cluster(cluster_id)
    spreadsheet = _open_or_create_spreadsheet(gspread_client, target_spreadsheet)
    worksheet = _open_or_create_detail_worksheet(spreadsheet, worksheet_title)
    existing_values = _get_unformatted_values(worksheet)
    values, replaced_rows = _merge_detail_summary_values(
        existing_values,
        rows,
        report_dates,
    )
    old_row_count = max(1, len(existing_values))
    old_col_count = max(
        [len(DETAIL_HEADERS), *(len(row) for row in existing_values)],
    )
    write_row_count = max(old_row_count, len(values))
    write_col_count = len(DETAIL_HEADERS)
    padded_values = [
        *[
            [*row, *([""] * (write_col_count - len(row)))]
            for row in values
        ],
        *([[""] * write_col_count] * (write_row_count - len(values))),
    ]

    _unmerge_all_cells(spreadsheet, worksheet)
    _ensure_grid_capacity(
        spreadsheet,
        worksheet,
        write_row_count,
        write_col_count,
    )
    worksheet.update(
        values=uppercase_sheet_rows(padded_values),
        range_name=sheet_range(
            1,
            write_row_count,
            1,
            write_col_count,
        ),
        value_input_option="USER_ENTERED",
    )
    if old_col_count > write_col_count:
        worksheet.batch_clear([
            sheet_range(
                1,
                old_row_count,
                write_col_count + 1,
                old_col_count,
            )
        ])
    _format_linkaja_detail_sheet(
        gspread_client,
        spreadsheet,
        worksheet,
        cluster_id,
        values,
    )
    return {
        "status": "success",
        "worksheet": worksheet_title,
        "cluster_id": cluster_id,
        "replaced_rows": replaced_rows,
        "rows": len(rows),
        "report_dates": sorted(set(report_dates), key=date_sort_key),
    }


def write_linkaja_monthly_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Render one LinkAja monthly preview/publication in a stable worksheet."""
    cluster_id = str(result.get("cluster_id", "")).strip()
    report_month = str(result.get("report_month", "")).strip()[:7]
    if not cluster_id or not report_month:
        raise ValueError("Monthly Google Sheet output needs cluster_id and report_month")

    spreadsheet = _open_or_create_spreadsheet(gspread_client, target_spreadsheet)
    worksheet_title = f"LINKAJA MONTHLY {cluster_id} {report_month}"
    try:
        worksheet = spreadsheet.worksheet(worksheet_title)
    except Exception:
        worksheet = spreadsheet.add_worksheet(title=worksheet_title, rows=500, cols=17)

    unresolved_rows = result.get("unresolved_reversal_rows", [])
    fee_rows = result.get("fee_rows", [])
    unresolved_count = int(result.get("unresolved_reversal_count", 0))
    incomplete_fee_count = int(result.get("incomplete_fee_rows", 0))
    if not result.get("published"):
        publication_status = "PREVIEW - NOT FINAL"
    elif unresolved_count or incomplete_fee_count:
        publication_status = "PUBLISHED WITH EXCEPTIONS - REVIEW REQUIRED"
    else:
        publication_status = "PUBLISHED"

    fee_headers = [
        "FEE CATEGORY", "GROSS TRANSACTION COUNT", "REVERSED TRANSACTION COUNT",
        "ACTIVE TRANSACTION COUNT", "GROSS FEE", "REVERSED FEE", "NET FEE",
        "MONTHLY PAYABLE FEE", "GROSS MISSING FEE COUNT",
        "REVERSED MISSING FEE COUNT", "ACTIVE MISSING FEE COUNT",
        "CALCULATION STATUS",
    ]
    values = [
        [f"LINKAJA MONTHLY SETTLEMENT - {cluster_id} - {report_month}"],
        ["STATUS", publication_status, "BUSINESS CUTOFF", result.get("calculation_cutoff", ""),
         "PUBLISHED", str(bool(result.get("published")))],
        ["TRANSACTION FACTS", result.get("transaction_rows", 0),
         "UNRESOLVED REVERSALS", unresolved_count,
         "INCOMPLETE FEE CATEGORIES", incomplete_fee_count],
        [],
        ["MONTHLY FEE SUMMARY"],
        fee_headers,
    ]
    values.extend([[row.get(header, "") for header in fee_headers] for row in fee_rows])
    exception_title_row = len(values) + 2
    values.extend([
        [],
        ["UNRESOLVED REVERSALS"],
        UNRESOLVED_REVERSAL_HEADERS,
    ])
    if unresolved_rows:
        values.extend([
            [row.get(header, "") for header in UNRESOLVED_REVERSAL_HEADERS]
            for row in unresolved_rows
        ])
    else:
        values.append(["NONE"] + [""] * (len(UNRESOLVED_REVERSAL_HEADERS) - 1))

    width = max(len(row) for row in values)
    values = [row + [""] * (width - len(row)) for row in values]
    unprotect_requests = _delete_all_protected_range_requests(spreadsheet, worksheet)
    if unprotect_requests:
        spreadsheet.batch_update({"requests": unprotect_requests})
    _unmerge_all_cells(spreadsheet, worksheet)
    worksheet.clear()
    _ensure_grid_capacity(spreadsheet, worksheet, len(values), width)
    worksheet.update(
        values=uppercase_sheet_rows(values),
        range_name=sheet_range(1, len(values), 1, width),
        value_input_option="USER_ENTERED",
    )

    navy = {"red": 0.122, "green": 0.306, "blue": 0.471}
    fee_header = {"red": 0.765, "green": 0.890, "blue": 0.741}
    fee_body = {"red": 0.925, "green": 0.973, "blue": 0.910}
    exception_header = {"red": 0.925, "green": 0.690, "blue": 0.690}
    exception_body = {"red": 0.992, "green": 0.925, "blue": 0.925}
    requests = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": worksheet.id,
                               "gridProperties": {"frozenRowCount": 6}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "mergeCells": {
                "range": _grid_range(worksheet, 1, 1, 1, width),
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 1, 1, 1, width),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": navy,
                    "textFormat": {"bold": True, "foregroundColor": WHITE_COLOR, "fontSize": 12},
                    "horizontalAlignment": "CENTER",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 6, 6, 1, len(fee_headers)),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": fee_header,
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                    "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 7, max(6 + len(fee_rows), 7), 1, len(fee_headers)),
                "cell": {"userEnteredFormat": {"backgroundColor": fee_body}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, exception_title_row, exception_title_row + 1,
                                     1, len(UNRESOLVED_REVERSAL_HEADERS)),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": exception_header,
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                    "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, exception_title_row + 2, len(values),
                                     1, len(UNRESOLVED_REVERSAL_HEADERS)),
                "cell": {"userEnteredFormat": {"backgroundColor": exception_body}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        {
            "updateBorders": {
                "range": _grid_range(worksheet, 1, len(values), 1, width),
                "top": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "bottom": {"style": "SOLID", "color": BORDER_COLOR},
                "left": {"style": "SOLID", "color": BORDER_COLOR},
                "right": {"style": "SOLID", "color": BORDER_COLOR},
                "innerHorizontal": {"style": "SOLID", "color": BORDER_COLOR},
                "innerVertical": {"style": "SOLID", "color": BORDER_COLOR},
            }
        },
        _add_protected_sheet_request(gspread_client, worksheet,
                                     "LinkAja monthly settlement output"),
    ]
    for index, width_px in enumerate([220, 150, 155, 145, 130, 130, 130, 155, 150, 155, 150, 220]):
        requests.append(_column_width_request(worksheet, index, index + 1, width_px))
    spreadsheet.batch_update({"requests": requests})
    return {
        "status": "success",
        "spreadsheet": target_spreadsheet,
        "worksheet": worksheet_title,
        "publication_status": publication_status,
        "fee_rows": len(fee_rows),
        "unresolved_rows": len(unresolved_rows),
    }


def _get_unformatted_values(worksheet) -> list[list[Any]]:
    try:
        from gspread.utils import ValueRenderOption

        return worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted,
        )
    except (ImportError, TypeError):
        return worksheet.get_all_values()


def _open_or_create_spreadsheet(gspread_client, title: str):
    import gspread

    try:
        return gspread_client.open(title)
    except gspread.SpreadsheetNotFound:
        return gspread_client.create(title)


def _open_or_create_worksheet(spreadsheet, title: str):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=STATIC_SHEET_COL_COUNT)


def _open_or_create_detail_worksheet(spreadsheet, title: str):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=len(DETAIL_HEADERS))


def _extract_cluster_tables(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    if not existing_rows:
        return _empty_known_cluster_tables()

    legacy_rows = _extract_legacy_single_table(existing_rows)
    if legacy_rows:
        return _normalize_known_cluster_tables(legacy_rows)

    return _extract_static_side_by_side_tables(existing_rows)


def _extract_legacy_single_table(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    headers = [str(value).strip().upper() for value in existing_rows[0]]
    required_headers = [
        REPORT_DATE_HEADER,
        CLUSTER_ID_HEADER,
        EXPECTED_FEE_HEADER,
        IN_CLUSTER_FEE_HEADER,
        TOTAL_FEE_HEADER,
    ]
    if not all(header in headers for header in required_headers):
        return {}

    columns = {header: headers.index(header) for header in required_headers}
    tables: dict[str, list[dict[str, Any]]] = {}
    for row in existing_rows[1:]:
        report_date = _cell_value(row, columns[REPORT_DATE_HEADER])
        cluster_id = _cell_value(row, columns[CLUSTER_ID_HEADER])
        if not report_date or not cluster_id:
            continue
        tables.setdefault(cluster_id, []).append({
            REPORT_DATE_HEADER: report_date,
            CLUSTER_ID_HEADER: cluster_id,
            EXPECTED_FEE_HEADER: _cell_value(row, columns[EXPECTED_FEE_HEADER]),
            IN_CLUSTER_FEE_HEADER: _cell_value(row, columns[IN_CLUSTER_FEE_HEADER]),
            TOTAL_FEE_HEADER: _cell_value(row, columns[TOTAL_FEE_HEADER]),
        })

    return tables


def _extract_static_side_by_side_tables(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    if len(existing_rows) < 2:
        return _empty_known_cluster_tables()

    tables = _empty_known_cluster_tables()
    for cluster_id in KNOWN_CLUSTER_ORDER:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        cluster_rows = []
        for row in existing_rows[2:]:
            report_date = _cell_value(row, start_column)
            if not report_date:
                continue
            cluster_rows.append({
                REPORT_DATE_HEADER: report_date,
                CLUSTER_ID_HEADER: cluster_id,
                EXPECTED_FEE_HEADER: _cell_value(row, start_column + 1),
                IN_CLUSTER_FEE_HEADER: _cell_value(row, start_column + 2),
                TOTAL_FEE_HEADER: _cell_value(row, start_column + 3),
            })
        tables[cluster_id] = cluster_rows

    return _sort_cluster_table_rows(tables)


def _cluster_id_from_title(value: str) -> str | None:
    match = re.match(r"\s*(?:CLUSTER\s+|[A-Z]{2,10}\s+-\s+)?(\d+)\s*$", value, re.IGNORECASE)
    return match.group(1) if match else None


def _cluster_title(cluster_id: str) -> str:
    display_name = CLUSTER_DISPLAY_NAMES.get(cluster_id)
    if display_name:
        return f"{display_name} - {cluster_id}"
    return cluster_id


def _merge_replacement_rows(
    existing_tables: dict[str, list[dict[str, Any]]],
    new_rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    tables = _normalize_known_cluster_tables(deepcopy(existing_tables))
    replaced_rows = 0

    for row in new_rows:
        cluster_id = str(row.get(CLUSTER_ID_HEADER, "")).strip()
        report_date = str(row.get(REPORT_DATE_HEADER, "")).strip()
        if not cluster_id:
            raise ValueError(f"LinkAja fee row is missing {CLUSTER_ID_HEADER}")
        if not report_date:
            raise ValueError(f"LinkAja fee row is missing {REPORT_DATE_HEADER}")
        _validate_known_cluster_id(cluster_id)

        existing_rows = tables.get(cluster_id, [])
        kept_rows = [
            existing_row
            for existing_row in existing_rows
            if str(existing_row.get(REPORT_DATE_HEADER, "")).strip() != report_date
        ]
        replaced_rows += len(existing_rows) - len(kept_rows)
        kept_rows.append(_visible_cluster_row(row, cluster_id, report_date))
        tables[cluster_id] = sorted(kept_rows, key=lambda item: date_sort_key(item[REPORT_DATE_HEADER]))

    return _sort_cluster_table_rows(tables), replaced_rows


def _visible_cluster_row(
    row: dict[str, Any],
    cluster_id: str,
    report_date: str,
) -> dict[str, Any]:
    return {
        REPORT_DATE_HEADER: report_date,
        CLUSTER_ID_HEADER: cluster_id,
        EXPECTED_FEE_HEADER: row.get(EXPECTED_FEE_HEADER, ""),
        IN_CLUSTER_FEE_HEADER: row.get(IN_CLUSTER_FEE_HEADER, ""),
        TOTAL_FEE_HEADER: row.get(TOTAL_FEE_HEADER, ""),
    }


def _sort_cluster_table_rows(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        cluster_id: sorted(rows, key=lambda item: date_sort_key(str(item[REPORT_DATE_HEADER])))
        for cluster_id, rows in _normalize_known_cluster_tables(tables).items()
    }


def _empty_known_cluster_tables() -> dict[str, list[dict[str, Any]]]:
    return {cluster_id: [] for cluster_id in KNOWN_CLUSTER_ORDER}


def _normalize_known_cluster_tables(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    normalized = _empty_known_cluster_tables()
    for cluster_id, rows in tables.items():
        _validate_known_cluster_id(cluster_id)
        normalized[cluster_id] = rows
    return normalized


def _validate_known_cluster_id(cluster_id: str) -> None:
    if cluster_id not in KNOWN_CLUSTER_ORDER:
        known_clusters = ", ".join(KNOWN_CLUSTER_ORDER)
        raise ValueError(
            f"Unknown LinkAja cluster ID {cluster_id!r}; expected one of: {known_clusters}"
        )


def _changed_clusters(rows: list[dict[str, Any]]) -> list[str]:
    cluster_ids = {
        str(row.get(CLUSTER_ID_HEADER, "")).strip()
        for row in rows
    }
    for cluster_id in cluster_ids:
        _validate_known_cluster_id(cluster_id)
    return [
        cluster_id
        for cluster_id in KNOWN_CLUSTER_ORDER
        if cluster_id in cluster_ids
    ]


def _worksheet_is_empty(existing_values: list[list[str]]) -> bool:
    return not any(
        str(value).strip()
        for row in existing_values
        for value in row
    )


def _build_static_header_values() -> list[list[Any]]:
    values = [["" for _ in range(STATIC_SHEET_COL_COUNT)] for _ in range(HEADER_ROW_COUNT)]

    for cluster_id in KNOWN_CLUSTER_ORDER:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        values[0][start_column] = _cluster_title(cluster_id)
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[1][start_column + header_offset] = header

    return values


def _write_cluster_data_blocks(
    worksheet,
    tables: dict[str, list[dict[str, Any]]],
    cluster_ids: list[str],
    row_count: int,
) -> None:
    if row_count < DATA_START_ROW:
        return

    for cluster_id in cluster_ids:
        _validate_known_cluster_id(cluster_id)
        start_column = CLUSTER_START_COLUMNS[cluster_id] + 1
        end_column = start_column + TABLE_WIDTH - 1
        values = _cluster_data_block_values(tables[cluster_id], row_count)
        worksheet.update(
            values=uppercase_sheet_rows(values),
            range_name=sheet_range(
                DATA_START_ROW,
                row_count,
                start_column,
                end_column,
            ),
            value_input_option="USER_ENTERED",
        )


def _cluster_data_block_values(
    rows: list[dict[str, Any]],
    row_count: int,
) -> list[list[Any]]:
    data_row_count = row_count - HEADER_ROW_COUNT
    values = [["" for _ in range(TABLE_WIDTH)] for _ in range(data_row_count)]
    for row_index, row in enumerate(rows[:data_row_count]):
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[row_index][header_offset] = row.get(header, "")
    return values


def _build_side_by_side_values(
    tables: dict[str, list[dict[str, Any]]],
) -> tuple[list[list[Any]], list[str]]:
    tables = _normalize_known_cluster_tables(tables)
    clusters = list(KNOWN_CLUSTER_ORDER)
    max_data_rows = max((len(rows) for rows in tables.values()), default=0)
    col_count = INITIAL_CLUSTER_COUNT * BLOCK_WIDTH - SPACER_WIDTH
    row_count = max(2 + max_data_rows, 2)
    values = [["" for _ in range(col_count)] for _ in range(row_count)]

    for cluster_id in clusters:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        values[0][start_column] = _cluster_title(cluster_id)
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[1][start_column + header_offset] = header

        for row_offset, row in enumerate(tables[cluster_id], start=2):
            for header_offset, header in enumerate(TABLE_HEADERS):
                values[row_offset][start_column + header_offset] = row.get(header, "")

    return values, clusters


def _cell_value(row: list[Any], column_index: int) -> str:
    if column_index >= len(row):
        return ""
    return str(row[column_index]).strip()


def _unmerge_all_cells(spreadsheet, worksheet) -> None:
    row_count = max(int(getattr(worksheet, "row_count", 1) or 1), 1)
    col_count = max(int(getattr(worksheet, "col_count", 1) or 1), 1)
    spreadsheet.batch_update({"requests": [{
        "unmergeCells": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": 0,
                "endRowIndex": row_count,
                "startColumnIndex": 0,
                "endColumnIndex": col_count,
            }
        }
    }]})


def _ensure_grid_capacity(
    spreadsheet,
    worksheet,
    required_rows: int,
    required_cols: int,
) -> None:
    current_rows = int(getattr(worksheet, "row_count", 0) or 0)
    current_cols = int(getattr(worksheet, "col_count", 0) or 0)
    target_rows = max(current_rows, required_rows + DEFAULT_ROW_BUFFER)
    target_cols = max(current_cols, required_cols + DEFAULT_COL_BUFFER)
    if current_rows >= required_rows and current_cols >= required_cols:
        return

    spreadsheet.batch_update({"requests": [{
        "updateSheetProperties": {
            "properties": {
                "sheetId": worksheet.id,
                "gridProperties": {
                    "rowCount": target_rows,
                    "columnCount": target_cols,
                },
            },
            "fields": "gridProperties(rowCount,columnCount)",
        }
    }]})


def _format_linkaja_sheet(
    gspread_client,
    spreadsheet,
    worksheet,
    clusters: list[str],
    row_count: int,
    col_count: int,
) -> None:
    requests = [
        *_delete_all_protected_range_requests(spreadsheet, worksheet),
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"frozenRowCount": 2},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    ]

    for cluster_index, _cluster_id in enumerate(clusters):
        start_column = cluster_index * BLOCK_WIDTH
        end_column = start_column + TABLE_WIDTH
        palette = CLUSTER_PALETTES[cluster_index % len(CLUSTER_PALETTES)]
        requests.extend(_cluster_format_requests(
            worksheet,
            start_column,
            end_column,
            row_count,
            palette,
        ))

        spacer_column = end_column
        if spacer_column < col_count:
            requests.append(
                _column_width_request(
                    worksheet,
                    spacer_column,
                    spacer_column + 1,
                    SPACER_COLUMN_WIDTH,
                )
            )

    protected_sheet_request = _add_protected_sheet_request(
        gspread_client,
        worksheet,
        "LinkAja protected fee summary sheet",
    )
    requests.append(protected_sheet_request)

    spreadsheet.batch_update({"requests": requests})


def _format_linkaja_detail_sheet(
    gspread_client,
    spreadsheet,
    worksheet,
    cluster_id: str,
    values: list[list[Any]],
) -> None:
    row_count = len(values)
    body_end_row = max(row_count, 2)

    # Keep the LinkAja daily ledger visually consistent with FinPay's current
    # cash-flow summary: white/striped DETAIL rows, a pale-blue MANDIRI block,
    # and a dedicated Cash reconciliation block.
    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE = {"red": 1, "green": 1, "blue": 1}
    COL_DETAIL_ALT = {"red": 0.965, "green": 0.980, "blue": 0.992}
    COL_FOOTER_HEADER = {"red": 0.800, "green": 0.880, "blue": 0.950}
    COL_FOOTER_BODY = {"red": 0.900, "green": 0.940, "blue": 0.980}
    COL_STATUS = {"red": 0.965, "green": 0.930, "blue": 0.990}
    COL_INPUT = COL_STATUS
    COL_DEFAULT_TEXT = {"red": 0, "green": 0, "blue": 0}
    COL_MUTED_TEXT = {"red": 0.420, "green": 0.420, "blue": 0.420}
    IDR = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    detail_runs: list[tuple[int, int]] = []
    mandiri_runs: list[tuple[int, int]] = []
    cash_runs: list[tuple[int, int]] = []
    cash_mandiri_rows: list[int] = []
    total_rows: list[int] = []
    selisih_rows: list[int] = []

    run_start: int | None = None
    run_date = ""
    run_section = ""

    def _finish_run() -> None:
        if run_start is None:
            return
        run_end = row_number - 1
        if run_section == _normalized_label(DETAIL_SECTION_LABEL):
            detail_runs.append((run_start, run_end))
        elif run_section == _normalized_label(MANDIRI_SECTION_LABEL):
            mandiri_runs.append((run_start, run_end))
        elif run_section == _normalized_label(CASH_SECTION_LABEL):
            cash_runs.append((run_start, run_end))

    for row_number, row in enumerate(values[1:], start=2):
        report_date = str(row[0]).strip() if row else ""
        section = _normalized_label(row[1] if len(row) > 1 else "")
        label = _normalized_label(row[2] if len(row) > 2 else "")
        is_layout_section = section in {
            _normalized_label(DETAIL_SECTION_LABEL),
            _normalized_label(MANDIRI_SECTION_LABEL),
            _normalized_label(CASH_SECTION_LABEL),
        }
        if not is_layout_section:
            _finish_run()
            run_start = None
            run_date = ""
            run_section = ""
            continue
        if (
            run_start is not None
            and (report_date != run_date or section != run_section)
        ):
            _finish_run()
            run_start = None
        if run_start is None:
            run_start = row_number
            run_date = report_date
            run_section = section

        if (
            section == _normalized_label(CASH_SECTION_LABEL)
            and label == _normalized_label(MANDIRI_SECTION_LABEL)
        ):
            cash_mandiri_rows.append(row_number)
        if label == _normalized_label(TOTAL_EXPECTED_MANDIRI_LABEL):
            total_rows.append(row_number)
        if label == _normalized_label(SELISIH_LABEL):
            selisih_rows.append(row_number)

    if run_start is not None:
        run_end = row_count
        if run_section == _normalized_label(DETAIL_SECTION_LABEL):
            detail_runs.append((run_start, run_end))
        elif run_section == _normalized_label(MANDIRI_SECTION_LABEL):
            mandiri_runs.append((run_start, run_end))
        elif run_section == _normalized_label(CASH_SECTION_LABEL):
            cash_runs.append((run_start, run_end))

    requests = [
        *_delete_all_protected_range_requests(spreadsheet, worksheet),
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 1, 1, 1, len(DETAIL_HEADERS)),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COL_HEADER,
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "foregroundColor": WHITE_COLOR,
                            "bold": True,
                        },
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,horizontalAlignment,"
                    "verticalAlignment,textFormat,wrapStrategy)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 2, body_end_row, 1, len(DETAIL_HEADERS)),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COL_WHITE,
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": COL_DEFAULT_TEXT,
                        },
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,verticalAlignment,textFormat)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 2, body_end_row, 4, 6),
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": IDR,
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "updateBorders": {
                "range": _grid_range(
                    worksheet,
                    1,
                    max(row_count, 1),
                    1,
                    len(DETAIL_HEADERS),
                ),
                "top": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "bottom": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "left": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "right": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "innerHorizontal": {"style": "SOLID", "color": BORDER_COLOR},
                "innerVertical": {"style": "SOLID", "color": BORDER_COLOR},
            }
        },
    ]
    def _format_report_section(
        start_row: int,
        end_row: int,
        first_row_color: dict,
        body_color: dict,
    ) -> None:
        requests.extend([
            {
                "repeatCell": {
                    "range": _grid_range(
                        worksheet,
                        start_row,
                        end_row,
                        1,
                        len(DETAIL_HEADERS),
                    ),
                    "cell": {"userEnteredFormat": {"backgroundColor": body_color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            },
            {
                "repeatCell": {
                    "range": _grid_range(worksheet, start_row, end_row, 4, 6),
                    "cell": {"userEnteredFormat": {"numberFormat": IDR}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
        ])
        if end_row > start_row:
            requests.append({
                "repeatCell": {
                    "range": _grid_range(worksheet, start_row + 1, end_row, 1, 2),
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"foregroundColor": COL_MUTED_TEXT},
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.foregroundColor",
                }
            })
        requests.append({
            "repeatCell": {
                "range": _grid_range(
                    worksheet,
                    start_row,
                    start_row,
                    1,
                    2,
                ),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": first_row_color,
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": COL_HEADER,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    def _format_detail_stripes(start_row: int, end_row: int) -> None:
        for row_number in range(start_row, end_row + 1, 2):
            stripe_start_column = 3 if row_number == start_row else 1
            requests.append({
                "repeatCell": {
                    "range": _grid_range(
                        worksheet,
                        row_number,
                        row_number,
                        stripe_start_column,
                        len(DETAIL_HEADERS),
                    ),
                    "cell": {
                        "userEnteredFormat": {"backgroundColor": COL_DETAIL_ALT},
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    for start_row, end_row in detail_runs:
        _format_report_section(start_row, end_row, COL_FOOTER_HEADER, COL_WHITE)
        _format_detail_stripes(start_row, end_row)
    for start_row, end_row in mandiri_runs:
        _format_report_section(
            start_row,
            end_row,
            COL_FOOTER_HEADER,
            COL_FOOTER_BODY,
        )
    for start_row, end_row in cash_runs:
        _format_report_section(start_row, end_row, COL_INPUT, COL_STATUS)

    for row_number in cash_mandiri_rows:
        requests.append({
            "repeatCell": {
                "range": _grid_range(
                    worksheet,
                    row_number,
                    row_number,
                    1,
                    len(DETAIL_HEADERS),
                ),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COL_INPUT,
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": COL_HEADER,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })
    for row_number in selisih_rows:
        requests.append({
            "repeatCell": {
                "range": _grid_range(
                    worksheet,
                    row_number,
                    row_number,
                    1,
                    len(DETAIL_HEADERS),
                ),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COL_STATUS,
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": COL_HEADER,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    for row_number in [
        start_row for start_row, _end_row in [*detail_runs, *mandiri_runs]
    ]:
        requests.append({
            "updateBorders": {
                "range": _grid_range(
                    worksheet,
                    row_number,
                    row_number,
                    1,
                    len(DETAIL_HEADERS),
                ),
                "top": {"style": "SOLID_MEDIUM", "color": COL_HEADER},
            }
        })
    for row_number in [*total_rows, *selisih_rows]:
        requests.append({
            "updateBorders": {
                "range": _grid_range(
                    worksheet,
                    row_number,
                    row_number,
                    4,
                    4,
                ),
                "top": {"style": "SOLID_MEDIUM", "color": COL_HEADER},
            }
        })
    for column_index, pixel_size in enumerate(DETAIL_COLUMN_WIDTHS):
        requests.append(
            _column_width_request(
                worksheet,
                column_index,
                column_index + 1,
                pixel_size,
            )
        )
    requests.append(
        _add_protected_sheet_request(
            gspread_client,
            worksheet,
            "LinkAja protected DETAIL, MANDIRI, and Cash ledger sheet",
        )
    )
    spreadsheet.batch_update({"requests": requests})


def _delete_all_protected_range_requests(spreadsheet, worksheet) -> list[dict]:
    metadata = spreadsheet.fetch_sheet_metadata({
        "fields": "sheets(properties(sheetId),protectedRanges(protectedRangeId))",
    })
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != worksheet.id:
            continue
        for protected_range in sheet.get("protectedRanges", []):
            requests.append({
                "deleteProtectedRange": {
                    "protectedRangeId": protected_range["protectedRangeId"],
                }
            })
    return requests


def _add_protected_sheet_request(
    gspread_client,
    worksheet,
    description: str,
) -> dict:
    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": {"sheetId": worksheet.id},
                "description": description,
                "warningOnly": False,
                "editors": {
                    "users": _protection_editor_emails(gspread_client),
                    "domainUsersCanEdit": False,
                },
            }
        }
    }


def _protection_editor_emails(gspread_client) -> list[str]:
    emails = set(_split_email_list(os.environ.get("FINPAY_PROTECTION_EDITOR_EMAILS")))
    emails.update(_split_email_list(os.environ.get("LINKAJA_PROTECTION_EDITOR_EMAILS")))

    service_account_email = _service_account_email(gspread_client)
    if service_account_email:
        emails.add(service_account_email)

    return sorted(emails)


def _service_account_email(gspread_client) -> str | None:
    auth = getattr(gspread_client, "auth", None)
    if auth is None:
        auth = getattr(getattr(gspread_client, "http_client", None), "auth", None)
    return (
        getattr(auth, "service_account_email", None)
        or getattr(auth, "signer_email", None)
    )


def _split_email_list(value: str | None) -> list[str]:
    if not value:
        return []
    if value.strip().upper() == "__EMPTY__":
        return []
    return [
        email.strip()
        for email in value.split(",")
        if email.strip()
    ]


def _cluster_format_requests(
    worksheet,
    start_column: int,
    end_column: int,
    row_count: int,
    palette: dict[str, dict[str, float]],
) -> list[dict]:
    body_end_row = max(row_count, 3)
    return [
        {
            "mergeCells": {
                "range": _grid_range(worksheet, 1, 1, start_column + 1, end_column),
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 1, 1, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["title"],
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"foregroundColor": WHITE_COLOR, "bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 2, 2, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["header"],
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 3, body_end_row, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["body"],
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 3, body_end_row, start_column + 2, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "updateBorders": {
                "range": _grid_range(worksheet, 1, max(row_count, 2), start_column + 1, end_column),
                "top": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "bottom": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "left": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "right": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "innerHorizontal": {"style": "SOLID", "color": BORDER_COLOR},
                "innerVertical": {"style": "SOLID", "color": BORDER_COLOR},
            }
        },
        _column_width_request(
            worksheet,
            start_column,
            start_column + 1,
            REPORT_DATE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 1,
            start_column + 2,
            EXPECTED_FEE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 2,
            start_column + 3,
            IN_CLUSTER_FEE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 3,
            start_column + 4,
            TOTAL_FEE_COLUMN_WIDTH,
        ),
    ]


def _grid_range(
    worksheet,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
) -> dict:
    return {
        "sheetId": worksheet.id,
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,
        "startColumnIndex": start_col - 1,
        "endColumnIndex": end_col,
    }


def _column_width_request(
    worksheet,
    start_column_index: int,
    end_column_index: int,
    pixel_size: int,
) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": worksheet.id,
                "dimension": "COLUMNS",
                "startIndex": start_column_index,
                "endIndex": end_column_index,
            },
            "properties": {"pixelSize": pixel_size},
            "fields": "pixelSize",
        }
    }
