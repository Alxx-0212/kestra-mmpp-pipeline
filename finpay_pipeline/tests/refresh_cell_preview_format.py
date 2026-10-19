"""Fixed-target, format-only follow-up approved for the synthetic preview."""
import argparse
import hashlib
import json
from pathlib import Path

from finpay_pipeline.cell_publication import ExistingSheetAdapter, CellSheetTarget, _format_requests, _marker_entries
from finpay_pipeline.cell_sheets import cell_grid_capacity
from finpay_pipeline.cell_workflow import make_cell_sheet_client
from finpay_pipeline.tests.run_cell_e2e_canary import COPY_ID, TAB_ID, TAB_NAME
from finpay_pipeline.tests.update_cell_design_preview import WRITER_SHA


def refresh(matrix_path, output_path, credential_path):
    if output_path.exists():
        raise ValueError("format evidence exists; inspect before retry")
    report = {"scope": "APPROVED_SYNTHETIC_FORMAT_ONLY", "database_called": False}
    started = False
    try:
        matrix = json.loads(matrix_path.read_text())
        if matrix.get("layout_version") != 3 or matrix.get("account") != "412110_B":
            raise ValueError("unexpected synthetic matrix")
        info = json.loads(credential_path.read_text())
        if hashlib.sha256(info["client_email"].encode()).hexdigest() != WRITER_SHA:
            raise ValueError("wrong service-account identity")
        book = make_cell_sheet_client(info).open_by_key(COPY_ID)
        worksheet = book.get_worksheet_by_id(TAB_ID)
        if worksheet.title != TAB_NAME:
            raise ValueError("test tab identity mismatch")
        adapter = ExistingSheetAdapter(book, worksheet)
        metadata = adapter.metadata()
        entries = _marker_entries(metadata)
        if len(entries) != 1 or json.loads(entries[0]["metadataValue"]).get("status") != "DESIGN_PREVIEW":
            raise ValueError("formatting is limited to the approved design preview")
        rows, width = len(matrix["values"]), len(matrix["values"][0])
        before = adapter.read(rows, width)
        other_tabs = [item["properties"] for item in book.fetch_sheet_metadata()["sheets"]
                      if item["properties"]["sheetId"] != TAB_ID]
        target = CellSheetTarget(COPY_ID, TAB_ID, info["client_email"], (info["client_email"],))
        capacity = cell_grid_capacity(rows, width, metadata["properties"]["gridProperties"])
        requests = [
            {"updateSheetProperties": {"properties": {"sheetId": TAB_ID, "gridProperties": capacity},
                                       "fields": "gridProperties(rowCount,columnCount)"}},
            {"unmergeCells": {"range": {"sheetId": TAB_ID, "startRowIndex": 0, "endRowIndex": 2,
                                        "startColumnIndex": 0, "endColumnIndex": width}}},
            *_format_requests(target, matrix),
        ]
        if any(item.get("updateCells", {}).get("fields", "").find("userEnteredValue") >= 0 for item in requests):
            raise ValueError("format-only operation contains a value write")
        if len(json.dumps({"requests": requests}).encode()) > 1_500_000:
            raise ValueError("format batch too large")
        report["status"] = "IN_FLIGHT"
        output_path.write_text(json.dumps(report))
        started = True
        adapter.batch_update(requests)
        if adapter.read(rows, width) != before:
            raise ValueError("concurrent or unexpected value/formula change; no automatic overwrite")
        final = adapter.metadata()
        if final.get("developerMetadata") != metadata.get("developerMetadata") or final.get("protectedRanges") != metadata.get("protectedRanges"):
            raise ValueError("formatting unexpectedly changed generation/protection metadata")
        if [item["properties"] for item in book.fetch_sheet_metadata()["sheets"]
            if item["properties"]["sheetId"] != TAB_ID] != other_tabs:
            raise ValueError("non-target tab changed")
        report.update(status="PASS", values_and_formulas_unchanged=True, metadata_and_protections_unchanged=True,
                      target_id=TAB_ID, dimensions=final["properties"]["gridProperties"])
    except Exception as exc:
        report.update(status="UNKNOWN" if started else "VALIDATION_FAILED", error_class=type(exc).__name__)
        raise
    finally:
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--execute-approved-format-update", action="store_true")
    args = parser.parse_args()
    if not args.execute_approved_format_update:
        parser.error("explicit approved format operation required")
    refresh(args.matrix, args.output, args.credential_file)
