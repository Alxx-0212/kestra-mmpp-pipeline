"""One-way Telegram review and failure alerts for the top-up pipeline.

Alerting is best-effort: a missing token/chat or Telegram outage must never
fail a pipeline cycle, so callers wrap ``send_telegram_alert`` and only log.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_MESSAGE_TIMEOUT = 30
TELEGRAM_MESSAGE_LIMIT = 4096
MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
LOCAL_TZ = ZoneInfo("Asia/Makassar")
CLUSTER_LABELS = {
    "411311": "Palangkaraya",
    "421306": "Morotai",
    "421307": "Tidore",
    "421315": "Banggai",
    "421318": "Morowali",
    "421320": "Ternate",
}
APPROVE_LABEL = "✅ Setujui & masukkan"
REJECT_LABEL = "❌ Tolak & hapus"
DETAILS_LABEL = "ℹ️ Lihat detail"


def send_telegram_message(
    token,
    chat_id,
    text,
    timeout=SEND_MESSAGE_TIMEOUT,
    api_base=TELEGRAM_API_BASE,
    reply_markup=None,
    parse_mode=None,
):
    """POST one sendMessage. Returns True when sent, False when not configured.

    Raises on HTTP/network errors so callers can decide how loudly to fail.
    Callers that select HTML are responsible for passing escaped content.
    """
    if not token or not chat_id:
        return False
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError("Telegram message exceeds 4096 characters")
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    response = requests.post(
        f"{api_base.rstrip('/')}/bot{token}/sendMessage",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return True


def send_telegram_alert(text, token=None, chat_id=None, reply_markup=None, parse_mode=None):
    """Send using explicit values or TELEGRAM_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID env."""
    return send_telegram_message(
        token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN"),
        chat_id if chat_id is not None else os.environ.get("TELEGRAM_ALERT_CHAT_ID"),
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


def _html(value):
    return escape(str(value), quote=True)


def _code(value):
    return f"<code>{_html(value)}</code>"


def _field(label, value):
    return f"<b>{_html(label)}:</b> {_code(value)}"


def _matching_row_block(row, running_saldo, value_limit=None):
    if not row:
        return None
    def value(key):
        raw = str(row.get(key) or "-")
        if value_limit is not None:
            return raw[:value_limit] + ("..." if len(raw) > value_limit else "")
        return raw

    values = [
        f"ID: {row.get('txn_id') or '-'}",
        f"Waktu: {_format_datetime(row.get('transaction_date'))}",
        f"Cluster: {row.get('cluster_id') or '-'}",
        f"Tipe: {row.get('transaction_type') or '-'}",
        f"Nilai: {_format_idr(row.get('amount'))}",
        f"Running saldo: {_format_idr(running_saldo)}",
        f"Pengirim: {value('sender')}",
        f"Penerima: {value('receiver')}",
        f"Mata uang: {value('currency')}",
        f"Keterangan: {value('remarks')}",
        f"Sumber: {value('source_file')}",
        f"Hash: {value('row_hash')}",
    ]
    return f"<pre>{escape(chr(10).join(values), quote=True)}</pre>"


def _cluster_lines(clusters, row_value_limit=None, checkpoint_dates=None, end=None):
    lines = []
    for cluster_id in sorted(clusters):
        c = clusters[cluster_id]
        checkpoint = c.get("checkpoint_as_of_date") or (checkpoint_dates or {}).get(cluster_id)
        cluster_lines = [
            "",
            f"<b>{_html(_cluster_label(cluster_id))}</b> {_code(cluster_id)}",
            _field("Status", _status_label(c.get("status"))),
            _field("Data diunduh", f"{c.get('downloaded_rows', c.get('source_row_count', 0)) or 0} baris"),
            _field("Baris dihitung", c.get("calculation_row_count") or 0),
            _field("Saldo berjalan", _format_idr(c.get("computed_saldo"))),
            _field("CMS BUCKET", _format_idr(c.get("bucket_topup_value"))),
            _field("Selisih", _format_idr(c.get("verification_diff"))),
        ]
        if checkpoint and end:
            cluster_lines.insert(2, _field("Periode wilayah", _format_date_range(checkpoint, end)))
        elif checkpoint:
            cluster_lines.insert(2, _field("Checkpoint", _format_date(checkpoint)))
        lines.extend(cluster_lines)
        if c.get("error"):
            lines.append(_field("Catatan", c["error"]))
        if c.get("staged_rows") is not None:
            lines.append(_field("Baris ditahan", c["staged_rows"]))
        if c.get("source_transaction_max_at"):
            lines.append(_field("Transaksi terakhir dihitung", _format_datetime(c["source_transaction_max_at"])))
        if c.get("bucket_snapshot_at"):
            lines.append(_field("CMS diambil", _format_datetime(c["bucket_snapshot_at"])))
        if c.get("calculation_cutoff_at"):
            lines.append(_field("Cocok dengan CMS pada", _format_datetime(c["calculation_cutoff_at"])))

        match_type = c.get("matching_type")
        row_number = c.get("matching_row_number")
        row_count = c.get("calculation_row_count") or 0
        if match_type == "TRANSACTION":
            lines.append(_field("Saldo cocok pada baris", f"{row_number} dari {row_count}"))
            block = _matching_row_block(
                c.get("matching_row"),
                c.get("matching_running_saldo"),
                value_limit=row_value_limit,
            )
            if block:
                lines.extend(["<b>Transaksi yang cocok</b>", block])
        elif match_type == "CHECKPOINT":
            lines.append(_field("Saldo cocok", "Checkpoint sebelum baris 1"))
        elif match_type == "CUTOFF":
            lines.append(_field("Saldo cocok", "Batas perhitungan CMS"))
        elif c.get("status") == "MISMATCH":
            lines.append("<b>Tidak ada baris yang cocok.</b>")
    return lines


def _cluster_label(cluster_id):
    return CLUSTER_LABELS.get(str(cluster_id), f"Cluster {cluster_id}")


def _format_idr(value):
    if value is None:
        return "-"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return f"Rp {value}"
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    if formatted.endswith(",00"):
        formatted = formatted[:-3]
    return f"Rp {formatted}"


def _format_date(value):
    if not value:
        return "-"
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)[:10]
    return f"{parsed.day} {MONTHS[parsed.month]} {parsed.year}"


def _format_date_range(start, end):
    start_text = _format_date(start)
    end_text = _format_date(end)
    return start_text if start_text == end_text else f"{start_text} - {end_text}"


def _format_datetime(value):
    if not value:
        return "-"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(LOCAL_TZ)
    except (TypeError, ValueError):
        return str(value)
    return f"{_format_date(parsed.date())} {parsed:%H:%M:%S}"


def _status_label(status):
    return {
        "VERIFIED": "Menunggu persetujuan",
        "MISMATCH": "Selisih perlu ditinjau",
        "VERIFY_FAILED": "Gagal diverifikasi",
        "SKIPPED": "Verifikasi dilewati",
    }.get(status, status.replace("_", " ").title() if status else "Tidak diketahui")


def format_cycle_alert(
    refresh_id,
    start,
    end,
    verification,
    committed=None,
    purged=None,
    attempt=None,
    staged=None,
    checkpoint_dates=None,
):
    """Human-readable one-way alert for a stage-then-verify cycle outcome."""
    status = verification.get("status", "UNKNOWN")
    clusters = verification.get("clusters") or {}
    partial_verification_failure = status == "VERIFY_FAILED" and any(
        c.get("status") == "VERIFIED" for c in clusters.values()
    )
    pending = status in {"VERIFIED", "MISMATCH", "PENDING_REVIEW"} or partial_verification_failure
    status_text = (
        "Sebagian terverifikasi; wilayah lain ditahan"
        if partial_verification_failure
        else _status_label(status)
    )
    scope = _cluster_label(next(iter(clusters))) if len(clusters) == 1 else "Semua wilayah"
    per_cluster_dates = len(clusters) > 1 and (
        bool(checkpoint_dates)
        or any(c.get("checkpoint_as_of_date") for c in clusters.values())
    )
    period = "Berbeda per wilayah" if per_cluster_dates else _format_date_range(start, end)
    lines = [
        "<b>FINPAY TOP-UP</b>",
        "<code>--------------------</code>",
        _field("Refresh", f"#{refresh_id}"),
        _field("Wilayah", scope),
        _field("Periode unduhan", period),
        _field("Status", status_text),
    ]
    if pending:
        lines.extend(_cluster_lines(clusters, checkpoint_dates=checkpoint_dates, end=end))
        if staged is not None and len(clusters) > 1:
            lines.extend(["", _field("Total baris ditahan", staged)])
        if status == "MISMATCH":
            lines.append(
                "<b>Perhatian: ada selisih dengan CMS. Wilayah berselisih tetap ditahan; "
                "hanya wilayah Terverifikasi yang masuk ledger utama.</b>"
            )
        elif partial_verification_failure:
            lines.append(
                "<b>Perhatian: ada wilayah yang belum terverifikasi. Hanya wilayah "
                "Terverifikasi yang masuk ledger utama.</b>"
            )
        lines.append("<b>Data belum masuk ledger utama.</b>")
    elif status == "VERIFY_FAILED":
        if purged is not None:
            lines.append(_field("Baris dihapus", purged))
        lines.append("<b>Ledger utama tidak berubah.</b>")
    elif status == "VERIFIED":
        inserted = (committed or {}).get("inserted")
        if inserted is not None:
            lines.append(_field("Masuk ledger utama", f"{inserted} transaksi"))
        lines.append("Refresh selesai.")
    else:
        lines.append("Tidak ada perubahan.")
    text = "\n".join(lines)
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    compact_limit = max(24, TELEGRAM_MESSAGE_LIMIT // max(1, len(clusters)) // 8)
    lines = [
        "<b>FINPAY TOP-UP</b>",
        "<code>--------------------</code>",
        _field("Refresh", f"#{refresh_id}"),
        _field("Wilayah", scope),
        _field("Periode unduhan", period),
        _field("Status", status_text),
    ]
    if pending:
        lines.extend(
            _cluster_lines(
                clusters,
                row_value_limit=compact_limit,
                checkpoint_dates=checkpoint_dates,
                end=end,
            )
        )
        if staged is not None and len(clusters) > 1:
            lines.extend(["", _field("Total baris ditahan", staged)])
        if status == "MISMATCH":
            lines.append(
                "<b>Perhatian: ada selisih dengan CMS. Wilayah berselisih tetap ditahan; "
                "hanya wilayah Terverifikasi yang masuk ledger utama.</b>"
            )
        elif partial_verification_failure:
            lines.append(
                "<b>Perhatian: ada wilayah yang belum terverifikasi. Hanya wilayah "
                "Terverifikasi yang masuk ledger utama.</b>"
            )
        lines.append("<b>Data belum masuk ledger utama.</b>")
    elif status == "VERIFY_FAILED":
        if purged is not None:
            lines.append(_field("Baris dihapus", purged))
        lines.append("<b>Ledger utama tidak berubah.</b>")
    else:
        lines.append("Tidak ada perubahan.")
    compact_text = "\n".join(lines)
    if len(compact_text) <= TELEGRAM_MESSAGE_LIMIT:
        return compact_text
    fallback = [
        "<b>FINPAY TOP-UP</b>",
        "<code>--------------------</code>",
        _field("Refresh", f"#{refresh_id}"),
        _field("Wilayah", scope),
        _field("Periode unduhan", period),
        _field("Status", status_text),
    ]
    if per_cluster_dates:
        for cluster_id in sorted(clusters):
            c = clusters[cluster_id]
            checkpoint = c.get("checkpoint_as_of_date") or (checkpoint_dates or {}).get(cluster_id)
            fallback.append(
                f"<b>{_html(_cluster_label(cluster_id))}</b>: "
                f"{_code(_format_date_range(checkpoint, end))}"
            )
    fallback.append("<b>Detail transaksi tidak ditampilkan karena melebihi batas pesan Telegram.</b>")
    return "\n".join(fallback)


def review_keyboard(refresh_id):
    """Telegram Bot API inline keyboard for a pending refresh decision."""
    return {
        "inline_keyboard": [[
            {"text": APPROVE_LABEL, "callback_data": f"r:a:{refresh_id}"},
            {"text": REJECT_LABEL, "callback_data": f"r:r:{refresh_id}"},
        ], [
            {"text": DETAILS_LABEL, "callback_data": f"r:d:{refresh_id}"},
        ]]
    }


def format_flow_failure_alert(execution_id, namespace, flow_id):
    return "\n".join([
        "<b>FINPAY TOP-UP</b>",
        "<code>--------------------</code>",
        _field("Status", "Gagal"),
        "<b>Tidak ada transaksi yang masuk ledger utama.</b>",
        _field("Eksekusi", execution_id),
        _field("Alur", f"{namespace}.{flow_id}"),
    ])
