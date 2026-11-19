"""Governed cell publication with explicit targets and read-only retries."""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from .cell_calculation import build_cluster_report, recalculate_stale_history
from .cell_contracts import business_date, fingerprint
from .cell_database import validate_cluster_batch
from .cell_sheets import (
    DESCRIPTION_WIDTH_PX, REFERENCE_COLORS, cell_grid_capacity, column_letter,
    mandiri_reset_dates, render_cell_matrix, section_border_rows, section_fill,
)

GUARD_PREFIX = "FINPAY CELL REFRESH V1:"
MANAGED_PREFIX = "FINPAY CELL REPORT V1:"
MAX_REQUEST_BYTES = 1_500_000
PUBLICATION_METADATA_KEY = "finpay_cell_publication_v2"
_TRANSIENT_READ_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_READ_RETRY_DELAYS = (2, 4, 8, 16, 32)
_MAX_READ_ATTEMPTS = len(_READ_RETRY_DELAYS) + 1
_MAX_RETRY_AFTER_SECONDS = 60
_SAFE_API_STATUSES = frozenset({
    "ABORTED", "ALREADY_EXISTS", "CANCELLED", "DATA_LOSS", "DEADLINE_EXCEEDED",
    "FAILED_PRECONDITION", "INTERNAL", "INVALID_ARGUMENT", "NOT_FOUND",
    "OUT_OF_RANGE", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "UNAUTHENTICATED",
    "UNAVAILABLE", "UNIMPLEMENTED", "UNKNOWN",
})
_SAFE_API_REASONS = frozenset({
    "accessNotConfigured", "backendError", "dailyLimitExceeded", "failedPrecondition",
    "forbidden", "insufficientPermissions", "internalError", "notFound", "quotaExceeded",
    "rateLimitExceeded", "resourceExhausted", "userRateLimitExceeded",
})
_SAFE_CELL_SHEET_PHASES = frozenset({"add_sheet", "add_sheet_readback"})
_SAFE_CELL_SHEET_REASONS = frozenset({
    "committed_worksheet_missing", "committed_worksheet_identity_conflict",
    "committed_sheet_snapshot_mismatch", "unpublished_worksheet_not_empty",
    "unpublished_worksheet_has_unmanaged_state", "unpublished_worksheet_grid_too_large",
})


def cell_sheet_validation_error(message, safe_reason):
    """Attach an allowlisted diagnostic code without logging raw target details."""
    if safe_reason not in _SAFE_CELL_SHEET_REASONS:
        raise ValueError("unknown safe cell Sheet failure reason")
    error = ValueError(message)
    error.safe_reason = safe_reason
    return error


def _read_with_retry(read_call):
    for attempt in range(_MAX_READ_ATTEMPTS):
        try:
            return read_call()
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            error = getattr(exc, "error", {})
            details = error.get("errors", []) if isinstance(error, dict) else []
            quota_limited = status_code == 403 and isinstance(details, list) and any(
                isinstance(item, dict) and item.get("domain") == "usageLimits"
                and item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
                for item in details
            )
            retryable_status = status_code in _TRANSIENT_READ_STATUS_CODES or quota_limited
            if (not retryable_status
                    and not isinstance(exc, (RequestsTimeout, RequestsConnectionError))):
                raise
            if attempt == len(_READ_RETRY_DELAYS):
                raise

            delay = _READ_RETRY_DELAYS[attempt]
            if retryable_status:
                headers = getattr(response, "headers", {})
                retry_after = next((value for key, value in headers.items()
                                    if str(key).lower() == "retry-after"), None)
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        raise exc from None
                    if (not math.isfinite(delay) or delay < 0
                            or delay > _MAX_RETRY_AFTER_SECONDS):
                        raise
            time.sleep(delay)


def safe_cell_sheet_error(exc):
    """Return bounded exception-class and allowlisted API diagnostics only."""
    summary = {"error_types": []}
    seen = set()
    current = exc

    for _ in range(8):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        summary["error_types"].append(type(current).__name__)

        safe_reason = getattr(current, "safe_reason", None)
        if ("safe_reason" not in summary and isinstance(safe_reason, str)
                and safe_reason in _SAFE_CELL_SHEET_REASONS):
            summary["safe_reason"] = safe_reason

        if "http_status" not in summary:
            response = getattr(current, "response", None)
            status_code = getattr(response, "status_code", None)
            if type(status_code) is int and 100 <= status_code <= 599:
                summary["http_status"] = status_code

        details = getattr(current, "errordict", None)
        if not isinstance(details, dict):
            details = getattr(current, "error", None)
        if isinstance(details, dict):
            status = details.get("status")
            if "api_status" not in summary and isinstance(status, str) and status in _SAFE_API_STATUSES:
                summary["api_status"] = status
            errors = details.get("errors", [])
            if "api_reason" not in summary and isinstance(errors, list):
                for item in errors[:8]:
                    reason = item.get("reason") if isinstance(item, dict) else None
                    if isinstance(reason, str) and reason in _SAFE_API_REASONS:
                        summary["api_reason"] = reason
                        break

        phase = getattr(current, "safe_phase", None)
        if ("setup_phase" not in summary and isinstance(phase, str)
                and phase in _SAFE_CELL_SHEET_PHASES):
            summary["setup_phase"] = phase

        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    return summary


def _marker_entries(metadata):
    entries = [item for item in metadata.get("developerMetadata", [])
               if item.get("metadataKey") == PUBLICATION_METADATA_KEY]
    if len(entries) > 1:
        raise ValueError("duplicate publication metadata requires reconciliation")
    return entries


def _marker_request(target, metadata, marker):
    entries = _marker_entries(metadata)
    value = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    if entries:
        return {"updateDeveloperMetadata": {
            "dataFilters": [{"developerMetadataLookup": {"metadataId": entries[0]["metadataId"]}}],
            "developerMetadata": {"metadataValue": value}, "fields": "metadataValue",
        }}
    return {"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": PUBLICATION_METADATA_KEY, "metadataValue": value,
        "location": {"sheetId": target.worksheet_id}, "visibility": "DOCUMENT",
    }}}


def _verify_marker(metadata, expected):
    entries = _marker_entries(metadata)
    if not entries or json.loads(entries[0].get("metadataValue", "null")) != expected:
        raise ValueError("publication generation metadata readback mismatch")


def _status_requests(target, marker):
    pending = marker["status"] != "COMPLETE"
    color = {"red": 1, "green": 0.75, "blue": 0.15} if pending else {"red": 0.18, "green": 0.55, "blue": 0.30}
    note = (
        f"Sheet publication INCOMPLETE; epoch {marker['epoch']}. Amber tab: do not use this report."
        if pending else
        f"Sheet contents VERIFIED; epoch {marker['epoch']}. Green confirms Sheets only. "
        "Database completion is tracked separately; a reported pipeline failure requires operator reconciliation."
    )
    if marker["status"] == "DESIGN_PREVIEW":
        color = {"red": 0.20, "green": 0.45, "blue": 0.75}
        note = "SYNTHETIC DESIGN PREVIEW ONLY. No database publication was updated by this presentation change."
    return [
        {"updateSheetProperties": {"properties": {"sheetId": target.worksheet_id,
            "tabColorStyle": {"rgbColor": color}}, "fields": "tabColorStyle"}},
        {"updateCells": {"range": {"sheetId": target.worksheet_id, "startRowIndex": 0,
            "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": note}]}], "fields": "note"}},
    ]


@dataclass(frozen=True)
class CellSheetTarget:
    spreadsheet_id: str
    worksheet_id: int
    writer_email: str
    finance_emails: tuple[str, ...]

    def validate(self, approved_targets):
        if self not in approved_targets or not self.spreadsheet_id:
            raise ValueError("explicit allowlisted cell worksheet target required")
        if type(self.worksheet_id) is not int or self.worksheet_id < 0:
            raise ValueError("worksheet ID must be explicit")
        if not self.writer_email or not self.finance_emails:
            raise ValueError("explicit writer and Finance editor identities required")


@dataclass(frozen=True)
class CellWorksheetTarget:
    """One pinned worksheet role within the separately approved v2 workbook."""
    role: str
    spreadsheet_id: str
    worksheet_id: int
    title: str
    writer_email: str
    finance_emails: tuple[str, ...]
    naming_scheme: str = "code"

    def sheet_target(self):
        if self.role not in {"summary", "invoice", "unusual", "reversal"}:
            raise ValueError("unknown cell worksheet role")
        if not self.spreadsheet_id or type(self.worksheet_id) is not int or self.worksheet_id < 0:
            raise ValueError("worksheet workbook/ID must be explicit")
        if not self.title or not self.writer_email or not self.finance_emails:
            raise ValueError("worksheet title and approved writers must be explicit")
        return CellSheetTarget(self.spreadsheet_id, self.worksheet_id,
                               self.writer_email, self.finance_emails)


class ExistingSheetAdapter:
    """Wrap an already-open approved worksheet; never discovers/creates/shares."""

    def __init__(self, spreadsheet, worksheet):
        self.spreadsheet = spreadsheet
        self.worksheet = worksheet
        self._owners = None

    @property
    def spreadsheet_id(self):
        return self.spreadsheet.id

    @property
    def worksheet_id(self):
        return self.worksheet.id

    def metadata(self):
        if self._owners is None:
            response = _read_with_retry(lambda: self.spreadsheet.client.request(
                "get", f"https://www.googleapis.com/drive/v3/files/{self.spreadsheet_id}",
                params={"fields": "id,owners(emailAddress)", "supportsAllDrives": "true"},
            ).json())
            if response.get("id") != self.spreadsheet_id:
                raise ValueError("spreadsheet ownership metadata identity mismatch")
            self._owners = [item["emailAddress"] for item in response.get("owners", [])]
        sheets = _read_with_retry(self.spreadsheet.fetch_sheet_metadata).get("sheets", [])
        result = dict(next(sheet for sheet in sheets if sheet["properties"]["sheetId"] == self.worksheet_id))
        result["implicitOwnerEmails"] = list(self._owners)
        return result

    def batch_update(self, requests):
        return self.spreadsheet.batch_update({"requests": requests})

    def read(self, rows, columns):
        return _read_with_retry(lambda: self.worksheet.get(
            f"A1:{column_letter(columns)}{rows}", value_render_option="FORMULA"))

    def read_range(self, start_row, end_row, columns, start_column=0):
        if (type(start_row) is not int or type(end_row) is not int or type(columns) is not int
                or start_row < 0 or not start_row < end_row <= 100_000
                or type(start_column) is not int or not 0 <= start_column < columns <= 100):
            raise ValueError("bounded worksheet value range is invalid")
        return _read_with_retry(lambda: self.worksheet.get(
            f"{column_letter(start_column + 1)}{start_row + 1}:{column_letter(columns)}{end_row}",
            value_render_option="FORMULA"))

    def read_style(self, rows, columns):
        """Read bounded formatting metadata, never values, formulas or other tabs."""
        if type(rows) is not int or type(columns) is not int or not 0 < rows <= 100_000 or not 0 < columns <= 100:
            raise ValueError("style read exceeds governed dimensions")
        title = self.worksheet.title.replace("'", "''")
        structure_fields = "spreadsheetId,sheets(properties(sheetId),merges,bandedRanges(range))"
        structure = _read_with_retry(lambda: self.spreadsheet.fetch_sheet_metadata(
            params={"fields": structure_fields}))
        if structure.get("spreadsheetId") != self.spreadsheet_id:
            raise ValueError("style spreadsheet identity mismatch")
        sheets = structure.get("sheets", [])
        matches = [sheet for sheet in sheets if sheet.get("properties", {}).get("sheetId") == self.worksheet_id]
        if len(matches) != 1 or any(sheet.get("data") for sheet in sheets):
            raise ValueError("style worksheet identity mismatch")
        merges = matches[0].get("merges", [])
        banding = matches[0].get("bandedRanges", [])
        fields = ("spreadsheetId,properties(defaultFormat),sheets(properties(sheetId,gridProperties),"
            "data(startRow,startColumn,columnMetadata(pixelSize),rowMetadata(pixelSize),"
            "rowData(values(effectiveFormat,userEnteredFormat))))")
        for start in range(0, rows, 100):
            stop = min(start + 100, rows)
            result = {"start": start, "formats": [[{} for _ in range(columns)] for _ in range(stop - start)],
                      "entered": [[{} for _ in range(columns)] for _ in range(stop - start)],
                      "widths": [None] * columns, "heights": [None] * (stop - start)}
            response = _read_with_retry(lambda: self.spreadsheet.fetch_sheet_metadata(params={"fields": fields,
                "ranges": f"'{title}'!A{start + 1}:{column_letter(columns)}{stop}"}))
            if response.get("spreadsheetId") != self.spreadsheet_id:
                raise ValueError("style spreadsheet identity mismatch")
            sheets = response.get("sheets", [])
            matches = [sheet for sheet in sheets if sheet["properties"]["sheetId"] == self.worksheet_id]
            if len(matches) != 1 or any(sheet.get("data") for sheet in sheets if sheet not in matches):
                raise ValueError("style worksheet identity mismatch")
            sheet = matches[0]
            result["grid"] = sheet["properties"].get("gridProperties", {})
            # Ranges clip grid data and also clip sheet-level merge/banding metadata.
            result["merges"] = merges
            result["banding"] = banding
            result["default"] = response.get("properties", {}).get("defaultFormat", {})
            for block in sheet.get("data", []):
                first, column = block.get("startRow", 0), block.get("startColumn", 0)
                if first != start or column != 0:
                    raise ValueError("unexpected style grid origin")
                for c, meta in enumerate(block.get("columnMetadata", [])):
                    if c >= columns:
                        raise ValueError("style column metadata exceeds requested range")
                    result["widths"][c] = meta.get("pixelSize")
                for offset, meta in enumerate(block.get("rowMetadata", [])):
                    if first + offset >= stop:
                        raise ValueError("style row metadata exceeds requested range")
                    result["heights"][offset] = meta.get("pixelSize")
                for offset, row in enumerate(block.get("rowData", [])):
                    if first + offset >= stop or len(row.get("values", [])) > columns:
                        raise ValueError("style cells exceed requested range")
                    for c, cell in enumerate(row.get("values", [])):
                        result["formats"][offset][c] = cell.get("effectiveFormat", {})
                        result["entered"][offset][c] = cell.get("userEnteredFormat", {})
            yield result


def _typed(value, *, allow_formulas=True):
    if value == "":
        return {}
    if type(value) is int and abs(value) <= 2 ** 53 - 1:
        return {"numberValue": value}
    if isinstance(value, str):
        return {"formulaValue" if allow_formulas and value.startswith("=") else "stringValue": value}
    raise ValueError("unsupported or inexact spreadsheet value")


def matrix_write_requests(values, sheet_id, *, start_row=0, rows_per_request=250, allow_formulas=True):
    if type(rows_per_request) is not int or rows_per_request < 1:
        raise ValueError("positive rows_per_request required")
    requests = []
    offset = 0
    while offset < len(values):
        count = min(rows_per_request, len(values) - offset)
        while True:
            chunk = values[offset:offset + count]
            request = {"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": start_row + offset,
                          "endRowIndex": start_row + offset + count,
                          "startColumnIndex": 0, "endColumnIndex": len(values[0])},
                "rows": [{"values": [{"userEnteredValue": _typed(value, allow_formulas=allow_formulas)} for value in row]} for row in chunk],
                "fields": "userEnteredValue",
            }}
            if len(json.dumps({"requests": [request]}).encode("utf-8")) <= MAX_REQUEST_BYTES:
                break
            if count == 1:
                raise ValueError("a spreadsheet row exceeds the bounded request size")
            count //= 2
        requests.append(request)
        offset += count
    return requests


def _legacy_sheet_report_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        if 0 < value < 100000:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return business_date(text).isoformat()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _legacy_column_a_spans(rows, first_data_row):
    spans = {}
    for index in range(first_data_row, len(rows)):
        row = rows[index]
        day = _sheet_report_date(row[0] if row else None)
        if day is None:
            continue
        if day in spans:
            start, count = spans[day]
            if index != start + count:
                raise ValueError("REPORT DATE rows are split into multiple blocks")
            spans[day] = (start, count + 1)
        else:
            spans[day] = (index, 1)
    return spans


def _legacy_matrix_date_block(role, matrix, report_date):
    values = matrix.get("values", matrix.get("rows"))
    if not isinstance(values, list) or not values:
        raise ValueError(f"{role} output matrix is empty")
    ranges = [item for item in matrix.get("date_ranges", [])
              if _sheet_report_date(item.get("report_date")) == report_date]
    if len(ranges) > 1:
        raise ValueError(f"{role} matrix has duplicate date ranges")
    if ranges:
        area = ranges[0]
        start = area["start_row"] - 1
        rows = values[start:area["end_row"]]
    else:
        indexes = [index for index, row in enumerate(values)
                   if row and _sheet_report_date(row[0]) == report_date]
        if not indexes:
            return [], None
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise ValueError(f"{role} date rows are not one contiguous block")
        start = indexes[0]
        rows = values[start:indexes[-1] + 1]
    if any(not row or _sheet_report_date(row[0]) != report_date for row in rows):
        raise ValueError(f"{role} date block lacks REPORT DATE values in column A")
    width = len(values[0])
    if any(len(row) != width for row in rows):
        raise ValueError(f"{role} date block is not rectangular")
    return [list(row) for row in rows], start


def _legacy_shift_date_block_formulas(rows, source_start, target_start):
    delta = target_start - source_start
    if not delta:
        return rows
    reference = re.compile(r"(?<![A-Za-z0-9_])(\$?[A-Z]{1,3})(\$?)(\d+)")

    def shift(value):
        if not isinstance(value, str) or not value.startswith("="):
            return value

        def replace(match):
            row = int(match.group(3))
            if source_start < row <= source_start + len(rows):
                return match.group(1) + match.group(2) + str(row + delta)
            return match.group(0)

        return reference.sub(replace, value)

    return [[shift(value) for value in row] for row in rows]


def _legacy_shift_format_request_rows(request, delta):
    operation = next(iter(request))
    if operation in ("repeatCell", "updateBorders"):
        area = request[operation]["range"]
        area["startRowIndex"] += delta
        area["endRowIndex"] += delta
    elif operation == "updateDimensionProperties":
        area = request[operation]["range"]
        if area.get("dimension") != "ROWS":
            return None
        area["startIndex"] += delta
        area["endIndex"] += delta
    elif operation == "autoResizeDimensions":
        area = request[operation]["dimensions"]
        if area.get("dimension") != "ROWS":
            return None
        area["startIndex"] += delta
        area["endIndex"] += delta
    else:
        return None
    return request


def _legacy_display_only_sheet_requests(target, metadata, has_publication_marker):
    writer = getattr(target, "writer_email", None)
    if not writer:
        raise ValueError("an explicit Sheet writer is required for display-only tabs")
    sheet_id = target.worksheet_id
    managed = [item for item in metadata.get("protectedRanges", [])
               if item.get("description", "").startswith(MANAGED_PREFIX)]
    computed = [item for item in managed if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    requests = []
    for item in managed:
        description = item.get("description", "")
        if description == MANAGED_PREFIX + "BANK INPUT" or description.startswith(GUARD_PREFIX):
            requests.append({"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}})
    protected = {
        "range": {"sheetId": sheet_id},
        "description": MANAGED_PREFIX + "COMPUTED",
        "warningOnly": False,
        "editors": {"users": [writer]},
        "unprotectedRanges": [],
    }
    if computed:
        current = computed[0]
        if (current.get("range") != protected["range"]
                or current.get("unprotectedRanges")
                or current.get("warningOnly", False)
                or current.get("editors", {}).get("users") != [writer]):
            requests.append({"updateProtectedRange": {
                "protectedRange": {"protectedRangeId": current["protectedRangeId"], **protected},
                "fields": "range,description,warningOnly,editors,unprotectedRanges",
            }})
        requests.extend({"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}}
                        for item in computed[1:])
    else:
        requests.append({"addProtectedRange": {"protectedRange": protected}})
    if has_publication_marker:
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id}, "fields": "tabColorStyle",
        }})
        requests.append({"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": ""}]}], "fields": "note",
        }})
    return requests


def _publish_cell_date_blocks_legacy(spreadsheet, targets, matrices, report_date, *, rows_per_request=200,
                                     metadata=None):
    """Rewrite one DB-derived report date in existing worksheets without a Sheet snapshot."""
    report_date = business_date(report_date).isoformat()
    if not targets or set(targets) != set(matrices):
        raise ValueError("date publication requires matching worksheet targets and matrices")
    spreadsheet_id = getattr(spreadsheet, "id", None)
    if not spreadsheet_id:
        raise ValueError("an explicit existing spreadsheet is required")
    if metadata is None:
        metadata = _read_with_retry(lambda: spreadsheet.fetch_sheet_metadata(params={
            "fields": ("spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount),tabColorStyle),"
                       "merges,basicFilter,"
                       "protectedRanges(protectedRangeId,description,range,unprotectedRanges,warningOnly,editors)),"
                       "developerMetadata(metadataId,metadataKey,location(sheetId))"),
        }))
    if metadata.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("opened spreadsheet ID differs from configured target")
    sheet_properties = {item.get("properties", {}).get("sheetId"): item
                        for item in metadata.get("sheets", [])}
    roles = list(targets)
    titles = {}
    for role in roles:
        target = targets[role]
        if target.spreadsheet_id != spreadsheet_id or target.worksheet_id not in sheet_properties:
            raise ValueError(f"configured {role} worksheet is missing from the existing spreadsheet")
        properties = sheet_properties[target.worksheet_id].get("properties", {})
        if getattr(target, "title", None) and properties.get("title") != target.title:
            raise ValueError(f"configured {role} worksheet ID/title mismatch")
        titles[role] = properties["title"]

    column_ranges = ["'" + titles[role].replace("'", "''") + "'!A:A" for role in roles]
    column_result = _read_with_retry(lambda: spreadsheet.values_batch_get(
        column_ranges, params={"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"},
    )).get("valueRanges", [])
    if len(column_result) != len(roles):
        raise RuntimeError("Google Sheets returned an incomplete REPORT DATE column batch")

    plans = {}
    for role, column_range in zip(roles, column_result):
        matrix = matrices[role]
        rows, source_start = _matrix_date_block(role, matrix, report_date)
        date_ranges = matrix.get("date_ranges", [])
        first_data_row = (min(item["start_row"] for item in date_ranges) - 1
                          if date_ranges else {"summary": 3, "invoice": 2}.get(role, 1))
        spans = _column_a_spans(column_range.get("values", []), first_data_row)
        old = spans.get(report_date)
        new_count = len(rows)
        grid = sheet_properties[targets[role].worksheet_id].get("properties", {}).get("gridProperties", {})
        grid_rows = grid.get("rowCount")
        if type(grid_rows) is not int:
            raise ValueError(f"{role} worksheet grid row count is unavailable")
        grid_columns = grid.get("columnCount")
        if type(grid_columns) is not int:
            raise ValueError(f"{role} worksheet grid column count is unavailable")

        dimensions = []
        if old:
            data_start, old_count = old
            if new_count > old_count:
                dimensions.append({"insertDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": data_start + old_count, "endIndex": data_start + new_count},
                    "inheritFromBefore": data_start + old_count > 0,
                }})
            elif new_count < old_count:
                dimensions.append({"deleteDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": data_start + new_count, "endIndex": data_start + old_count},
                }})
        elif new_count:
            later = [(day, span) for day, span in spans.items() if day > report_date]
            earlier = [(day, span) for day, span in spans.items() if day < report_date]
            if later:
                insert_at = min(span[0] for _, span in later)
                separator_rows = 2 if role == "invoice" else 0
                data_start = insert_at
                inserted_count = new_count + separator_rows
            elif earlier:
                last_end = max(start + count for start, count in spans.values())
                separator_rows = 2 if role == "invoice" else 0
                insert_at = last_end
                data_start = insert_at + separator_rows
                inserted_count = new_count + separator_rows
            else:
                data_start = first_data_row
                insert_at = data_start
                inserted_count = new_count
            if inserted_count:
                dimensions.append({"insertDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": insert_at, "endIndex": insert_at + inserted_count},
                    "inheritFromBefore": insert_at > 0,
                }})
        else:
            data_start = old[0] if old else first_data_row

        required_grid_rows = data_start + new_count
        required_insert_end = max((
            item["insertDimension"]["range"]["endIndex"]
            for item in dimensions if "insertDimension" in item
        ), default=0)
        required_grid_rows = max(required_grid_rows, required_insert_end)
        if required_grid_rows > grid_rows:
            dimensions.insert(0, {"appendDimension": {
                "sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                "length": required_grid_rows - grid_rows,
            }})

        if rows:
            rows = _shift_date_block_formulas(rows, source_start, data_start)
        value_requests = matrix_write_requests(
            rows, targets[role].worksheet_id, start_row=data_start,
            rows_per_request=rows_per_request, allow_formulas=role == "summary",
        ) if rows else []
        style_requests = []
        if rows and matrix.get("style_contract") and isinstance(targets[role], CellWorksheetTarget):
            from .cell_publication_v2 import _date_format_requests
            style_requests = _date_format_requests(role, targets[role], matrix, {
                "start_row": source_start, "row_count": new_count, "spacer_ranges": [],
            })
            delta = data_start - source_start
            style_requests = [shifted for request in style_requests
                              if (shifted := _shift_format_request_rows(request, delta)) is not None]
        plans[role] = {
            "dimensions": dimensions, "values": value_requests, "styles": style_requests,
            "markers": ([{"updateCells": {
                "range": {"sheetId": targets[role].worksheet_id,
                          "startRowIndex": data_start, "endRowIndex": data_start + new_count,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rows": [{"values": [{"userEnteredValue": _typed(row[0], allow_formulas=False)}]}
                         for row in rows],
                "fields": "userEnteredValue",
            }}] if rows else []),
            "rows": rows, "start": data_start, "count": new_count,
            "title": titles[role], "grid_rows": grid_rows,
        }

    managed_requests = []
    for role in roles:
        target = targets[role]
        sheet = sheet_properties[target.worksheet_id]
        markers = [item for item in metadata.get("developerMetadata", [])
                   if item.get("metadataKey") == PUBLICATION_METADATA_KEY
                   and item.get("location", {}).get("sheetId") == target.worksheet_id]
        managed_requests.extend({"deleteDeveloperMetadata": {"dataFilter": {
            "developerMetadataLookup": {"metadataId": item["metadataId"]},
        }}} for item in markers)
        managed_requests.extend(_display_only_sheet_requests(target, sheet, bool(markers)))

    role_requests = {}
    all_requests = list(managed_requests)
    for role in roles:
        plan = plans[role]
        role_requests[role] = [
            *plan["dimensions"], *plan["layout_before"], *plan["preamble_values"],
            *plan["layout_after"], *plan["preamble_styles"], *plan["markers"],
            *plan["values"], *plan["styles"],
        ]
        all_requests.extend(role_requests[role])
    if any(len(json.dumps({"requests": [request]}).encode("utf-8")) > MAX_REQUEST_BYTES
           for request in all_requests):
        raise ValueError("one stateless date subrequest exceeds the configured request-size limit")

    sent_batches = 0

    def send(requests):
        nonlocal sent_batches
        if not requests:
            return
        if len(json.dumps({"requests": requests}).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("one date write batch exceeds the configured request-size limit")
        spreadsheet.batch_update({"requests": requests})
        sent_batches += 1

    if len(json.dumps({"requests": all_requests}).encode("utf-8")) <= MAX_REQUEST_BYTES:
        send(all_requests)
    else:
        headers = list(managed_requests)
        for role in roles:
            headers.extend(plans[role]["dimensions"])
            headers.extend(plans[role]["layout_before"])
            headers.extend(plans[role]["preamble_values"])
            headers.extend(plans[role]["layout_after"])
            headers.extend(plans[role]["preamble_styles"])
            headers.extend(plans[role]["markers"])
        if len(json.dumps({"requests": headers}).encode("utf-8")) <= MAX_REQUEST_BYTES:
            send(headers)
        else:
            if managed_requests:
                send(managed_requests)
            for role in roles:
                header = [
                    *plans[role]["dimensions"], *plans[role]["layout_before"],
                    *plans[role]["preamble_values"], *plans[role]["layout_after"],
                    *plans[role]["preamble_styles"], *plans[role]["markers"],
                ]
                if header:
                    send(header)
        for role in roles:
            pending = []
            for request in [*plans[role]["values"], *plans[role]["styles"]]:
                candidate = [*pending, request]
                if len(json.dumps({"requests": candidate}).encode("utf-8")) > MAX_REQUEST_BYTES:
                    send(pending)
                    pending = [request]
                else:
                    pending = candidate
            send(pending)

    read_ranges, readback_specs = [], []
    for role in roles:
        plan = plans[role]
        title = plan["title"].replace("'", "''")
        width = len(plan["matrix"].get("values", plan["matrix"].get("rows"))[0])
        if plan["preamble_count"]:
            read_ranges.append(f"'{title}'!A1:{column_letter(width)}{plan['preamble_count']}")
            readback_specs.append((role, "preamble"))
        if plan["count"]:
            first, last = plan["start"] + 1, plan["start"] + plan["count"]
            read_ranges.append(f"'{title}'!A{first}:{column_letter(width)}{last}")
        else:
            read_ranges.append(f"'{title}'!A{plan['preamble_count'] + 1}:A")
        readback_specs.append((role, "date"))
    readback = _read_with_retry(lambda: spreadsheet.values_batch_get(
        read_ranges, params={"valueRenderOption": "FORMULA", "majorDimension": "ROWS"},
    )).get("valueRanges", [])
    if len(readback) != len(readback_specs):
        raise RuntimeError("Google Sheets returned incomplete date-block readback")
    for (role, kind), value_range in zip(readback_specs, readback):
        plan = plans[role]
        if kind == "preamble":
            expected = plan["preamble_rows"]
            actual = _padded(value_range.get("values", []), len(expected),
                             len(plan["matrix"].get("values", plan["matrix"].get("rows"))[0]))
            if actual != expected:
                raise ValueError(f"{role} opening/header readback differs from DB-derived output")
        elif plan["count"]:
            actual = _padded(value_range.get("values", []), plan["count"], len(plan["rows"][0]))
            if actual != plan["rows"]:
                raise ValueError(f"{role} REPORT DATE block readback differs from DB-derived output")
        elif any(_sheet_report_date(row[0] if row else None) == report_date
                 for row in value_range.get("values", [])):
            raise ValueError(f"{role} REPORT DATE block remained after delete")
    return {"report_date": report_date, "roles": roles, "http_write_batches": sent_batches}
def _sheet_report_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        if 0 < value < 100000:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return business_date(value.strip()).isoformat()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _column_a_spans(rows, first_data_row):
    spans = {}
    for index in range(first_data_row, len(rows)):
        row = rows[index]
        day = _sheet_report_date(row[0] if row else None)
        if day is None:
            continue
        if day in spans:
            start, count = spans[day]
            if index != start + count:
                raise ValueError("REPORT DATE rows are split into multiple blocks")
            spans[day] = (start, count + 1)
        else:
            spans[day] = (index, 1)
    return spans


def _matrix_date_block(role, matrix, report_date):
    values = matrix.get("values", matrix.get("rows"))
    if not isinstance(values, list) or not values:
        raise ValueError(f"{role} output matrix is empty")
    ranges = [item for item in matrix.get("date_ranges", [])
              if _sheet_report_date(item.get("report_date")) == report_date]
    if len(ranges) > 1:
        raise ValueError(f"{role} matrix has duplicate date ranges")
    if ranges:
        area = ranges[0]
        start = area["start_row"] - 1
        rows = values[start:area["end_row"]]
    else:
        indexes = [index for index, row in enumerate(values)
                   if row and _sheet_report_date(row[0]) == report_date]
        if not indexes:
            return [], None
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise ValueError(f"{role} date rows are not one contiguous block")
        start = indexes[0]
        rows = values[start:indexes[-1] + 1]
    if any(not row or _sheet_report_date(row[0]) != report_date for row in rows):
        raise ValueError(f"{role} date block lacks REPORT DATE values in column A")
    width = len(values[0])
    if any(len(row) != width for row in rows):
        raise ValueError(f"{role} date block is not rectangular")
    return [list(row) for row in rows], start


def _invoice_spacer_ranges(matrix, report_date):
    date_ranges = matrix.get("date_ranges", [])
    selected = [index for index, item in enumerate(date_ranges)
                if _sheet_report_date(item.get("report_date")) == report_date]
    if len(selected) > 1:
        raise ValueError("invoice matrix has duplicate date ranges")
    if not selected:
        return []

    index = selected[0]
    adjacent = []
    if index:
        adjacent.append((date_ranges[index - 1], date_ranges[index]))
    if index + 1 < len(date_ranges):
        adjacent.append((date_ranges[index], date_ranges[index + 1]))

    spacers = []
    for left, right in adjacent:
        # Date ranges use 1-based starts and exclusive end rows in matrix slices.
        start = left["end_row"]
        end = right["start_row"] - 1
        gap_rows = end - start
        if gap_rows == 0:
            continue
        if gap_rows != 2:
            raise ValueError("invoice day separators must contain exactly two rows")
        spacers.append((start, end))
    return spacers


def _bounded_date_write_batches(value_requests, style_requests, spacer_ranges, max_bytes):
    def size(requests):
        return len(json.dumps({"requests": requests}).encode("utf-8"))

    spacer_styles, remaining_styles = [], []
    for request in style_requests:
        operation = next(iter(request))
        area = request[operation].get("range", {}) if operation in ("repeatCell", "updateBorders") else {}
        if any(area.get("startRowIndex", 0) < end and area.get("endRowIndex", 0) > start
               for start, end in spacer_ranges):
            spacer_styles.append(request)
        else:
            remaining_styles.append(request)

    paired_index = None
    if spacer_styles:
        if not value_requests:
            raise ValueError("invoice spacer formatting requires a date value write")
        paired_index = min(
            range(len(value_requests)),
            key=lambda index: size([value_requests[index], *spacer_styles]),
        )
        if size([value_requests[paired_index], *spacer_styles]) > max_bytes:
            raise ValueError("invoice spacer formatting cannot share a bounded date write")

    units = []
    for index, request in enumerate(value_requests):
        unit = [request]
        if index == paired_index:
            unit.extend(spacer_styles)
        units.append(unit)
    units.extend([[request] for request in remaining_styles])

    batches, pending = [], []
    for unit in units:
        if size(unit) > max_bytes:
            raise ValueError("one date write batch exceeds the configured request-size limit")
        if pending and size([*pending, *unit]) > max_bytes:
            batches.append(pending)
            pending = []
        pending.extend(unit)
    if pending:
        batches.append(pending)
    return batches


def _shift_date_block_formulas(rows, source_start, target_start):
    delta = target_start - source_start
    if not delta:
        return rows
    reference = re.compile(r"(?<![A-Za-z0-9_])(\$?[A-Z]{1,3})(\$?)(\d+)")

    def shift(value):
        if not isinstance(value, str) or not value.startswith("="):
            return value

        def replace(match):
            row = int(match.group(3))
            if source_start < row <= source_start + len(rows):
                return match.group(1) + match.group(2) + str(row + delta)
            return match.group(0)

        return reference.sub(replace, value)

    return [[shift(value) for value in row] for row in rows]


def _shift_format_request_rows(request, delta):
    operation = next(iter(request))
    if operation in ("repeatCell", "updateBorders"):
        area = request[operation]["range"]
        area["startRowIndex"] += delta
        area["endRowIndex"] += delta
    elif operation == "updateDimensionProperties":
        area = request[operation]["range"]
        if area.get("dimension") != "ROWS":
            return None
        area["startIndex"] += delta
        area["endIndex"] += delta
    elif operation == "autoResizeDimensions":
        area = request[operation]["dimensions"]
        if area.get("dimension") != "ROWS":
            return None
        area["startIndex"] += delta
        area["endIndex"] += delta
    else:
        return None
    return request


def _display_only_sheet_requests(target, metadata, has_publication_marker):
    writer = getattr(target, "writer_email", None)
    if not writer:
        raise ValueError("an explicit Sheet writer is required for display-only tabs")
    sheet_id = target.worksheet_id
    managed = [item for item in metadata.get("protectedRanges", [])
               if item.get("description", "").startswith(MANAGED_PREFIX)]
    computed = [item for item in managed if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    requests = []
    for item in managed:
        description = item.get("description", "")
        if description == MANAGED_PREFIX + "BANK INPUT" or description.startswith(GUARD_PREFIX):
            requests.append({"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}})
    protected = {
        "range": {"sheetId": sheet_id},
        "description": MANAGED_PREFIX + "COMPUTED",
        "warningOnly": False,
        "editors": {"users": [writer]},
        "unprotectedRanges": [],
    }
    if computed:
        current = computed[0]
        if (current.get("range") != protected["range"]
                or current.get("unprotectedRanges")
                or current.get("warningOnly", False)
                or current.get("editors", {}).get("users") != [writer]):
            requests.append({"updateProtectedRange": {
                "protectedRange": {"protectedRangeId": current["protectedRangeId"], **protected},
                "fields": "range,description,warningOnly,editors,unprotectedRanges",
            }})
        requests.extend({"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}}
                        for item in computed[1:])
    else:
        requests.append({"addProtectedRange": {"protectedRange": protected}})
    if has_publication_marker:
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id}, "fields": "tabColorStyle",
        }})
        requests.append({"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": ""}]}], "fields": "note",
        }})
    return requests


def _cell_layout_requests(target, matrix, sheet_metadata, preamble_count):
    contract = matrix.get("style_contract")
    if not contract:
        return [], []
    values = matrix.get("values", matrix.get("rows"))
    widths = contract.get("widths")
    if (not values or not isinstance(widths, list) or len(widths) != len(values[0])
            or any(type(width) is not int or width <= 0 for width in widths)):
        raise ValueError("cell style contract has invalid column widths")
    sheet_id = target.worksheet_id
    after_values = [{"updateSheetProperties": {"properties": {"sheetId": sheet_id,
        "gridProperties": {"frozenRowCount": contract["frozen_rows"],
                           "frozenColumnCount": contract["frozen_columns"],
                           "hideGridlines": True}},
        "fields": "gridProperties(frozenRowCount,frozenColumnCount,hideGridlines)"}}]
    for column, width in enumerate(widths):
        after_values.append({"updateDimensionProperties": {"range": {
            "sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": column, "endIndex": column + 1,
        }, "properties": {"pixelSize": width}, "fields": "pixelSize"}})

    expected_merges = {tuple(area) for area in contract.get("merges", [])}
    existing_merges = sheet_metadata.get("merges", [])
    before_values = []
    for merge in existing_merges:
        box = (merge.get("startRowIndex", 0), merge.get("endRowIndex", 0),
               merge.get("startColumnIndex", 0), merge.get("endColumnIndex", 0))
        if box in expected_merges:
            before_values.append({"unmergeCells": {"range": {
                "sheetId": sheet_id, "startRowIndex": box[0], "endRowIndex": box[1],
                "startColumnIndex": box[2], "endColumnIndex": box[3],
            }}})
            continue
        if box[0] < preamble_count and box[1] > 0:
            if box[0] < 0 or box[1] > preamble_count:
                raise ValueError("an existing merge crosses the managed cell preamble")
            before_values.append({"unmergeCells": {"range": {
                "sheetId": sheet_id, "startRowIndex": box[0], "endRowIndex": box[1],
                "startColumnIndex": box[2], "endColumnIndex": box[3],
            }}})
    for box in sorted(expected_merges):
        if box[0] < 0 or box[1] > preamble_count or box[0] >= box[1]:
            raise ValueError("cell style merge falls outside the managed preamble")
        after_values.append({"mergeCells": {"range": {
            "sheetId": sheet_id, "startRowIndex": box[0], "endRowIndex": box[1],
            "startColumnIndex": box[2], "endColumnIndex": box[3],
        }, "mergeType": "MERGE_ALL"}})
    return before_values, after_values


def publish_cell_date_blocks(spreadsheet, targets, matrices, report_date, *, rows_per_request=200,
                             metadata=None):
    """Rewrite one DB-derived date block and reassert its shared preamble/layout."""
    report_date = business_date(report_date).isoformat()
    if not targets or set(targets) != set(matrices):
        raise ValueError("date publication requires matching worksheet targets and matrices")
    spreadsheet_id = getattr(spreadsheet, "id", None)
    if not spreadsheet_id:
        raise ValueError("an explicit existing spreadsheet is required")
    if metadata is None:
        metadata = _read_with_retry(lambda: spreadsheet.fetch_sheet_metadata(params={
            "fields": ("spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount),tabColorStyle),"
                       "merges,basicFilter,"
                       "protectedRanges(protectedRangeId,description,range,unprotectedRanges,warningOnly,editors)),"
                       "developerMetadata(metadataId,metadataKey,location(sheetId))"),
        }))
    if metadata.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("opened spreadsheet ID differs from configured target")
    sheet_properties = {item.get("properties", {}).get("sheetId"): item
                        for item in metadata.get("sheets", [])}
    roles = list(targets)
    titles = {}
    for role in roles:
        target = targets[role]
        if target.spreadsheet_id != spreadsheet_id or target.worksheet_id not in sheet_properties:
            raise ValueError(f"configured {role} worksheet is missing from the existing spreadsheet")
        properties = sheet_properties[target.worksheet_id].get("properties", {})
        if getattr(target, "title", None) and properties.get("title") != target.title:
            raise ValueError(f"configured {role} worksheet ID/title mismatch")
        titles[role] = properties["title"]

    column_ranges = ["'" + titles[role].replace("'", "''") + "'!A:A" for role in roles]
    column_result = _read_with_retry(lambda: spreadsheet.values_batch_get(
        column_ranges, params={"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"},
    )).get("valueRanges", [])
    if len(column_result) != len(roles):
        raise RuntimeError("Google Sheets returned an incomplete REPORT DATE column batch")

    plans = {}
    for role, column_range in zip(roles, column_result):
        matrix = matrices[role]
        rows, source_start = _matrix_date_block(role, matrix, report_date)
        date_ranges = matrix.get("date_ranges", [])
        first_data_row = (min(item["start_row"] for item in date_ranges) - 1
                          if date_ranges else {"summary": 3, "invoice": 2}.get(role, 1))
        spans = _column_a_spans(column_range.get("values", []), first_data_row)
        old = spans.get(report_date)
        new_count = len(rows)
        grid = sheet_properties[targets[role].worksheet_id].get("properties", {}).get("gridProperties", {})
        grid_rows = grid.get("rowCount")
        if type(grid_rows) is not int:
            raise ValueError(f"{role} worksheet grid row count is unavailable")
        grid_columns = grid.get("columnCount")
        if type(grid_columns) is not int:
            raise ValueError(f"{role} worksheet grid column count is unavailable")

        dimensions = []
        if old:
            data_start, old_count = old
            if new_count > old_count:
                dimensions.append({"insertDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": data_start + old_count, "endIndex": data_start + new_count},
                    "inheritFromBefore": data_start + old_count > 0,
                }})
            elif new_count < old_count:
                dimensions.append({"deleteDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": data_start + new_count, "endIndex": data_start + old_count},
                }})
        elif new_count:
            later = [(day, span) for day, span in spans.items() if day > report_date]
            earlier = [(day, span) for day, span in spans.items() if day < report_date]
            if later:
                data_start = min(span[0] for _, span in later)
                inserted_count = new_count + (2 if role == "invoice" else 0)
                insert_at = data_start
            elif earlier:
                last_end = max(start + count for start, count in spans.values())
                separator_rows = 2 if role == "invoice" else 0
                insert_at = last_end
                data_start = insert_at + separator_rows
                inserted_count = new_count + separator_rows
            else:
                data_start = first_data_row
                insert_at = data_start
                inserted_count = new_count
            if inserted_count:
                dimensions.append({"insertDimension": {
                    "range": {"sheetId": targets[role].worksheet_id, "dimension": "ROWS",
                              "startIndex": insert_at, "endIndex": insert_at + inserted_count},
                    "inheritFromBefore": insert_at > 0,
                }})
        else:
            data_start = old[0] if old else first_data_row

        inserted_rows = sum(
            item["insertDimension"]["range"]["endIndex"]
            - item["insertDimension"]["range"]["startIndex"]
            for item in dimensions if "insertDimension" in item
        )
        deleted_rows = sum(
            item["deleteDimension"]["range"]["endIndex"]
            - item["deleteDimension"]["range"]["startIndex"]
            for item in dimensions if "deleteDimension" in item
        )
        final_grid_rows = grid_rows + inserted_rows - deleted_rows
        required_grid_rows = data_start + new_count
        if required_grid_rows > final_grid_rows:
            dimensions.insert(0, {"appendDimension": {
                "sheetId": targets[role].worksheet_id,
                "dimension": "ROWS",
                "length": required_grid_rows - final_grid_rows,
            }})
        required_grid_columns = len(matrix.get("values", matrix.get("rows"))[0])
        if required_grid_columns > grid_columns:
            dimensions.insert(0, {"appendDimension": {
                "sheetId": targets[role].worksheet_id,
                "dimension": "COLUMNS",
                "length": required_grid_columns - grid_columns,
            }})

        if rows:
            rows = _shift_date_block_formulas(rows, source_start, data_start)
        values = matrix.get("values", matrix.get("rows"))
        preamble_rows = [list(row) for row in values[:first_data_row]]
        spacer_ranges = _invoice_spacer_ranges(matrix, report_date) if role == "invoice" else []
        preamble_values = matrix_write_requests(
            preamble_rows, targets[role].worksheet_id, start_row=0,
            rows_per_request=rows_per_request, allow_formulas=role == "summary",
        ) if preamble_rows else []
        layout_before, layout_after = _cell_layout_requests(
            targets[role], matrix, sheet_properties[targets[role].worksheet_id], first_data_row,
        )
        preamble_styles = []
        if preamble_rows and matrix.get("style_contract") and isinstance(targets[role], CellWorksheetTarget):
            from .cell_publication_v2 import _date_format_requests
            preamble_styles = [request for request in _date_format_requests(
                role, targets[role], matrix,
                {"start_row": 0, "row_count": first_data_row, "spacer_ranges": []},
            ) if "setBasicFilter" not in request]
        value_requests = matrix_write_requests(
            rows, targets[role].worksheet_id, start_row=data_start,
            rows_per_request=rows_per_request, allow_formulas=role == "summary",
        ) if rows else []
        style_requests = []
        if rows and matrix.get("style_contract") and isinstance(targets[role], CellWorksheetTarget):
            from .cell_publication_v2 import _date_format_requests
            style_requests = _date_format_requests(role, targets[role], matrix, {
                "start_row": source_start, "row_count": new_count,
                "spacer_ranges": spacer_ranges,
            })
            delta = data_start - source_start
            style_requests = [shifted for request in style_requests
                              if (shifted := _shift_format_request_rows(request, delta)) is not None]

        plans[role] = {
            "dimensions": dimensions, "layout_before": layout_before,
            "layout_after": layout_after,
            "preamble_values": preamble_values, "preamble_styles": preamble_styles,
            "preamble_rows": preamble_rows, "preamble_count": first_data_row,
            "values": value_requests, "styles": style_requests,
            "target_spacer_ranges": [(start + data_start - source_start,
                                      end + data_start - source_start)
                                     for start, end in spacer_ranges]
                                    if rows and matrix.get("style_contract") else [],
            "markers": ([{"updateCells": {
                "range": {"sheetId": targets[role].worksheet_id,
                          "startRowIndex": data_start, "endRowIndex": data_start + new_count,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rows": [{"values": [{"userEnteredValue": _typed(row[0], allow_formulas=False)}]}
                         for row in rows],
                "fields": "userEnteredValue",
            }}] if rows else []),
            "rows": rows, "start": data_start, "count": new_count,
            "source_start": source_start, "matrix": matrix, "title": titles[role],
            "sheet_id": targets[role].worksheet_id, "grid_rows": grid_rows,
        }

    # Read-only snapshots and manual edit holes from the older publisher are removed once.
    managed_requests = []
    for role in roles:
        target = targets[role]
        sheet = sheet_properties[target.worksheet_id]
        markers = [item for item in metadata.get("developerMetadata", [])
                   if item.get("metadataKey") == PUBLICATION_METADATA_KEY
                   and item.get("location", {}).get("sheetId") == target.worksheet_id]
        managed_requests.extend({"deleteDeveloperMetadata": {"dataFilter": {
            "developerMetadataLookup": {"metadataId": item["metadataId"]},
        }}} for item in markers)
        managed_requests.extend(_display_only_sheet_requests(target, sheet, bool(markers)))

    role_requests = {}
    all_requests = list(managed_requests)
    for role in roles:
        plan = plans[role]
        role_requests[role] = [
            *plan["dimensions"], *plan["layout_before"], *plan["preamble_values"],
            *plan["layout_after"], *plan["preamble_styles"], *plan["markers"],
            *plan["values"], *plan["styles"],
        ]
        all_requests.extend(role_requests[role])

    if any(len(json.dumps({"requests": [request]}).encode("utf-8")) > MAX_REQUEST_BYTES
           for request in all_requests):
        raise ValueError("one stateless date subrequest exceeds the configured request-size limit")

    overflow_role_batches = None
    if len(json.dumps({"requests": all_requests}).encode("utf-8")) > MAX_REQUEST_BYTES:
        overflow_role_batches = {
            role: _bounded_date_write_batches(
                plans[role]["values"], plans[role]["styles"],
                plans[role]["target_spacer_ranges"], MAX_REQUEST_BYTES,
            )
            for role in roles
        }

    sent_batches = 0

    def send(requests):
        nonlocal sent_batches
        if not requests:
            return
        if len(json.dumps({"requests": requests}).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("one date write batch exceeds the configured request-size limit")
        spreadsheet.batch_update({"requests": requests})
        sent_batches += 1

    if len(json.dumps({"requests": all_requests}).encode("utf-8")) <= MAX_REQUEST_BYTES:
        send(all_requests)
    else:
        headers = list(managed_requests)
        for role in roles:
            headers.extend(plans[role]["dimensions"])
            headers.extend(plans[role]["layout_before"])
            headers.extend(plans[role]["preamble_values"])
            headers.extend(plans[role]["layout_after"])
            headers.extend(plans[role]["preamble_styles"])
            headers.extend(plans[role]["markers"])
        if len(json.dumps({"requests": headers}).encode("utf-8")) <= MAX_REQUEST_BYTES:
            send(headers)
        else:
            if managed_requests:
                send(managed_requests)
            for role in roles:
                header = [
                    *plans[role]["dimensions"], *plans[role]["layout_before"],
                    *plans[role]["preamble_values"], *plans[role]["layout_after"],
                    *plans[role]["preamble_styles"], *plans[role]["markers"],
                ]
                if header:
                    send(header)
        for role in roles:
            for batch in overflow_role_batches[role]:
                send(batch)

    read_ranges, readback_specs = [], []
    for role in roles:
        plan = plans[role]
        title = plan["title"].replace("'", "''")
        width = len(plan["matrix"].get("values", plan["matrix"].get("rows"))[0])
        if plan["preamble_count"]:
            count = plan["preamble_count"]
            read_ranges.append(f"'{title}'!A1:{column_letter(width)}{count}")
            readback_specs.append({"role": role, "kind": "preamble", "start_row": 0,
                                   "count": count, "width": width})
        if plan["count"]:
            first, last = plan["start"] + 1, plan["start"] + plan["count"]
            read_ranges.append(f"'{title}'!A{first}:{column_letter(width)}{last}")
            readback_specs.append({"role": role, "kind": "date", "start_row": plan["start"],
                                   "count": plan["count"], "width": width})
        else:
            read_ranges.append(f"'{title}'!A{plan['preamble_count'] + 1}:A")
            readback_specs.append({"role": role, "kind": "absent",
                                   "start_row": plan["preamble_count"], "width": 1})
        for start, end in plan["target_spacer_ranges"]:
            read_ranges.append(f"'{title}'!A{start + 1}:{column_letter(width)}{end}")
            readback_specs.append({"role": role, "kind": "spacer", "start_row": start,
                                   "count": end - start, "width": width})

    readback = _read_with_retry(lambda: spreadsheet.fetch_sheet_metadata(params={
        "includeGridData": True,
        "ranges": read_ranges,
        "fields": "sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveFormat(backgroundColor)))))",
    }))
    grid_blocks = {}
    for sheet in readback.get("sheets", []):
        sheet_id = sheet.get("properties", {}).get("sheetId")
        role = next((item for item in roles if plans[item]["sheet_id"] == sheet_id), None)
        if role is None:
            continue
        for block in sheet.get("data", []):
            grid_blocks[(role, block.get("startRow", 0), block.get("startColumn", 0))] = block

    for spec in readback_specs:
        role, kind = spec["role"], spec["kind"]
        plan = plans[role]
        block = grid_blocks.get((role, spec["start_row"], 0), {"rowData": []})
        grid_rows = block.get("rowData", [])
        actual_rows = [[_grid_cell_value(cell) for cell in row.get("values", [])]
                       for row in grid_rows]
        if kind == "absent":
            if any(_sheet_report_date(row[0] if row else None) == report_date
                   for row in actual_rows):
                raise ValueError(f"{role} REPORT DATE block remained after delete")
        elif kind in ("preamble", "date"):
            expected = plan["preamble_rows"] if kind == "preamble" else plan["rows"]
            actual = _padded(actual_rows, spec["count"], spec["width"])
            if actual != expected:
                description = "opening/header" if kind == "preamble" else "REPORT DATE block"
                raise ValueError(f"{role} {description} readback differs from DB-derived output")
        else:
            for row_offset in range(spec["count"]):
                row = grid_rows[row_offset] if row_offset < len(grid_rows) else {}
                for column in range(spec["width"]):
                    cells = row.get("values", [])
                    cell = cells[column] if column < len(cells) else {}
                    color = cell.get("effectiveFormat", {}).get("backgroundColor")
                    if color and any(abs(color.get(channel, 0.0) - 1.0) > 1e-6
                                     for channel in ("red", "green", "blue")):
                        raise ValueError(
                            f"invoice spacer background is not white at row "
                            f"{spec['start_row'] + row_offset + 1}, column {column + 1}"
                        )
    return {"report_date": report_date, "roles": roles, "http_write_batches": sent_batches}


def _grid_cell_value(cell):
    value = cell.get("userEnteredValue", {})
    for key in ("formulaValue", "stringValue", "numberValue", "boolValue"):
        if key in value:
            return value[key]
    return ""


def _padded(rows, count, width):
    if len(rows) > count or any(len(row) > width for row in rows):
        raise ValueError("unexpected spreadsheet readback dimensions")
    if any(type(value) is bool for row in rows for value in row):
        raise ValueError("unexpected boolean in governed spreadsheet values")
    return [list(rows[index]) + [""] * (width - len(rows[index])) if index < len(rows)
            else [""] * width for index in range(count)]


def _manual_values(old_matrix, actual):
    result = {}
    if old_matrix is None:
        if any(any(value != "" for value in row) for row in actual):
            raise ValueError("non-empty worksheet has no governed cell publication baseline")
        return result
    width = len(old_matrix["values"][0])
    actual = _padded(actual, len(old_matrix["values"]), width)
    for entry in old_matrix["editable_inputs"]:
        value = actual[entry["row"] - 1][entry["column"] - 1]
        if value == entry["default"]:
            continue
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if type(value) is not int or abs(value) > 2 ** 53 - 1:
            raise ValueError("manual bank input is not an exact amount or recognized default")
        result[(entry["report_date"], entry["cell_code"])] = value
    return result


def _validate_input_policy(matrix):
    if matrix.get("editable_inputs"):
        raise ValueError("cell visualization matrices cannot contain editable Sheet inputs")


def _protection_requests(target, matrix, metadata, guard_id):
    _validate_input_policy(matrix)
    managed = [item for item in metadata.get("protectedRanges", [])
               if item.get("description", "").startswith(MANAGED_PREFIX)
               and item["protectedRangeId"] != guard_id]
    requests = [{"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}} for item in managed]
    inputs = [{"sheetId": target.worksheet_id, "startRowIndex": item["row"] - 1,
               "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
               "endColumnIndex": item["column"]} for item in matrix.get("editable_inputs", [])]
    requests.append({"updateProtectedRange": {"protectedRange": {
        "protectedRangeId": guard_id, "description": MANAGED_PREFIX + "COMPUTED",
        "warningOnly": False, "editors": {"users": [target.writer_email]},
        "unprotectedRanges": inputs,
    }, "fields": "description,warningOnly,editors,unprotectedRanges"}})
    for grid in inputs:
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": grid, "description": MANAGED_PREFIX + "BANK INPUT",
            "warningOnly": False,
            "editors": {"users": sorted(set((target.writer_email, *target.finance_emails)))},
        }}})
    return requests


def _format_requests(target, matrix):
    if matrix.get("layout_version") in (3, 4, 5):
        return _compact_format_requests(target, matrix)
    width = len(matrix["values"][0])
    sheet_id = target.worksheet_id
    requests = [{"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 2,
                  "startColumnIndex": 0, "endColumnIndex": width},
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.14, "green": 0.24, "blue": 0.30},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }}, "fields": "userEnteredFormat(backgroundColor,textFormat)",
    }}]
    block_borders = []
    for block in matrix["blocks"]:
        start = block["start_column"] - 1
        requests.append({"mergeCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 3 if start == 0 else start, "endColumnIndex": start + 6},
            "mergeType": "MERGE_ALL",
        }})
        for offset, pixels in enumerate((115, 100, 390, 130, 155, 150)):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": start + offset, "endIndex": start + offset + 1},
                "properties": {"pixelSize": pixels}, "fields": "pixelSize",
            }})
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": len(matrix["values"]),
                      "startColumnIndex": start + 3, "endColumnIndex": start + 6},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }})
        edge = {"style": "SOLID_MEDIUM", "color": {"red": 0.14, "green": 0.24, "blue": 0.30}}
        for first, last in [(0, len(matrix["values"])), (0, 2), (2, 3), *[(item["start_row"] - 1, item["end_row"]) for item in matrix["date_ranges"]]]:
            block_borders.append({"updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": first, "endRowIndex": last,
                          "startColumnIndex": start, "endColumnIndex": start + 6},
                "left": edge, "right": edge, "top": edge, "bottom": edge,
            }})
    for spacer in (6, 13, 20):
        if spacer >= width:
            continue
        grid = {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(matrix["values"]),
                "startColumnIndex": spacer, "endColumnIndex": spacer + 1}
        requests.extend([
            {"repeatCell": {"range": grid, "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 1, "green": 1, "blue": 1},
            }}, "fields": "userEnteredFormat.backgroundColor"}},
            {"updateBorders": {"range": grid, **{side: {"style": "NONE"} for side in
                ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                "startIndex": spacer, "endIndex": spacer + 1}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
        ])
    requests.extend(block_borders)
    for item in matrix["editable_inputs"]:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": item["row"] - 1,
                      "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
                      "endColumnIndex": item["column"]},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 0.95, "blue": 0.75}}},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    return requests


def _rgb(hex_color):
    return dict(zip(("red", "green", "blue"), (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))))


def _compact_format_requests(target, matrix):
    sheet, width, height = target.worksheet_id, len(matrix["values"][0]), len(matrix["values"])
    requests = []

    def grid(sr, er, sc, ec):
        return {"sheetId": sheet, "startRowIndex": sr, "endRowIndex": er,
                "startColumnIndex": sc, "endColumnIndex": ec}

    def format_range(area, value, fields):
        requests.append({"repeatCell": {"range": area, "cell": {"userEnteredFormat": value}, "fields": fields}})

    format_range(grid(0, height, 0, width), {"textFormat": {"fontFamily": "Arial", "fontSize": 10,
        "foregroundColor": _rgb(REFERENCE_COLORS["text"]), "bold": False},
        "horizontalAlignment": "LEFT", **({"verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP"}
            if matrix.get("layout_version") in (4, 5) else {})},
        "userEnteredFormat" if matrix.get("layout_version") in (4, 5) else "userEnteredFormat(textFormat,horizontalAlignment)")
    format_range(grid(0, 2, 0, width), {"backgroundColor": _rgb(REFERENCE_COLORS["header"]),
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        **({"wrapStrategy": "CLIP"} if matrix.get("layout_version") in (4, 5) else {}),
        "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True,
                       "foregroundColor": _rgb("FFFFFF")}}, "userEnteredFormat")
    for index in range(2, height):
        format_range(grid(index, index + 1, 0, width), {"backgroundColor": _rgb(section_fill(matrix, index + 1))},
                     "userEnteredFormat.backgroundColor")
        key = matrix["row_keys"][index].split(":", 1)[0]
        if key in ("DETAIL", "MANDIRI") and key != matrix["row_keys"][index - 1].split(":", 1)[0]:
            format_range(grid(index, index + 1, 1, 2), {"backgroundColor": _rgb(REFERENCE_COLORS["section"]),
                "textFormat": {"bold": True, "foregroundColor": _rgb(REFERENCE_COLORS["text"]),
                    **({"fontFamily": "Arial", "fontSize": 10} if matrix.get("layout_version") in (4, 5) else {})}},
                "userEnteredFormat(backgroundColor,textFormat)")
    format_range(grid(0, height, 2, 3), {"wrapStrategy": "CLIP"}, "userEnteredFormat.wrapStrategy")
    for col, pixels in enumerate((115, 100, DESCRIPTION_WIDTH_PX)):
        requests.extend([
            {"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
                "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": pixels}, "fields": "pixelSize"}},
            {"mergeCells": {"range": grid(0, 2, col, col + 1), "mergeType": "MERGE_ALL"}},
        ])
    border = {"style": "SOLID_MEDIUM", "color": _rgb(REFERENCE_COLORS["header"])}
    areas = [(0, 3)]
    for block in matrix["blocks"]:
        start, end = block["start_column"] - 1, block["end_column"]
        areas.append((start, end))
        requests.append({"mergeCells": {"range": grid(0, 1, start, end), "mergeType": "MERGE_ALL"}})
        format_range(grid(2, height, start, end), {"horizontalAlignment": "RIGHT",
            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}},
            "userEnteredFormat(horizontalAlignment,numberFormat)")
        for col in range(start, end):
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
                "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}})
    for spacer in (6, 10, 14):
        if spacer >= width:
            continue
        area = grid(0, height, spacer, spacer + 1)
        format_range(area, {"backgroundColor": _rgb("FFFFFF")}, "userEnteredFormat.backgroundColor")
        requests.append({"updateBorders": {"range": area, **{side: {"style": "NONE"} for side in
            ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}})
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
            "startIndex": spacer, "endIndex": spacer + 1}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}})
    light = {"style": "SOLID", "color": _rgb("A5B2BF")}
    section_rows, subtotal_rows = section_border_rows(matrix)
    # Apply the reference grid before strong rules so reruns do not leave
    # old Cash or category borders from an earlier presentation.
    grid_requests = []
    for start, end in areas:
        grid_requests.append({"updateBorders": {"range": grid(0, height, start, end),
            **{side: light for side in ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}})
    requests.extend(grid_requests)
    for start, end in areas:
        requests.append({"updateBorders": {"range": grid(0, height, start, end), "left": border, "right": border}})
        requests.append({"updateBorders": {"range": grid(0, 2, start, end), "top": border, "bottom": border}})
        for row in section_rows:
            requests.append({"updateBorders": {"range": grid(row - 1, row, start, end), "top": border}})
    for block in matrix["blocks"]:
        for row in subtotal_rows:
            start = block["start_column"] - 1
            requests.append({"updateBorders": {"range": grid(row - 1, row, start, start + 1), "top": border}})
    return requests


def _verify_final_protections(target, matrix, metadata):
    _validate_input_policy(matrix)
    protections = metadata.get("protectedRanges", [])
    expected = [{"sheetId": target.worksheet_id, "startRowIndex": item["row"] - 1,
                 "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
                 "endColumnIndex": item["column"]} for item in matrix.get("editable_inputs", [])]
    computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    inputs = [item for item in protections if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
    if len(computed) != 1 or len(inputs) != len(expected):
        raise ValueError("governed protection counts do not match committed layout")
    principal = computed[0]
    owners = metadata.get("implicitOwnerEmails", [])
    if principal.get("range") != {"sheetId": target.worksheet_id} or principal.get("warningOnly", False) is not False:
        raise ValueError("computed range protection was not confirmed")
    actual_holes = sorted(json.dumps(item, sort_keys=True) for item in principal.get("unprotectedRanges", []))
    expected_holes = sorted(json.dumps(item, sort_keys=True) for item in expected)
    if not _editors_match(principal.get("editors", {}), [target.writer_email], owners) or actual_holes != expected_holes:
        raise ValueError("computed/input protection policy mismatch")
    by_range = {json.dumps(item["range"], sort_keys=True): item for item in inputs}
    for grid in expected:
        item = by_range.get(json.dumps(grid, sort_keys=True), {})
        if item.get("unprotectedRanges") or item.get("warningOnly", False) is not False or not _editors_match(
            item.get("editors", {}), [target.writer_email, *target.finance_emails], owners,
        ):
            raise ValueError("Finance input editor protections were not confirmed")


def _editors_match(editors, users, implicit_owners=()):
    actual, expected, owners = set(editors.get("users", [])), set(users), set(implicit_owners)
    return (actual <= expected | owners and expected - owners <= actual
            and not editors.get("groups") and not editors.get("domainUsersCanEdit", False))


def publish_cell_cluster(store, target, approved_targets, adapter, batch, config,
                         *, changed_dates, rows_per_request=250, additional_batches=None,
                         configuration_only=False):
    """Reject the retired snapshot/status publisher; use date-block publication."""
    raise RuntimeError(
        "stateful cell publication is retired; use finpay_pipeline.cell_format.publish_cell_sheets"
    )
    target.validate(approved_targets)
    validate_cluster_batch(batch, config.cells)
    if store.cluster_id != config.account[:-2] or store.target_id != target.spreadsheet_id:
        raise ValueError("publisher session/target identity mismatch")
    if (adapter.spreadsheet_id, adapter.worksheet_id) != (target.spreadsheet_id, target.worksheet_id):
        raise ValueError("opened spreadsheet/worksheet differs from approved IDs")
    if changed_dates != [batch[0]["report_date"]]:
        raise ValueError("publish one complete changed cluster date at a time")
    if configuration_only:
        old_sources = store.current_results(changed_dates[0], changed_dates[0])
        expected = {(item["identity"]["cell_code"], item["generation_id"]) for item in batch}
        actual = {(item["cell_code"], item["generation_id"]) for item in old_sources}
        if len(old_sources) != len(batch) or actual != expected:
            raise ValueError("configuration-only operation cannot import new source generations")
    if additional_batches is None:
        through = max(changed_dates[0], store.latest_report_date() or changed_dates[0])
        additional_batches = recalculate_stale_history(store, config, through, submitted_dates=set(changed_dates))
    for cohort in additional_batches:
        validate_cluster_batch(cohort, config.cells)

    def call(method, *args):
        store.assert_alive()
        response = method(*args)
        store.assert_alive()
        return response

    metadata = call(adapter.metadata)
    if metadata["properties"]["sheetId"] != target.worksheet_id:
        raise ValueError("metadata worksheet identity mismatch")
    protections = metadata.get("protectedRanges", [])
    marker_entries = _marker_entries(metadata)
    if marker_entries and json.loads(marker_entries[0].get("metadataValue", "null")).get("status") == "DESIGN_PREVIEW":
        raise ValueError("design preview needs an approved DB-backed baseline before normal publication")
    if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
        raise RuntimeError("orphaned refresh guard requires operator reconciliation")
    if any(not item.get("description", "").startswith(MANAGED_PREFIX) for item in protections):
        raise ValueError("worksheet contains unmanaged protection; use a dedicated approved worksheet")
    computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    if len(computed) > 1 or any(item.get("range") != {"sheetId": target.worksheet_id} for item in computed):
        raise ValueError("managed computed protection must cover the whole approved worksheet")
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    staged = None
    write_started = False
    try:
        guard_value = {"description": GUARD_PREFIX + operation_id, "warningOnly": False,
                       "editors": {"users": [target.writer_email]}, "unprotectedRanges": []}
        if computed:
            guard_id = computed[0]["protectedRangeId"]
            request = {"updateProtectedRange": {"protectedRange": {
                "protectedRangeId": guard_id, **guard_value,
            }, "fields": "description,warningOnly,editors,unprotectedRanges"}}
        else:
            request = {"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": target.worksheet_id}, **guard_value,
            }}}
        lock_inputs = [{"updateProtectedRange": {"protectedRange": {
            "protectedRangeId": item["protectedRangeId"], "warningOnly": False,
            "editors": {"users": [target.writer_email]},
        }, "fields": "warningOnly,editors"}} for item in protections
            if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
        guard_reply = call(adapter.batch_update, [request, *lock_inputs])
        if not computed:
            guard_id = guard_reply["replies"][0]["addProtectedRange"]["protectedRange"]["protectedRangeId"]
        guarded = call(adapter.metadata)
        if not any(item.get("protectedRangeId") == guard_id
                   and item.get("range") == {"sheetId": target.worksheet_id}
                   and item.get("description") == GUARD_PREFIX + operation_id
                   and item.get("warningOnly", False) is False
                   and _editors_match(item.get("editors", {}), [target.writer_email], guarded.get("implicitOwnerEmails", []))
                   and not item.get("unprotectedRanges") for item in guarded.get("protectedRanges", [])):
            raise ValueError("temporary input protection was not confirmed")
        if any(item.get("unprotectedRanges") or not _editors_match(item.get("editors", {}), [target.writer_email], guarded.get("implicitOwnerEmails", []))
               for item in guarded.get("protectedRanges", []) if item.get("description") == MANAGED_PREFIX + "BANK INPUT"):
            raise ValueError("temporary bank input lock was not confirmed")
        previous = store.latest_publication()
        old_matrix = previous["snapshot"].get("matrix") if previous else None
        if old_matrix:
            actual = call(adapter.read, len(old_matrix["values"]), len(old_matrix["values"][0]))
        else:
            dimensions = metadata["properties"].get("gridProperties", {})
            actual = call(adapter.read, dimensions.get("rowCount", 1000), dimensions.get("columnCount", 27))
        manual = _manual_values(old_matrix, actual)
        audits = []

        def render_committed(session):
            through = max(changed_dates[0], session.latest_report_date() or changed_dates[0])
            current = session.current_results(config.first_report_date, through)
            identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
            facts = [{"identity": identities[row["cell_code"]],
                      "report_date": row["report_date"].isoformat() if hasattr(row["report_date"], "isoformat") else row["report_date"],
                      "calculation_id": row["calculation_id"], "generation_id": row["generation_id"],
                      "config_id": row["config_id"], "calculation": row["result"]} for row in current]
            report = build_cluster_report(config, facts, through)
            all_changed = sorted(set(changed_dates) | set(session.dirty_dates()))
            reset_changed = sorted((set() if configuration_only else set(changed_dates))
                                   | set(session.dirty_dates(reset_only=True)))
            matrix = render_cell_matrix(report, manual_values=manual,
                                        reset_dates=mandiri_reset_dates(report, reset_changed) if reset_changed else set())
            audits.extend(matrix.pop("manual_reset_audit"))
            from .cell_calculation import affected_publication_range
            first, last = affected_publication_range(config.first_report_date, all_changed, through)
            generations = {}
            for fact in facts:
                generations.setdefault(fact["report_date"], []).append(fact["calculation_id"])
            return {"start": first, "end": last,
                    "snapshot": {"worksheet_id": target.worksheet_id, "matrix": matrix,
                                 "source_calculations": {day: fingerprint(sorted(ids)) for day, ids in generations.items()}},
                    "refresh_audit_required": bool(audits)}

        staged = store.commit_and_stage(batch, config.cells, render_committed, write_gsheet=True,
                                       additional_batches=additional_batches)
        committed = store.publication_snapshot(staged["epoch"])
        matrix = committed["snapshot"]["matrix"]
        if committed["snapshot"]["worksheet_id"] != target.worksheet_id or matrix.get("dummy"):
            raise ValueError("committed snapshot is not approved for this worksheet")
        for audit in audits:
            store.record_manual_reset(staged["epoch"], audit["cell_code"], audit["report_date"],
                                      audit["previous_value"], audit["replacement_value"])
        expected = copy.deepcopy(matrix["values"])
        marker = {"status": "PENDING", "epoch": staged["epoch"], "fingerprint": staged["fingerprint"]}
        if len(expected) > 100_000:
            raise ValueError("matrix exceeds bounded publication size")
        dimensions = guarded["properties"].get("gridProperties", {})
        managed_width = max(len(expected[0]), len(old_matrix["values"][0]) if old_matrix else 0)
        setup = [{"unmergeCells": {"range": {"sheetId": target.worksheet_id,
            "startRowIndex": 0, "endRowIndex": 3, "startColumnIndex": 0,
            "endColumnIndex": managed_width}}}, {"updateSheetProperties": {"properties": {
            "sheetId": target.worksheet_id, "gridProperties": {
                **cell_grid_capacity(len(expected), len(expected[0]), dimensions),
                "frozenRowCount": 2 if matrix.get("layout_version") == 3 else 1, "frozenColumnCount": 3,
            }}, "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)"}}]
        # Stage DB state before any irreversible governed values mutation.
        if staged["status"] == "COMPLETE":
            marker["status"] = "COMPLETE"
            _verify_marker(call(adapter.metadata), marker)
            if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
                raise ValueError("previous complete report no longer matches governed snapshot")
        else:
            store.begin_sheet_write(staged["epoch"])
            write_started = True
            call(adapter.batch_update, setup + [_marker_request(target, guarded, marker)] + _status_requests(target, marker))
            for index, request in enumerate(matrix_write_requests(expected, target.worksheet_id, rows_per_request=rows_per_request)):
                call(adapter.batch_update, [request])
                store.confirm_chunk(staged["epoch"], f"rows-{index}")
            if old_matrix and len(old_matrix["values"]) > len(expected):
                blank = [[""] * len(old_matrix["values"][0])]
                clear = matrix_write_requests(blank, target.worksheet_id, start_row=len(expected))[0]
                clear["updateCells"]["range"]["endRowIndex"] = len(old_matrix["values"])
                call(adapter.batch_update, [clear])
            if old_matrix and len(old_matrix["values"][0]) > len(expected[0]):
                call(adapter.batch_update, [{"updateCells": {
                    "range": {"sheetId": target.worksheet_id, "startRowIndex": 0,
                              "endRowIndex": max(len(expected), len(old_matrix["values"])),
                              "startColumnIndex": len(expected[0]), "endColumnIndex": managed_width},
                    "rows": [], "fields": "userEnteredValue,userEnteredFormat,note",
                }}, {"updateDimensionProperties": {"range": {"sheetId": target.worksheet_id,
                    "dimension": "COLUMNS", "startIndex": len(expected[0]), "endIndex": managed_width},
                    "properties": {"pixelSize": 100}, "fields": "pixelSize"}}])
            if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
                raise ValueError("governed generation/value/formula readback mismatch")
            _verify_marker(call(adapter.metadata), marker)
        finish = [{"updateSheetProperties": {"properties": {"sheetId": target.worksheet_id,
            "gridProperties": cell_grid_capacity(len(expected), len(expected[0]), dimensions)},
            "fields": "gridProperties(rowCount,columnCount)"}}, {"unmergeCells": {"range": {"sheetId": target.worksheet_id,
            "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0,
            "endColumnIndex": len(expected[0])}}}]
        finish += _format_requests(target, matrix) + _protection_requests(target, matrix, guarded, guard_id)
        marker["status"] = "COMPLETE"
        finish.append(_marker_request(target, call(adapter.metadata), marker))
        finish.extend(_status_requests(target, marker))
        if len(json.dumps({"requests": finish}).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("protection/confirmation request exceeds publication size limit")
        call(adapter.batch_update, finish)
        final_metadata = call(adapter.metadata)
        if any(item.get("description", "").startswith(GUARD_PREFIX) for item in final_metadata.get("protectedRanges", [])):
            raise ValueError("temporary protection restoration was not confirmed")
        _verify_final_protections(target, matrix, final_metadata)
        _verify_marker(final_metadata, marker)
        if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
            raise ValueError("final generation/formula readback mismatch")
        if staged["status"] != "COMPLETE":
            store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])
        store.finish_target_operation(operation_id)
        return {**staged, "status": "COMPLETE"}
    except Exception as exc:
        # Never retry or remove a guard after an unconfirmed external mutation.
        # A dead connection leaves its durable IN_FLIGHT intent for recovery.
        try:
            if staged and write_started:
                store.mark_unknown(staged["epoch"], type(exc).__name__)
            store.finish_target_operation(operation_id, unknown_error=type(exc).__name__)
        except Exception:
            pass
        raise
