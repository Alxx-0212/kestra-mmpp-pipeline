import hashlib
from decimal import Decimal, InvalidOperation
from datetime import datetime

import csv


TRANSACTION_TYPES = {"Kredit", "Debit"}
CSV_COLUMNS = {
    "No",
    "Transaction Date",
    "Sender",
    "Receiver",
    "Transaction Type",
    "Amount",
    "Currency",
    "Remarks",
}


def parse_amount(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        digits = "".join(ch for ch in s if ch.isdigit() or ch == ".")
        return Decimal(digits) if digits else None


def parse_transaction_date(raw):
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return datetime.strptime(digits[:10], "%Y%m%d%H")
    raise ValueError(f"Unparseable transaction date: {raw!r}")


def parse_topup_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = CSV_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Top Up CSV missing columns: {sorted(missing)}")
        for line in reader:
            ttype_raw = (line.get("Transaction Type") or "").strip()
            amount = parse_amount(line.get("Amount"))
            if ttype_raw == "" and amount is None:
                continue
            if ttype_raw not in TRANSACTION_TYPES:
                raise ValueError(f"Unsupported transaction type: {ttype_raw!r}")
            if amount is None:
                raise ValueError(f"Missing amount for transaction type {ttype_raw!r}")
            transaction_date = parse_transaction_date(line.get("Transaction Date"))
            if transaction_date is None:
                raise ValueError(f"Missing transaction date for transaction type {ttype_raw!r}")
            rows.append({
                "transaction_date": transaction_date,
                "sender": (line.get("Sender") or "").strip() or None,
                "receiver": (line.get("Receiver") or "").strip() or None,
                "transaction_type": ttype_raw,
                "amount": amount,
                "currency": (line.get("Currency") or "IDR").strip() or None,
                "remarks": (line.get("Remarks") or "").strip() or None,
            })
    return rows
