from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from .config import TABLE_MANUAL_ADJUSTMENT
from .saldo import _checkpoint_source_sql


LOCAL_TZ = ZoneInfo("Asia/Makassar")
MAX_EXPORT_DAYS = 366
MAX_EXPORT_ROWS = 200_000
MAX_TELEGRAM_FILE_BYTES = 50 * 1024 * 1024
RUPIAH_FORMAT = '_-"Rp"* #,##0_-;\\-"Rp"* #,##0_-;_-"Rp"* "-"??_-;_-@_-'
HEADERS = (
    "Transaction Date",
    "Sender",
    "Receiver",
    "Transaction Type",
    "SETOR",
    "TOPUP",
    "SALDO",
    "Currency",
    "Remarks",
    "Outlet",
    "Source",
)
CLUSTER_SHEETS = {
    "411311": "Palangkaraya",
    "421306": "Morotai",
    "421307": "Tidore",
    "421315": "Banggai",
    "421318": "Morowali",
    "421320": "Ternate",
}


def _as_date(value):
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _local_date(value):
    return value.astimezone(LOCAL_TZ).date() if value.tzinfo is not None else value.date()


def _excel_datetime(value):
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None) if value.tzinfo is not None else value


def _cluster_rows(conn, cluster_id, start_date, end_date):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT opening_balance, as_of_date FROM ({_checkpoint_source_sql()}) checkpoints "
            "WHERE cluster_id=%s AND as_of_date<=%s "
            "ORDER BY as_of_date DESC, priority DESC, override_id DESC LIMIT 1",
            (cluster_id, start_date),
        )
        checkpoint = cur.fetchone()
        if checkpoint is None:
            raise ValueError(f"checkpoint before {start_date} is missing for cluster {cluster_id}")
        opening_balance, opening_date = checkpoint
        cur.execute(
            "SELECT u.txn_id,u.transaction_date,u.sender,u.receiver,u.transaction_type,u.amount,"
            "u.currency,u.remarks,u.source_file,u.row_hash,u.source_kind,"
            "COALESCE(o.label,c.outlet_code) AS outlet_label "
            "FROM ("
            "SELECT txn_id,cluster_id,transaction_date,sender,receiver,transaction_type,amount,currency,"
            "remarks,source_file,row_hash,'DigiPOS' AS source_kind FROM finpay_topup_txn WHERE cluster_id=%(cluster_id)s "
            "UNION ALL "
            f"SELECT (-adjustment_id)::bigint,cluster_id,effective_at,sender,receiver,transaction_type,amount,currency,"
            f"remarks,'manual_adjustment',adjustment_hash,'Manual Adjustment' FROM {TABLE_MANUAL_ADJUSTMENT} "
            "WHERE cluster_id=%(cluster_id)s AND status='APPROVED'"
            ") u LEFT JOIN finpay_topup_classification c ON c.txn_id=u.txn_id AND u.source_kind='DigiPOS' "
            "LEFT JOIN finpay_outlet o ON o.code=c.outlet_code "
            "WHERE (u.transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %(opening_date)s "
            "AND (u.transaction_date AT TIME ZONE 'Asia/Makassar')::date <= %(end_date)s "
            "ORDER BY u.transaction_date ASC",
            {"cluster_id": cluster_id, "opening_date": opening_date, "end_date": end_date},
        )
        source_rows = cur.fetchall()

    saldo = Decimal(str(opening_balance))
    export_rows = []
    for row in source_rows:
        amount = Decimal(str(row[5]))
        if row[4] == "Kredit":
            saldo += amount
        elif row[4] == "Debit":
            saldo -= amount
        if _local_date(row[1]) < start_date:
            continue
        export_rows.append({
            "transaction_date": row[1],
            "sender": row[2],
            "receiver": row[3],
            "transaction_type": row[4],
            "debit": amount if row[4] == "Debit" else None,
            "kredit": amount if row[4] == "Kredit" else None,
            "saldo": saldo,
            "currency": row[6] or "IDR",
            "remarks": row[7],
            "outlet": row[11],
            "source": row[10],
        })
    return export_rows


def build_finance_workbook(conn, start_date, end_date, cluster_ids):
    """Build a finance-style, read-only Top-Up workbook in memory."""
    start_date = _as_date(start_date)
    end_date = _as_date(end_date)
    if start_date > end_date:
        raise ValueError("start date must be on or before end date")
    if (end_date - start_date).days >= MAX_EXPORT_DAYS:
        raise ValueError(f"date range may not exceed {MAX_EXPORT_DAYS} days")
    cluster_ids = list(dict.fromkeys(str(cluster_id) for cluster_id in cluster_ids))
    if not cluster_ids or any(cluster_id not in CLUSTER_SHEETS for cluster_id in cluster_ids):
        raise ValueError("export cluster is not configured")

    workbook = Workbook()
    workbook.remove(workbook.active)
    counts = {}
    total_rows = 0
    for cluster_id in cluster_ids:
        rows = _cluster_rows(conn, cluster_id, start_date, end_date)
        total_rows += len(rows)
        if total_rows > MAX_EXPORT_ROWS:
            raise ValueError(f"export exceeds {MAX_EXPORT_ROWS:,} rows")
        counts[cluster_id] = len(rows)
        sheet = workbook.create_sheet(CLUSTER_SHEETS[cluster_id])
        sheet.append(HEADERS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        for row in rows:
            sheet.append((
                _excel_datetime(row["transaction_date"]),
                row["sender"],
                row["receiver"],
                row["transaction_type"],
                row["kredit"],
                row["debit"],
                row["saldo"],
                row["currency"],
                row["remarks"],
                row["outlet"],
                row["source"],
            ))
        for cell in sheet["A"][1:]:
            cell.number_format = "dd-mm-yyyy hh:mm:ss"
        for column in ("E", "F", "G"):
            for cell in sheet[column][1:]:
                cell.number_format = RUPIAH_FORMAT
        widths = (20, 18, 18, 18, 16, 16, 18, 12, 32, 20, 20)
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + index)].width = width
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:K{max(sheet.max_row, 1)}"

    output = BytesIO()
    workbook.save(output)
    content = output.getvalue()
    if len(content) > MAX_TELEGRAM_FILE_BYTES:
        raise ValueError("generated workbook exceeds Telegram's 50 MB document limit")
    scope = cluster_ids[0] if len(cluster_ids) == 1 else "all"
    return {
        "content": content,
        "filename": f"finpay-topup-{scope}-{start_date.isoformat()}-to-{end_date.isoformat()}.xlsx",
        "row_count": total_rows,
        "cluster_counts": counts,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
    }
