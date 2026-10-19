"""Approved presentation-only update of the existing synthetic tab. No database."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import uuid
from pathlib import Path

from finpay_pipeline.cell_calculation import build_cluster_report
from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.cell_publication import (
    ExistingSheetAdapter, CellSheetTarget, GUARD_PREFIX, MANAGED_PREFIX,
    _editors_match, _format_requests, _manual_values, _marker_entries,
    _marker_request, _padded, _protection_requests, _status_requests,
    _verify_final_protections, _verify_marker, matrix_write_requests,
)
from finpay_pipeline.cell_sheets import cell_grid_capacity, render_cell_matrix
from finpay_pipeline.cell_workflow import make_cell_sheet_client, prepare_cell_bundle
from finpay_pipeline.tests.run_cell_e2e_canary import COPY_ID, TAB_ID, TAB_NAME, DATES

CONFIG_SHA = "7f7355d731d461729424000478cd06b4c348840fd1e89fee2bf464a9bc771215"
WRITER_SHA = "2ab7954647e7cca1673131da541b0121e45d5c3226bcae5584e1b01dbce5d5b4"
BUNDLE_SHA = {
    "2026-10-13": "bf9c4e6992a00e06f2d4ac35887732973b6cb4c13b496f9e1c385921164ea23b",
    "2026-10-14": "09ad150108c107d7a3b89a2c617039d498404b667e18775d9489bbce42c4b69a",
    "2026-10-15": "0aee2ab7a2ba96953027784a24e06ffa961a4190b97a6f62fd3af086af205a3f",
}


def _write(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _create_preview(book, report, target, output_root, *, tab_title=TAB_NAME):
    if book.id != COPY_ID:
        raise ValueError("preview creation is limited to the approved copied spreadsheet")
    sheet_id = target.worksheet_id
    compact = render_cell_matrix(report)
    rows, width = len(compact["values"]), len(compact["values"][0])
    inputs = [{"sheetId": sheet_id, "startRowIndex": item["row"] - 1, "endRowIndex": item["row"],
               "startColumnIndex": item["column"] - 1, "endColumnIndex": item["column"]}
              for item in compact["editable_inputs"]]
    protections = [{"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sheet_id}, "description": MANAGED_PREFIX + "COMPUTED", "warningOnly": False,
        "editors": {"users": [target.writer_email]}, "unprotectedRanges": inputs,
    }}}]
    protections.extend({"addProtectedRange": {"protectedRange": {
        "range": area, "description": MANAGED_PREFIX + "BANK INPUT", "warningOnly": False,
        "editors": {"users": [target.writer_email]},
    }}} for area in inputs)
    marker = {"status": "DESIGN_PREVIEW", "epoch": 0, "fingerprint": fingerprint(compact),
              "layout_version": 3, "database_publication_updated": False}
    requests = [
        {"addSheet": {"properties": {"sheetId": sheet_id, "title": tab_title, "gridProperties": {
            **cell_grid_capacity(rows, width), "frozenRowCount": 2,
            "frozenColumnCount": 3, "hideGridlines": True}}}},
        *matrix_write_requests(compact["values"], sheet_id), *_format_requests(target, compact),
        *protections, _marker_request(target, {}, marker), *_status_requests(target, marker),
    ]
    if len(json.dumps({"requests": requests}).encode()) > 1_500_000:
        raise ValueError("new preview exceeds bounded atomic request size")
    book.batch_update({"requests": requests})
    worksheet = book.get_worksheet_by_id(sheet_id)
    adapter = ExistingSheetAdapter(book, worksheet)
    metadata = adapter.metadata()
    _verify_marker(metadata, marker)
    _verify_final_protections(target, compact, metadata)
    if _padded(adapter.read(rows, width), rows, width) != compact["values"]:
        raise ValueError("new compact preview formula/value readback mismatch")
    evaluated = worksheet.get(f"A1:N{rows}", value_render_option="UNFORMATTED_VALUE")
    if _padded(evaluated, rows, width) != compact["numeric_preview"]:
        raise ValueError("new preview evaluated values differ from synthetic model")
    _write(output_root / "compact-matrix.json", compact)
    return {"layout_version": 3, "columns": width, "rows": rows, "preserved_manual_inputs": 0,
            "numeric_and_text_results_preserved": True, "created_approved_missing_tab": True,
            "cloud_status": "DESIGN_PREVIEW", "database_publication_updated": False}


def update(input_root, output_root, credential_path, *, create_missing=False):
    output_root.mkdir(parents=True, exist_ok=True)
    journal = output_root / "design-preview-result.json"
    if journal.exists():
        raise RuntimeError("preview journal already exists; inspect/reconcile rather than blind retry")
    outcome = {"scope": "APPROVED_SYNTHETIC_TAB_PRESENTATION_ONLY", "spreadsheet_id": COPY_ID,
               "worksheet_id": TAB_ID, "database_called": False, "original_modified": False}
    mutation_started = False
    with (output_root / "preview.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            info = json.loads(Path(credential_path).read_text())
            writer = info["client_email"]
            if hashlib.sha256(writer.encode()).hexdigest() != WRITER_SHA:
                raise ValueError("credential principal differs from approved synthetic baseline")
            facts, config = [], None
            for day in DATES:
                _, configs, _, batches = prepare_cell_bundle(
                    input_root / day / "cell_sources.zip", input_root / "approved-canary-configuration.json",
                    CONFIG_SHA, BUNDLE_SHA[day],
                )
                config = configs["412110_B"]
                facts.extend(batches["412110_B"])
            report = build_cluster_report(config, facts, DATES[-1])
            wide = render_cell_matrix(report, presentation_version=2)
            old_snapshot = {"worksheet_id": TAB_ID, "matrix": copy.deepcopy(wide),
                "source_calculations": {day: fingerprint(sorted(item["calculation_id"] for item in facts
                                            if item["report_date"] == day)) for day in DATES}}
            old_snapshot["matrix"].pop("manual_reset_audit")
            expected_old_fp = fingerprint({"cluster_id": "412110", "target_id": COPY_ID,
                "range_start": "2026-10-14", "range_end": "2026-10-15", "snapshot": old_snapshot})
            book = make_cell_sheet_client(info).open_by_key(COPY_ID)
            tabs = book.fetch_sheet_metadata()["sheets"]
            found = [item for item in tabs if item["properties"]["sheetId"] == TAB_ID or item["properties"]["title"] == TAB_NAME]
            target = CellSheetTarget(COPY_ID, TAB_ID, writer, (writer,))
            if create_missing:
                if found:
                    raise ValueError("creation approval cannot overwrite an existing preview tab")
                outcome.update(status="IN_FLIGHT", operation_id="CREATE_DESIGN_" + uuid.uuid4().hex)
                _write(journal, outcome)
                mutation_started = True
                outcome.update(_create_preview(book, report, target, output_root), status="PASS")
                return
            worksheet = book.get_worksheet_by_id(TAB_ID)
            if worksheet.title != TAB_NAME:
                raise ValueError("approved synthetic tab identity mismatch")
            adapter = ExistingSheetAdapter(book, worksheet)
            metadata = adapter.metadata()
            entries = _marker_entries(metadata)
            if len(entries) != 1:
                raise ValueError("published synthetic baseline metadata missing")
            previous = json.loads(entries[0]["metadataValue"])
            if previous.get("status") != "COMPLETE" or previous.get("fingerprint") != expected_old_fp:
                raise ValueError("test tab is not the independently reconstructed canary baseline")
            _verify_final_protections(target, wide, metadata)
            protections = metadata["protectedRanges"]
            if any(p.get("description", "").startswith(GUARD_PREFIX) for p in protections):
                raise ValueError("unresolved refresh guard blocks the design update")
            computed = [p for p in protections if p.get("description") == MANAGED_PREFIX + "COMPUTED"]
            guard_id = computed[0]["protectedRangeId"]
            operation = "DESIGN_" + uuid.uuid4().hex
            outcome.update(status="IN_FLIGHT", operation_id=operation, source_publication=previous)
            _write(journal, outcome)
            mutation_started = True
            guard = {"updateProtectedRange": {"protectedRange": {
                "protectedRangeId": guard_id, "description": GUARD_PREFIX + operation,
                "editors": {"users": [writer]}, "warningOnly": False, "unprotectedRanges": [],
            }, "fields": "description,editors,warningOnly,unprotectedRanges"}}
            locks = [{"updateProtectedRange": {"protectedRange": {
                "protectedRangeId": p["protectedRangeId"], "editors": {"users": [writer]}, "warningOnly": False,
            }, "fields": "editors,warningOnly"}} for p in protections if p.get("description") == MANAGED_PREFIX + "BANK INPUT"]
            adapter.batch_update([guard, *locks])
            guarded = adapter.metadata()
            for p in guarded.get("protectedRanges", []):
                if (p.get("warningOnly", False) is not False or p.get("unprotectedRanges")
                        or not _editors_match(p.get("editors", {}), [writer], guarded["implicitOwnerEmails"])):
                    raise ValueError("exclusive synthetic edit window not confirmed")
            protected = next(p for p in guarded["protectedRanges"] if p["protectedRangeId"] == guard_id)
            if protected.get("range") != {"sheetId": TAB_ID} or protected.get("description") != GUARD_PREFIX + operation:
                raise ValueError("full-sheet guard missing")
            _verify_marker(guarded, previous)
            actual = adapter.read(len(wide["values"]), 20)
            manual = _manual_values(wide, actual)
            wide_with_manual = render_cell_matrix(report, manual_values=manual, presentation_version=2)
            if _padded(actual, len(wide["values"]), 20) != wide_with_manual["values"]:
                raise ValueError("unrelated computed-cell edit detected; refusing overwrite")
            compact = render_cell_matrix(report, manual_values=manual)
            marker = {"status": "DESIGN_PREVIEW", "epoch": previous["epoch"],
                      "fingerprint": fingerprint(compact), "source_publication_fingerprint": previous["fingerprint"],
                      "layout_version": 3, "database_publication_updated": False}
            rows, width = len(compact["values"]), len(compact["values"][0])
            requests = [
                {"unmergeCells": {"range": {"sheetId": TAB_ID, "startRowIndex": 0, "endRowIndex": 3,
                    "startColumnIndex": 0, "endColumnIndex": 20}}},
                {"updateSheetProperties": {"properties": {"sheetId": TAB_ID, "gridProperties": {
                    "frozenRowCount": 2, "frozenColumnCount": 3}},
                    "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
                {"updateCells": {"range": {"sheetId": TAB_ID, "startRowIndex": 0, "endRowIndex": rows,
                    "startColumnIndex": width, "endColumnIndex": 20}, "rows": [],
                    "fields": "userEnteredValue,userEnteredFormat"}},
                {"updateDimensionProperties": {"range": {"sheetId": TAB_ID, "dimension": "COLUMNS",
                    "startIndex": width, "endIndex": 20}, "properties": {"pixelSize": 100}, "fields": "pixelSize"}},
                *matrix_write_requests(compact["values"], TAB_ID),
                *_format_requests(target, compact),
                *_protection_requests(target, compact, guarded, guard_id),
                _marker_request(target, guarded, marker), *_status_requests(target, marker),
            ]
            if len(json.dumps({"requests": requests}).encode()) > 1_500_000:
                raise ValueError("design migration exceeds bounded single-batch size")
            adapter.batch_update(requests)
            final = adapter.metadata()
            _verify_marker(final, marker)
            _verify_final_protections(target, compact, final)
            if _padded(adapter.read(rows, width), rows, width) != compact["values"]:
                raise ValueError("compact formula/value readback mismatch")
            values = worksheet.get(f"A1:T{rows}", value_render_option="UNFORMATTED_VALUE")
            for i, row in enumerate(compact["numeric_preview"]):
                for j, expected in enumerate(row):
                    found = values[i][j] if i < len(values) and j < len(values[i]) else ""
                    if found != expected:
                        raise ValueError("compact evaluated result differs from preserved wide calculations")
            if any(any(value != "" for value in row[width:]) for row in values):
                raise ValueError("stale removed detail columns remain")
            dimensions = book.fetch_sheet_metadata(params={"ranges": f"'{TAB_NAME}'!O1:T1",
                "fields": "sheets(data(columnMetadata(pixelSize)))"})
            obsolete = dimensions["sheets"][0]["data"][0]["columnMetadata"]
            if len(obsolete) != 6 or any(item.get("pixelSize") != 100 for item in obsolete):
                raise ValueError("obsolete column widths were not reset")
            _write(output_root / "compact-matrix.json", compact)
            outcome.update(status="PASS", layout_version=3, columns=width, rows=rows,
                preserved_manual_inputs=len(manual), numeric_and_text_results_preserved=True,
                stale_columns_cleared=True, cloud_status="DESIGN_PREVIEW", database_publication_updated=False)
            outcome["obsolete_column_widths_reset"] = True
        except Exception as exc:
            outcome.update(status="UNKNOWN" if mutation_started else "VALIDATION_FAILED", error_class=type(exc).__name__)
            if isinstance(exc, ValueError):
                outcome["reason"] = str(exc)
            raise
        finally:
            _write(journal, outcome)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--execute-approved-design-update", action="store_true")
    parser.add_argument("--create-approved-missing-preview", action="store_true")
    args = parser.parse_args()
    if not args.execute_approved_design_update:
        parser.error("explicit approved design-update execution flag required")
    try:
        update(Path(args.input_dir), Path(args.output_dir), args.credential_file,
               create_missing=args.create_approved_missing_preview)
    except Exception:
        print("Design update stopped; inspect the sanitized local preview journal before retry.")
        raise SystemExit(1)
    print("Approved synthetic design preview updated; no database publication changed.")
