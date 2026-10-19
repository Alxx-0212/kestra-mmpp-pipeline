"""Independent October cash-flow oracle over recorded eligible ledger rows.

This does not import the report producer or certify Finance opening balances,
bank statements, or the correctness of recorded family dispositions.
"""
from collections import defaultdict

# Pinned legacy cash-flow contract; October sales hold is intentionally invoice-only.
DETAIL = ("CASHOUT APOLLO", "QRISDUWIT", "DISBURSEMENT", "FEETRANSAKSI", "RECHARGE",
    "RECHARGEFEE", "PEMBELIAN RECHARGE OUT CLUSTER", "RECHARGE OUT CLUSTER", "RECHARGE OUT CLUSTER FEE",
    "REVERSAL - NGRS", "REVERSAL - NGRS FEE", "REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER",
    "REVERSAL - RECHARGE OUT CLUSTER", "REVERSAL - RECHARGE OUT CLUSTER FEE", "SELLTHRU", "SELLTHRUFEE")
GROUPS = {
    "NGRS": ("RECHARGE", "RECHARGEFEE", "PEMBELIAN RECHARGE OUT CLUSTER"),
    "Recharge Out Cluster": ("RECHARGE OUT CLUSTER", "RECHARGE OUT CLUSTER FEE"),
    "Reversal - NGRS": ("REVERSAL - NGRS", "REVERSAL - NGRS FEE"),
    "Reversal - Pembelian Recharge Out Cluster": ("REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER",),
    "Reversal - Recharge Out Cluster": ("REVERSAL - RECHARGE OUT CLUSTER", "REVERSAL - RECHARGE OUT CLUSTER FEE"),
    "PPOB": ("FEETRANSAKSI",), "ST": ("SELLTHRU", "SELLTHRUFEE"),
    "DISBURSEMENT": ("DISBURSEMENT",), "QRISDUWIT": ("QRISDUWIT",),
}


def audit_scope(runtime, root, config_sha256):
    import hashlib
    import json
    from finpay_pipeline.tests import run_cell_live_design_e2e as short_test
    from finpay_pipeline.tests import run_cell_october_backfill as backfill
    target = runtime["database_target"]
    small = ((target["host"], target["port"], target["database"], target["schema"], target["user"],
              runtime["spreadsheet_id"]) == ("127.0.0.1", short_test.PORT, short_test.DATABASE,
              short_test.SCHEMA, short_test.USER, short_test.BOOK))
    full = ((target["host"], target["port"], target["database"], target["schema"], target["user"],
             runtime["spreadsheet_id"]) == ("127.0.0.1", backfill.PORT, backfill.DATABASE,
             backfill.SCHEMA, backfill.USER, backfill.BOOK)
        and runtime.get("worksheet_naming") == "cluster_alias"
        and runtime.get("worksheet_targets") == backfill.TARGETS
        and config_sha256 == run_october_config_sha256(root))
    if not (small or full):
        raise ValueError("audit target is not an exact approved test scope")
    expected_raw_rows = 0
    if full:
        receipts = json.loads((root / "sources.json").read_text())
        result = json.loads((root / "result.json").read_text())
        receipt_sha256 = hashlib.sha256((root / "sources.json").read_bytes()).hexdigest()
        if (result.get("status") != "PASS" or result.get("source_receipt_sha256") != receipt_sha256
            or result.get("source_summary") != receipts or set(receipts) != set(backfill.DATES)
            or sum(item.get("cells", 0) for item in receipts.values()) != 306):
            raise ValueError("published audit source receipt/coverage mismatch")
        expected_raw_rows = sum(item["rows"] for item in receipts.values())
    password = (root / "db-password") if small else (
        root.parent / "cell_live_design_e2e_20261017" / "db-password")
    return {"schema": target["schema"], "database": target["database"], "user": target["user"],
        "host": target["host"], "port": target["port"], "workbook": runtime["spreadsheet_id"],
        "password_file": password, "expected_raw_rows": expected_raw_rows,
        "expected_cell_days": 306 if full else 54, "full_backfill": full}


def run_october_config_sha256(root):
    from finpay_pipeline.tests.run_cell_october_backfill import sha
    return sha(root / "configuration.json")


def audit_summary(config, facts, matrix, *, manual_inputs=None):
    """Check every October DETAIL/MANDIRI/CASH numeric cell without producer math."""
    manual_inputs = manual_inputs or {}
    if matrix["layout_version"] != 5:
        raise ValueError("financial oracle requires the corrected current layout")
    indexed = {}
    checked_rows = 0
    for fact in facts:
        date, code = fact["report_date"], fact["identity"]["cell_code"]
        if not date.startswith("2026-10-") or code not in config.cell_codes or (date, code) in indexed:
            raise ValueError("unsupported or duplicate financial audit coverage")
        totals = defaultdict(lambda: [0, 0, 0])
        for row in fact["calculation"]["summary_ready"]:
            credit, debit = row["Kredit"], row["Debet"]
            if type(credit) is not int or type(debit) is not int:
                raise ValueError("eligible ledger contains noninteger amounts")
            key = row["processed_transaction_label"].strip().upper()
            totals[key][0] += credit
            totals[key][1] += debit
            totals[key][2] += 1
            checked_rows += 1
        supplied = {row["Transaction"].strip().upper():
            [row["Sum_of_Kredit"], row["Sum_of_Debet"], row["Transaction_Count"]]
            for row in fact["calculation"]["summary"]}
        if supplied != dict(totals):
            raise ValueError("stored category summary differs from eligible ledger aggregation")
        if set(totals) - set(DETAIL) - {"SELLTHRUSALESFEE"}:
            raise ValueError("eligible category lacks an approved cash-flow bridge")
        indexed[(date, code)] = totals
    dates = [item["report_date"] for item in matrix["date_ranges"]]
    if set(indexed) != {(date, code) for date in dates for code in config.cell_codes}:
        raise ValueError("incomplete financial audit cohort")
    balances = dict(config.opening_balances)
    running, bank = dict.fromkeys(config.cell_codes, 0), dict.fromkeys(config.cell_codes, "PENDING NEXT DAY")
    blocks = {item["cell_code"]: item for item in matrix["blocks"]}
    if set(blocks) != set(config.cell_codes):
        raise ValueError("financial audit block identity mismatch")
    comparisons = 0
    for code, block in blocks.items():
        if matrix["numeric_preview"][2][block["end_column"] - 1] != config.opening_balances[code]:
            raise ValueError("displayed opening does not match the declared boundary input")
        comparisons += 1
    for day_index, area in enumerate(matrix["date_ranges"]):
        date = area["report_date"]
        positions = {matrix["row_keys"][row - 1]: row for row in range(area["start_row"], area["end_row"] + 1)}
        if len(positions) != area["end_row"] - area["start_row"] + 1:
            raise ValueError("duplicate financial row key")
        required = {"DETAIL:" + item for item in DETAIL} | {"MANDIRI:net:" + item for item in (*GROUPS, "Total")} | {
            "DETAIL:purchase_count", "DETAIL:expected_fee", "DETAIL:reversal_count",
            "CASH:cash:RUNNING TOTAL", "CASH:cash:MANDIRI", "CASH:cash:SELISIH"}
        if set(positions) != required:
            raise ValueError("financial template category coverage mismatch")
        for code in config.cell_codes:
            totals = indexed[(date, code)]
            wanted = {}
            for category in DETAIL:
                credit, debit, _ = totals.get(category, [0, 0, 0])
                balances[code] += credit - debit
                wanted["DETAIL:" + category] = (credit, debit, balances[code])
            count = totals.get("PEMBELIAN RECHARGE OUT CLUSTER", [0, 0, 0])[2]
            wanted["DETAIL:purchase_count"] = (count, "", "")
            wanted["DETAIL:expected_fee"] = ("", count * 200, "")
            wanted["DETAIL:reversal_count"] = (totals.get("REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER", [0, 0, 0])[2], "", "")
            nets = {name: sum(totals.get(key, [0, 0, 0])[0] - totals.get(key, [0, 0, 0])[1] for key in keys)
                    for name, keys in GROUPS.items()}
            for name, amount in nets.items():
                wanted["MANDIRI:net:" + name] = (amount, "", "")
            total = sum(nets.values())
            wanted["MANDIRI:net:Total"] = (total, "", "")
            running[code] = total if type(bank[code]) is int and bank[code] > 0 else running[code] + total
            bank[code] = (indexed[(dates[day_index + 1], code)].get("CASHOUT APOLLO", [0, 0, 0])[1]
                          if day_index + 1 < len(dates) else "PENDING NEXT DAY")
            bank[code] = manual_inputs.get((date, code), bank[code])
            difference = bank[code] - running[code] if type(bank[code]) is int and bank[code] != 0 else ""
            status = ("PENDING TRANSFER" if difference == "" else "SESUAI" if difference == 0
                      else "LEBIH BAYAR" if difference > 0 else "KURANG BAYAR")
            wanted["CASH:cash:RUNNING TOTAL"] = (running[code], "", "")
            wanted["CASH:cash:MANDIRI"] = (bank[code], "", "")
            wanted["CASH:cash:SELISIH"] = (difference, status, "")
            column = blocks[code]["start_column"] - 1
            for key, expected in wanted.items():
                actual = tuple(matrix["numeric_preview"][positions[key] - 1][column:column + 3])
                if actual != expected:
                    # Deliberately omit amounts/identifiers from exception/log output.
                    raise ValueError(f"independent numeric mismatch: {key}")
                comparisons += 3
            entry = next(item for item in matrix["editable_inputs"] if (item["report_date"], item["cell_code"]) == (date, code))
            if entry["column"] != column + 1 or matrix["row_keys"][entry["row"] - 1] != "CASH:cash:MANDIRI":
                raise ValueError("editable MANDIRI input moved outside its approved DEBET cell")
            if day_index + 1 < len(dates):
                following = matrix["date_ranges"][day_index + 1]
                target = next(row for row in range(following["start_row"], following["end_row"] + 1)
                              if matrix["row_keys"][row - 1] == "DETAIL:CASHOUT APOLLO")
                index, letters = column + 2, ""
                while index:
                    index, remainder = divmod(index - 1, 26)
                    letters = chr(65 + remainder) + letters
                expected_default = f"={letters}{target}"
            else:
                expected_default = "PENDING NEXT DAY"
            if entry["default"] != expected_default:
                raise ValueError("bank default formula does not reference next-day KREDIT")
            if (date, code) not in manual_inputs and matrix["values"][entry["row"] - 1][column] != expected_default:
                raise ValueError("automatic bank formula differs from the independently derived reference")
    return {"cell_days": len(indexed), "eligible_rows_aggregated": checked_rows,
            "numeric_cells_compared": comparisons, "mismatches": 0, "declared_manual_bank_inputs": len(manual_inputs),
            "opening_balances_finance_approved": False, "bank_statement_validated": False,
            "family_dispositions_independently_classified": False,
            "october_sales_hold_policy": "invoice-only; excluded from cash-flow per approved contract"}


def main():
    import argparse
    import json
    from pathlib import Path
    import psycopg
    from finpay_pipeline.cell_workflow import read_cell_configuration_v2
    from finpay_pipeline.cell_publication_v2 import build_cell_cluster_matrices_v2
    from finpay_pipeline.tests.run_cell_live_design_e2e import private_root, sha, dump
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--database-password-file", type=Path)
    parser.add_argument("--published", action="store_true")
    args = parser.parse_args()
    private_root(args.root)
    if sha(args.root / "configuration.json") != args.configuration_sha256:
        raise ValueError("audit configuration pin mismatch")
    runtime, configs, _ = read_cell_configuration_v2(args.root / "configuration.json",
        args.configuration_sha256, args.configuration_sha256)
    scope = audit_scope(runtime, args.root, args.configuration_sha256)
    if args.database_password_file is not None:
        scope["password_file"] = args.database_password_file
    if not scope["password_file"].is_file():
        raise ValueError("explicit isolated database password file is unavailable")
    schema, workbook = scope["schema"], scope["workbook"]
    from psycopg import sql
    table = lambda name: sql.Identifier(schema, name)
    connection = psycopg.connect(host=scope["host"], port=scope["port"], dbname=scope["database"],
        user=scope["user"], password=scope["password_file"].read_text().strip())
    try:
        from psycopg import sql
        table = lambda name: sql.Identifier(schema, name)
        raw = connection.execute(sql.SQL("SELECT l.raw_row FROM {} c JOIN {} l ON l.generation_id=c.generation_id").format(
            table("finpay_cell_current_v1"), table("finpay_cell_ledger_v1"))).fetchall()
        balance_errors = negatives = holds = 0
        for (row,) in raw:
            opening, credit, debit, closing = [row[name] for name in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir")]
            if any(type(value) is not int for value in (opening, credit, debit, closing)):
                raise ValueError("raw ledger audit requires exact integers")
            balance_errors += closing != opening + credit - debit
            negatives += credit < 0 or debit < 0
            holds += str(row.get("Transaction", "")).strip().upper() == "SELLTHRUSALESHOLD"
        if ((scope["full_backfill"] and len(raw) != scope["expected_raw_rows"]) or balance_errors or negatives):
            raise ValueError("raw ledger completeness/arithmetic gate failed")
        rows = connection.execute(sql.SQL("SELECT c.cluster_id,c.cell_code,c.report_date,c.generation_id,c.calculation_id,"
            "v.config_id,v.result FROM {} c JOIN {} v ON c.calculation_id=v.calculation_id "
            "ORDER BY c.cluster_id,c.report_date,c.cell_code").format(
                table("finpay_cell_current_v1"), table("finpay_cell_calculations_v1"))).fetchall()
        if len(rows) != scope["expected_cell_days"]:
            raise ValueError("audit requires all 54 immutable cell-days")
        facts = defaultdict(list)
        for cluster, code, date, generation, calculation, config_id, result in rows:
            config = configs[cluster + "_B"]
            identity = next(item.to_dict() for item in config.cells if item.cell_code == code)
            facts[config.account].append({"identity": identity, "report_date": str(date), "generation_id": generation,
                "calculation_id": calculation, "config_id": config_id, "rules_version": config.rules_version,
                "calculation": result})
        audits = {}
        for account, config in sorted(configs.items()):
            if args.published:
                saved = connection.execute(sql.SQL("SELECT snapshot FROM {} WHERE cluster_id=%s AND target_id=%s "
                    "AND status='COMPLETE' ORDER BY epoch DESC LIMIT 1").format(table("finpay_cell_publications_v1")),
                    (account[:-2], workbook)).fetchone()
                if not saved:
                    raise ValueError("missing complete publication for financial audit")
                matrix = saved[0]["worksheets"]["summary"]["matrix"]
            else:
                matrix = build_cell_cluster_matrices_v2(config, facts[account], "2026-10-16")["summary"]
            manual = {(item["report_date"], item["cell_code"]): matrix["values"][item["row"] - 1][item["column"] - 1]
                for item in matrix["editable_inputs"]
                if type(matrix["values"][item["row"] - 1][item["column"] - 1]) is int} if args.published else {}
            audits[account] = audit_summary(config, facts[account], matrix, manual_inputs=manual)
        report = {"status": "PASS", "mode": "published" if args.published else "candidate",
            "raw_rows_checked": len(raw), "raw_balance_equation_mismatches": balance_errors,
            "negative_source_amounts": negatives, "raw_october_hold_rows": holds, "accounts": audits,
            "finance_release": "BLOCKED: test openings, bank evidence, continuity and real Finance identity sign-off pending",
            "timestamp_continuity_independently_verified": False}
        dump(args.root / ("summary-financial-audit-published.json" if args.published else "summary-financial-audit-candidate.json"), report)
        print(json.dumps({"status": "PASS", "accounts": len(audits), "raw_rows": len(raw),
            "cell_days": sum(item["cell_days"] for item in audits.values()),
            "numeric_cells_compared": sum(item["numeric_cells_compared"] for item in audits.values()),
            "mismatches": 0, "finance_release": "NOT APPROVED"}, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
