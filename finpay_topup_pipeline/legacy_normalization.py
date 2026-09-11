from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import (
    TABLE_BALANCE,
    TABLE_CLASS,
    TABLE_CLUSTER_OUTLET,
    TABLE_OUTLET,
    TABLE_TXN,
)
from .io_xlsx import parse_morowali_xlsx
from .legacy_outlet import outlet_code
from .loader import row_hash
from .schema import ensure_schema


LOCAL_TZ = ZoneInfo("Asia/Makassar")

LEGACY_WORKBOOKS = {
    "411311": "FINPAY PALANGKARAYA.xlsx",
    "421306": "FINPAY MOROTAI.xlsx",
    "421307": "FINPAY TIDORE.xlsx",
    "421315": "FINPAY BANGGAI.xlsx",
    "421318": "FINPAY MOROWALI.xlsx",
    "421320": "FINPAY TERNATE.xlsx",
}

# These are conservative last-complete-day candidates from the workbook audit.
# The existing latest checkpoint always wins when it is newer.
LEGACY_CHECKPOINT_CANDIDATES = {
    "411311": (date(2026, 9, 4), Decimal("73626600")),
    "421306": (date(2026, 9, 4), Decimal("54076000")),
    "421307": (date(2026, 9, 4), Decimal("39133701")),
    "421315": (date(2026, 8, 11), Decimal("36302500")),
    "421318": (date(2026, 9, 4), Decimal("62562200")),
    "421320": (date(2026, 9, 4), Decimal("38061838")),
}


def _local_date(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(LOCAL_TZ)
    return value.date() if hasattr(value, "date") else value


def apply_legacy_workbooks(conn, data_dir, *, source_label="legacy_workbook"):
    """Load pre-checkpoint legacy rows and high-confidence outlet classifications."""
    ensure_schema(conn)
    data_dir = Path(data_dir)
    report = {
        "transactions_seen": 0,
        "transactions_inserted": 0,
        "classifications_inserted": 0,
        "outlets_inserted": 0,
        "mappings_inserted": 0,
        "ambiguous_rows": 0,
        "parse_errors": 0,
        "checkpoint_updates": 0,
        "clusters": {},
    }

    for cluster_id, filename in LEGACY_WORKBOOKS.items():
        path = data_dir / filename
        parsed = parse_morowali_xlsx(path, cluster_id)
        checkpoint_date, checkpoint_balance = LEGACY_CHECKPOINT_CANDIDATES[cluster_id]
        eligible = [
            row for row in parsed["txns"]
            if _local_date(row.get("transaction_date")) is not None
            and _local_date(row["transaction_date"]) < checkpoint_date
        ]
        report["transactions_seen"] += len(eligible)
        report["parse_errors"] += len(parsed.get("errors", []))
        labels_by_hash = defaultdict(set)
        for row in parsed["txns"]:
            if row.get("outlet_label") and not row.get("outlet_ambiguous"):
                labels_by_hash[row_hash(cluster_id, row)].add(row["outlet_label"])

        outlet_stats = Counter()
        with conn.cursor() as cur:
            for row in parsed["txns"]:
                label = row.get("outlet_label")
                if not label:
                    continue
                code = outlet_code(cluster_id, label)
                cur.execute(
                    f"INSERT INTO {TABLE_OUTLET} (code, label) VALUES (%s, %s) "
                    "ON CONFLICT (code) DO NOTHING RETURNING code",
                    (code, label),
                )
                report["outlets_inserted"] += int(cur.fetchone() is not None)
                cur.execute(
                    f"INSERT INTO {TABLE_CLUSTER_OUTLET} "
                    "(cluster_id, outlet_code, source_label, source_file, confidence) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (cluster_id, outlet_code) DO NOTHING "
                    "RETURNING cluster_id",
                    (cluster_id, code, label, str(path), row.get("outlet_confidence") or "exact"),
                )
                report["mappings_inserted"] += int(cur.fetchone() is not None)
                outlet_stats[label] += 1

            for row in parsed["txns"]:
                digest = row_hash(cluster_id, row)
                if _local_date(row["transaction_date"]) < checkpoint_date:
                    cur.execute(
                        f"INSERT INTO {TABLE_TXN} "
                        "(cluster_id, transaction_date, sender, receiver, transaction_type, amount, currency, remarks, source_file, row_hash) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (row_hash) DO NOTHING",
                        (
                            cluster_id,
                            row["transaction_date"],
                            row.get("sender"),
                            row.get("receiver"),
                            row["transaction_type"],
                            row["amount"],
                            row.get("currency") or "IDR",
                            row.get("remarks"),
                            str(path),
                            digest,
                        ),
                    )
                    report["transactions_inserted"] += int(cur.rowcount > 0)
                labels = labels_by_hash.get(digest, set())
                if row["transaction_type"] != "Kredit":
                    continue
                if len(labels) != 1:
                    if row.get("outlet_ambiguous") or len(labels) > 1:
                        report["ambiguous_rows"] += 1
                    continue
                label = next(iter(labels))
                code = outlet_code(cluster_id, label)
                cur.execute(
                    f"SELECT txn_id FROM {TABLE_TXN} WHERE row_hash = %s",
                    (digest,),
                )
                txn = cur.fetchone()
                if txn is None:
                    continue
                cur.execute(
                    f"INSERT INTO {TABLE_CLASS} "
                    "(txn_id, category, note, classified_by, outlet_code, source_label, classification_source, classification_confidence) "
                    "VALUES (%s,NULL,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (txn_id) DO UPDATE SET outlet_code=COALESCE(finpay_topup_classification.outlet_code, EXCLUDED.outlet_code), "
                    "source_label=COALESCE(finpay_topup_classification.source_label, EXCLUDED.source_label), "
                    "classification_source=COALESCE(finpay_topup_classification.classification_source, EXCLUDED.classification_source), "
                    "classification_confidence=COALESCE(finpay_topup_classification.classification_confidence, EXCLUDED.classification_confidence), "
                    "classified_at=now() WHERE finpay_topup_classification.outlet_code IS NULL",
                    (
                        txn[0],
                        f"{source_label}: {row.get('outlet_raw') or label}",
                        source_label,
                        code,
                        row.get("outlet_raw") or label,
                        source_label,
                        row.get("outlet_confidence") or "exact",
                    ),
                )
                report["classifications_inserted"] += int(cur.rowcount > 0)

            for opening in parsed["openings"]:
                if opening["as_of_date"] < checkpoint_date:
                    cur.execute(
                        f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (cluster_id, as_of_date) DO NOTHING",
                        (cluster_id, opening["as_of_date"], opening["opening_balance"], opening.get("note")),
                    )

            cur.execute(
                f"SELECT max(as_of_date) FROM {TABLE_BALANCE} WHERE cluster_id = %s",
                (cluster_id,),
            )
            latest = cur.fetchone()[0]
            if latest is None or latest < checkpoint_date:
                cur.execute(
                    f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (cluster_id, as_of_date) DO UPDATE SET opening_balance=EXCLUDED.opening_balance",
                    (
                        cluster_id,
                        checkpoint_date,
                        checkpoint_balance,
                        f"{source_label}: last complete-looking date {checkpoint_date - date.resolution}",
                    ),
                )
                report["checkpoint_updates"] += int(cur.rowcount > 0)
        report["clusters"][cluster_id] = {
            "eligible_rows": len(eligible),
            "outlets": dict(sorted(outlet_stats.items())),
            "candidate_checkpoint": checkpoint_date.isoformat(),
            "candidate_opening_balance": str(checkpoint_balance),
            "parse_errors": parsed.get("errors", []),
        }
    conn.commit()
    return report
