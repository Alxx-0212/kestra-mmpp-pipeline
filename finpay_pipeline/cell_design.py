"""Generate explicitly synthetic local cell-report designs without live clients."""
from __future__ import annotations

import json
from pathlib import Path

from .cell_calculation import CellReportConfig, build_cluster_report, calculate_cell_day
from .cell_contracts import OBSERVED_CELL_ROSTER, fingerprint
from .cell_sheets import mandiri_reset_dates, render_cell_matrix, write_cell_design_workbook


def dummy_cluster_report(account: str, codes: tuple[str, ...], cluster_index: int = 0) -> dict:
    dates = ("2026-10-13", "2026-10-14", "2026-10-15")
    config = CellReportConfig(
        account, codes, dates[0], dates[0] + "T00:00:00+08:00",
        {code: (index + 1) * 1_000_000 + cluster_index * 100_000
         for index, code in enumerate(codes)},
        "DUMMY-ROSTER-NOT-PRODUCTION",
    )
    facts = []
    for day_index, report_date in enumerate(dates):
        for cell_index, cell in enumerate(config.cells):
            credit = (cell_index + 1) * 100_000 + day_index * 10_000
            debit = 0
            if cell_index == len(codes) - 1:
                credit, debit = 0, credit // 2
            rows = [{
                "No": 1, "Transaction Date": report_date + " 12:00:00",
                "Transaction ID": f"DUMMY_{cell.cell_code}_{day_index}_SALE",
                "Saldo Awal": config.opening_balances[cell.cell_code],
                "Kredit": credit, "Debet": debit,
                "Saldo Akhir": config.opening_balances[cell.cell_code] + credit - debit,
                "Transaction Type": "Credit" if credit else "Debit",
                "Transaction": "RECHARGE", "Nomor RS": "-", "Remarks": "Transaksi Sellthru",
            }]
            if day_index:
                transfer = (cell_index + 1) * 50_000
                rows.append({
                    "No": 2, "Transaction Date": report_date + " 09:00:00",
                    "Transaction ID": f"DUMMY_{cell.cell_code}_{day_index}_BANK",
                    "Saldo Awal": config.opening_balances[cell.cell_code],
                    "Kredit": transfer, "Debet": 0,
                    "Saldo Akhir": config.opening_balances[cell.cell_code] + transfer,
                    "Transaction Type": "Credit", "Transaction": "CASHOUT APOLLO",
                    "Nomor RS": "-",
                    "Remarks": "APOLLO MRC100 " + report_date[8:10] + "-10-2026",
                })
            if day_index == 1 and cell_index == len(codes) - 1:
                rows = []  # A verified empty district day, not missing coverage.
            manifest = {
                **cell.to_dict(), "report_date": report_date,
                "status": "validated" if rows else "empty",
                "validated_rows": len(rows), "footer_total": len(rows),
                "artifact": "DUMMY_SOURCE.xlsx" if rows else None,
                "source_sha256": fingerprint(rows),
            }
            facts.append(calculate_cell_day(cell, manifest, rows, config))
    return build_cluster_report(config, facts, dates[-1])


def generate_dummy_cell_design(output_dir: str | Path, *, presentation_version=3) -> dict:
    matrices = []
    for index, (account, codes) in enumerate(sorted(OBSERVED_CELL_ROSTER.items())):
        report = dummy_cluster_report(account, codes, index)
        manual = {("2026-10-13", codes[0]): 45_000, ("2026-10-14", codes[0]): 33_333}
        matrices.append(render_cell_matrix(
            report, manual_values=manual,
            reset_dates=mandiri_reset_dates(report, ["2026-10-15"]), dummy=True,
            presentation_version=presentation_version,
        ))
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    workbook = directory / "finpay-cell-design-DUMMY.xlsx"
    data = directory / "finpay-cell-design-DUMMY.json"
    write_cell_design_workbook(workbook, matrices)
    data.write_text(json.dumps({
        "dummy_data_only": True, "live_calls": 0,
        "notice": "NUMERIC PREVIEW IS PYTHON-CALCULATED, NOT EXCEL/GOOGLE FORMULA VALIDATION",
        "matrices": matrices,
    }, indent=2), encoding="utf-8")
    return {"dummy_data_only": True, "clusters": len(matrices),
            "cells": sum(block["cell_code"] is not None for matrix in matrices for block in matrix["blocks"]),
            "workbook": str(workbook), "matrix_json": str(data)}
