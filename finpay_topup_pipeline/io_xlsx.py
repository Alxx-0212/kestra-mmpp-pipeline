import openpyxl

from .io_csv import parse_amount, parse_transaction_date
from .legacy_outlet import row_outlet_candidates


def _header_map(headers):
    m = {}
    for i, h in enumerate(headers):
        if h is None:
            continue
        key = str(h).strip().lower()
        m[key] = i
    return m


def _cell(row, idx):
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def parse_morowali_xlsx(path, default_cluster_id="MOROWALI"):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    txns = []
    openings = []
    errors = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = rows[0]
        hm = _header_map(header)
        date_i = hm.get("transaction date")
        sender_i = hm.get("sender")
        receiver_i = hm.get("receiver")
        type_i = hm.get("transaction type")
        remarks_i = hm.get("remarks")
        cluster_i = hm.get("cluster")
        outlet_i = next(
            (hm.get(name) for name in ("outlet", "outlet name", "location", "lokasi", "lokasi outlet") if name in hm),
            None,
        )
        saldo_i = hm.get("saldo")
        setor_i = hm.get("setor")
        topup_i = hm.get("topup")
        kredit_i = hm.get("kredit")
        debit_i = hm.get("debit")
        for row_number, r in enumerate(rows[1:], 2):
            if r is None:
                continue
            try:
                tdate = parse_transaction_date(_cell(r, date_i))
            except (TypeError, ValueError) as exc:
                errors.append({
                    "sheet": ws.title,
                    "row": row_number,
                    "error": str(exc),
                    "value": str(_cell(r, date_i)),
                })
                continue
            ttype = (str(_cell(r, type_i) or "")).strip()
            canonical_type = {"debit": "Debit", "kredit": "Kredit"}.get(ttype.lower())
            saldo_raw = _cell(r, saldo_i)
            amount = None
            if setor_i is not None or topup_i is not None:
                if ttype.lower() == "debit":
                    amount = parse_amount(_cell(r, topup_i))
                elif ttype.lower() == "kredit":
                    amount = parse_amount(_cell(r, setor_i))
            if amount is None and (kredit_i is not None or debit_i is not None):
                if ttype.lower() == "kredit":
                    amount = parse_amount(_cell(r, kredit_i))
                elif ttype.lower() == "debit":
                    amount = parse_amount(_cell(r, debit_i))
            cluster_id = (str(_cell(r, cluster_i)).strip() if _cell(r, cluster_i) else None) or default_cluster_id
            remarks = (str(_cell(r, remarks_i)).strip() if _cell(r, remarks_i) else None)
            outlet_candidates = row_outlet_candidates(r, remarks_i, cluster_id, outlet_i)
            outlet_labels = {candidate[0] for candidate in outlet_candidates}
            outlet_label = next(iter(outlet_labels)) if len(outlet_labels) == 1 else None
            outlet_confidence = (
                "exact"
                if outlet_label and all(candidate[1] == "exact" for candidate in outlet_candidates)
                else "derived"
                if outlet_label
                else None
            )
            saldo_val = parse_amount(saldo_raw)
            if amount is not None and canonical_type is not None:
                txns.append({
                    "cluster_id": cluster_id,
                    "transaction_date": tdate,
                    "sender": (str(_cell(r, sender_i)).strip() if _cell(r, sender_i) else None),
                    "receiver": (str(_cell(r, receiver_i)).strip() if _cell(r, receiver_i) else None),
                    "transaction_type": canonical_type,
                    "amount": amount,
                    "currency": "IDR",
                    "remarks": remarks,
                    "saldo": saldo_val,
                    "outlet_label": outlet_label,
                    "outlet_raw": outlet_candidates[0][2] if len(outlet_candidates) == 1 else None,
                    "outlet_confidence": outlet_confidence,
                    "outlet_ambiguous": sorted(outlet_labels) if len(outlet_labels) > 1 else [],
                })
            elif saldo_val is not None and tdate is not None and amount is None:
                openings.append({
                    "cluster_id": cluster_id,
                    "as_of_date": tdate.date(),
                    "opening_balance": saldo_val,
                    "note": remarks,
                })
    return {"txns": txns, "openings": openings, "errors": errors}
