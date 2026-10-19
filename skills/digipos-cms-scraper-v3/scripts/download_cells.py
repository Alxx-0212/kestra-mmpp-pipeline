#!/usr/bin/env python3
"""Ingest one DigiPOS _B account into one XLSX artifact per district cell."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import digipos_v3 as dg  # noqa: E402


def _new_page(browser, account: str, password: str) -> tuple[object, dict]:
    page = browser.new_page(accept_downloads=True, viewport={"width": 1440, "height": 1000})
    shims = dg.install_googleapis_compatibility(page)
    dg.login(page, account, password)
    dg.open_monitoring(page)
    return page, shims


def _ingest_cell(
    page,
    target: dg.CellTarget,
    report_date: str,
    run_dir: Path,
    page_len: int,
    result: dict,
) -> dict:
    search = dg.search_cell(page, target, report_date)
    result["footer_total"] = search["footer_total"]
    result["search_status_code"] = search["status_code"]
    result["search_records_filtered"] = search["records_filtered"]
    result["search_info_text"] = search["info_text"]

    if search["footer_total"] == 0:
        result["status"] = "empty"
        result["validated_rows"] = 0
        result["method"] = "datatable-search-xhr"
        return result

    raw_rows, validation = dg.fetch_all_rows(
        page,
        target,
        report_date,
        footer_total=search["footer_total"],
        page_len=page_len,
    )
    rows = [dg.row_to_cells(row, index) for index, row in enumerate(raw_rows, 1)]
    cell_dir = run_dir / target.account / target.cell_code
    cell_dir.mkdir(parents=True, exist_ok=True)
    name = dg.artifact_filename(target, report_date)
    final_path = cell_dir / name
    pending_path = cell_dir / f".{name}.pending.xlsx"
    try:
        dg.write_xlsx(pending_path, rows)
        saved_rows = dg.row_count_from_xlsx(pending_path, report_date)
        if saved_rows != search["footer_total"]:
            raise RuntimeError(
                f"saved workbook has {saved_rows} rows; expected {search['footer_total']}"
            )
        pending_path.replace(final_path)
    finally:
        pending_path.unlink(missing_ok=True)

    result.update(
        {
            "status": "validated",
            "artifact": str(final_path.relative_to(run_dir)),
            "sha256": dg.sha256_file(final_path),
            "footer_total": search["footer_total"],
            "validated_rows": saved_rows,
            "method": "datatable-xhr-length-rewrite",
            "download_length": validation["length"],
            "download_records_filtered": validation["recordsFiltered"],
        }
    )
    return result


def run(args: argparse.Namespace) -> tuple[dict, int]:
    report_date = datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    cluster_id = dg.cluster_id_for(args.account)
    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    output_root = Path(args.output_dir).resolve()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "source": "DigiPOS CMS /deposit/monitoring-riwayat",
        "started_at": started_at.isoformat(),
        "report_date": report_date,
        "account": args.account,
        "cluster_id": cluster_id,
        "processing": "ingestion-only; no cluster or cross-cell aggregation",
        "aggregate_options": [],
        "cells": [],
    }

    from playwright.sync_api import sync_playwright

    password = dg.load_password()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = None
        try:
            page, shims = _new_page(browser, args.account, password)
            report["browser_shims"] = shims
            cells, aggregate_options = dg.read_cells(page, args.account)
            report["aggregate_options"] = aggregate_options
            report["discovered_cell_count"] = len(cells)
            print(
                f"{args.account} / cluster {cluster_id}: "
                f"{len(cells)} cells; aggregate options excluded={len(aggregate_options)}",
                flush=True,
            )

            for target in cells:
                print(f"Cell {target.cell_code}: {target.label}", flush=True)
                last_error = None
                result = dg.result_dict(target, report_date)
                for attempt in range(1, args.max_attempts + 1):
                    try:
                        if page is None or page.is_closed():
                            page, retry_shims = _new_page(browser, args.account, password)
                            report.setdefault("retry_browser_shims", []).append(retry_shims)
                        _ingest_cell(page, target, report_date, run_dir, args.page_len, result)
                        result["attempt"] = attempt
                        report["cells"].append(result)
                        print(
                            f"  {result['status']}: rows={result['validated_rows']} "
                            f"artifact={result['artifact'] or '-'}",
                            flush=True,
                        )
                        last_error = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"{type(exc).__name__}: {exc}"
                        result.setdefault("attempt_errors", []).append(last_error)
                        if page is not None and not page.is_closed():
                            page.close()
                        page = None
                        if attempt < args.max_attempts:
                            print(
                                f"  attempt {attempt} failed: {last_error}; relogin and retry",
                                flush=True,
                            )
                if last_error is not None:
                    result.update(
                        {"status": "failed", "attempt": args.max_attempts, "error": last_error}
                    )
                    report["cells"].append(result)
                    print(f"  failed after {args.max_attempts} attempts: {last_error}", flush=True)
        finally:
            if page is not None and not page.is_closed():
                page.close()
            browser.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {
        "validated": sum(cell["status"] == "validated" for cell in report["cells"]),
        "empty": sum(cell["status"] == "empty" for cell in report["cells"]),
        "failed": sum(cell["status"] == "failed" for cell in report["cells"]),
    }
    return report, 1 if report["summary"]["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=sorted(dg.ACCOUNT_ROSTER))
    parser.add_argument("--date", required=True, help="Report date in YYYY-MM-DD")
    parser.add_argument("--output-dir", required=True, help="Fresh output root for this run")
    parser.add_argument("--manifest", help="Manifest path; defaults to <run-dir>/manifest.json")
    parser.add_argument("--page-len", type=int, default=5_000)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.page_len <= dg.MAX_PAGE_LENGTH:
        parser.error(f"--page-len must be between 1 and {dg.MAX_PAGE_LENGTH}")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be positive")

    report, exit_code = run(args)
    import json

    output_root = Path(args.output_dir).resolve()
    run_dir = output_root / report["run_id"]
    manifest = Path(args.manifest).resolve() if args.manifest else run_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(dg.manifest_json(report), encoding="utf-8")
    print(f"Manifest: {manifest}", flush=True)
    print(json.dumps(report["summary"], sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
