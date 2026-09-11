"""Pure helpers for the classification bot: callback contract, auth, rendering.

This module must stay importable without python-telegram-bot or psycopg so the
repo's host-side unittest suites can exercise the public callback contract.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo


BOT_COMMANDS = (
    ("start", "Buka panel FinPay"),
    ("topup", "Jalankan penyegaran Top Up"),
    ("status", "Lihat saldo dan status"),
    ("kredit", "Tinjau dan tandai Kredit"),
    ("kreditlist", "Lihat daftar Kredit"),
    ("topupexcel", "Unduh analisis Top Up Excel"),
    ("help", "Bantuan singkat"),
)


CLUSTER_USERS = {
    "411311": "411311_A",
    "421306": "421306_A",
    "421307": "421307_A",
    "421315": "421315_A",
    "421318": "421318_A",
    "421320": "421320_A",
}
CLUSTER_LABELS = {
    "411311": "Palangkaraya",
    "421306": "Morotai",
    "421307": "Tidore",
    "421315": "Banggai",
    "421318": "Morowali",
    "421320": "Ternate",
}


CALLBACK_MAX_BYTES = 64
WALKTHROUGH_WINDOW_DAYS = 7
KREDIT_REPORT_PAGE_SIZE = 20
OUTLET_BUTTONS_PER_ROW = 3
LOCAL_TZ = ZoneInfo("Asia/Makassar")

APPROVE_LABEL = "✅ Setujui & masukkan"
REJECT_LABEL = "❌ Tolak & hapus"
DETAILS_LABEL = "ℹ️ Lihat detail"

SKIP_CALLBACK = "n:skip"
DONE_CALLBACK = "n:done"
SKIP_LABEL = "Lewati"
DONE_LABEL = "Selesai"

REFUSE_TEXT = "Gunakan tombol menu. Pilih Tinjau Kredit untuk melihat transaksi yang belum ditandai."
HELP_TEXT = (
    "FINPAY\n"
    "Gunakan menu untuk memilih tindakan.\n\n"
    "/topup - penyegaran data Top Up\n"
    "/status - saldo dan status refresh\n"
    "/kredit - tinjau dan tandai Kredit\n"
    "/kreditlist - lihat semua Kredit dan outlet\n"
    "/topupexcel - unduh Excel Top Up\n"
    "/refresh cluster <id> | /refresh all - perintah lanjutan\n\n"
    "Pada Tinjau Kredit, pilih outlet untuk menandai setiap transaksi. "
    "Lewati menunda transaksi untuk ditinjau kembali."
)
USAGE_TEXT = (
    "Format: /kredit [1|3|7|all|custom YYYY-MM-DD YYYY-MM-DD] | "
    "/kredit cluster <id> [1|3|7|all|custom YYYY-MM-DD YYYY-MM-DD]"
)
REFRESH_USAGE = "Format: /refresh cluster <id> | /refresh all"
STATUS_USAGE = "Format: /status all | /status cluster <id>"
UNKNOWN_CLUSTER_TEXT = "Wilayah tidak dikenal."
HOME_TEXT = "FINPAY\nPilih tindakan yang ingin dilakukan."
TOPUP_MENU_TEXT = (
    "TOP-UP\n"
    "Pilih wilayah yang ingin diperbarui.\n"
    "Semua wilayah memakai tanggal checkpoint masing-masing."
)
STATUS_MENU_TEXT = (
    "STATUS\n"
    "Pilih wilayah untuk melihat saldo, checkpoint, dan status refresh."
)
KREDIT_MENU_TEXT = "TINJAU KREDIT\nPilih wilayah dan rentang data."
KREDIT_REPORT_MENU_TEXT = "DAFTAR KREDIT\nPilih wilayah."
KREDIT_REPORT_PERIOD_TEXT = "DAFTAR KREDIT\nPilih rentang data."
TOPUP_EXPORT_MENU_TEXT = "EXCEL TOP-UP\nPilih wilayah."
TOPUP_EXPORT_PERIOD_TEXT = "EXCEL TOP-UP\nPilih rentang data."
TOPUP_EXPORT_USAGE = (
    "Format: /topupexcel [1|3|7|custom YYYY-MM-DD YYYY-MM-DD] | "
    "/topupexcel all [1|3|7|custom YYYY-MM-DD YYYY-MM-DD] | "
    "/topupexcel cluster <id> [1|3|7|custom YYYY-MM-DD YYYY-MM-DD]"
)
ID_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


class UpdateIdCache:
    """Bounded update_id dedupe. Losing it on restart is safe: DB writes are idempotent."""

    def __init__(self, capacity=4096):
        self.capacity = capacity
        self._seen = OrderedDict()

    def seen(self, update_id):
        """Record update_id and return True when it was already processed."""
        if update_id in self._seen:
            self._seen.move_to_end(update_id)
            return True
        self._seen[update_id] = True
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return False


def is_allowed(chat_id, user_id, allowed_chat_ids, allowed_user_ids):
    """Fail closed unless both whitelists are configured and match."""
    if not allowed_chat_ids or not allowed_user_ids:
        return False
    return chat_id in allowed_chat_ids and user_id in allowed_user_ids


def cluster_label(cluster_id):
    return CLUSTER_LABELS.get(str(cluster_id), f"Cluster {cluster_id}")


def format_idr(value):
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


def format_date_label(value):
    if not value:
        return "-"
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)[:10]
    return f"{parsed.day} {ID_MONTHS[parsed.month]} {parsed.year}"


def format_date_range(start, end):
    if not start and not end:
        return "Semua tanggal"
    start_label = format_date_label(start)
    end_label = format_date_label(end)
    return start_label if start_label == end_label else f"{start_label} - {end_label}"


def format_datetime_label(value):
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
    return f"{format_date_label(parsed.date())} {parsed:%H:%M:%S}"


def format_state(value):
    return {
        "REQUESTED": "Diterima",
        "DOWNLOADED": "Data diunduh",
        "STAGED": "Menunggu verifikasi",
        "PENDING_REVIEW": "Menunggu persetujuan",
        "VERIFIED": "Terverifikasi",
        "MISMATCH": "Ada selisih",
        "COMMITTED": "Berhasil",
        "REJECTED": "Ditolak",
        "VERIFY_FAILED": "Gagal diverifikasi",
        "FAILED": "Gagal",
        "VERIFY_SKIPPED": "Verifikasi dilewati",
    }.get(value, str(value or "Tidak diketahui").replace("_", " ").title())


def build_home_keyboard():
    return [
        [("Top Up", "m:topup"), ("Status", "m:status")],
        [("Tinjau Kredit", "m:kredit"), ("Daftar Kredit", "m:kreditlist")],
        [("Excel Top Up", "m:topupexcel"), ("Bantuan", "m:help")],
    ]


def build_topup_keyboard():
    return _cluster_scope_keyboard("t")


def build_status_keyboard():
    return _cluster_scope_keyboard("s")


def _cluster_scope_keyboard(prefix):
    buttons = [
        (cluster_label(cluster_id), f"{prefix}:c:{cluster_id}")
        for cluster_id in CLUSTER_USERS
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.extend([
        [("Semua wilayah", f"{prefix}:a")],
        [("Kembali", "m:home")],
    ])
    return rows


def build_kredit_keyboard():
    rows = [[(cluster_label(cluster_id), f"k:c:{cluster_id}:m")] for cluster_id in CLUSTER_USERS]
    rows.extend([
        [("Semua wilayah", "k:a:m")],
        [("Kembali", "m:home")],
    ])
    return rows


def _kredit_period_keyboard(prefix, back_callback):
    return [
        [("1 hari", f"{prefix}:1"), ("3 hari", f"{prefix}:3")],
        [("7 hari", f"{prefix}:7"), ("Semua", f"{prefix}:a")],
        [("Rentang custom", f"{prefix}:x")],
        [("Pilih wilayah", back_callback)],
    ]


def build_kredit_period_keyboard(cluster_id=None):
    prefix = f"k:c:{cluster_id}" if cluster_id else "k:a"
    return _kredit_period_keyboard(prefix, "m:kredit")


def build_kredit_report_keyboard():
    rows = [[(cluster_label(cluster_id), f"l:c:{cluster_id}:m")] for cluster_id in CLUSTER_USERS]
    rows.extend([
        [("Semua wilayah", "l:a:m")],
        [("Kembali", "m:home")],
    ])
    return rows


def build_kredit_report_period_keyboard(cluster_id=None):
    prefix = f"l:c:{cluster_id}" if cluster_id else "l:a"
    return _kredit_period_keyboard(prefix, "m:kreditlist")


def build_kredit_report_page_keyboard(cluster_id, page, has_more):
    prefix = f"l:c:{cluster_id}" if cluster_id else "l:a"
    navigation = []
    if page > 0:
        navigation.append(("Sebelumnya", f"{prefix}:p:{page - 1}"))
    if has_more:
        navigation.append(("Berikutnya", f"{prefix}:p:{page + 1}"))
    rows = [navigation] if navigation else []
    rows.append([("Pilih ulang", "m:kreditlist")])
    return rows


def build_topup_export_keyboard():
    rows = [[(cluster_label(cluster_id), f"e:c:{cluster_id}:m")] for cluster_id in CLUSTER_USERS]
    rows.extend([
        [("Semua wilayah", "e:a:m")],
        [("Kembali", "m:home")],
    ])
    return rows


def build_topup_export_period_keyboard(cluster_id=None):
    prefix = f"e:c:{cluster_id}" if cluster_id else "e:a"
    return [
        [("1 hari", f"{prefix}:1"), ("3 hari", f"{prefix}:3")],
        [("7 hari", f"{prefix}:7"), ("Rentang custom", f"{prefix}:x")],
        [("Pilih wilayah", "m:topupexcel")],
    ]


def build_review_keyboard(refresh_id):
    return [
        [(APPROVE_LABEL, f"r:a:{refresh_id}"), (REJECT_LABEL, f"r:r:{refresh_id}")],
        [(DETAILS_LABEL, f"r:d:{refresh_id}")],
    ]


def make_classify_callback(seq, txn_id, outlet):
    if not isinstance(outlet, str) or not outlet or ":" in outlet:
        raise ValueError("outlet code is not valid callback data")
    data = f"c:{seq}:{txn_id}:{outlet}"
    if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError(f"callback_data exceeds Telegram {CALLBACK_MAX_BYTES}-byte limit: {data!r}")
    return data


def parse_callback_data(data):
    """Parse the plan's callback contract.

    ``c:<seq>:<txn_id>:<outlet>`` -> {"action": "classify", ...}
    ``n:skip`` / ``n:done``      -> {"action": "skip"|"done"}
    ``l:<scope>:<window>``       -> read-only Kredit report
    ``r:a:<refresh_id>`` / ``r:r:<refresh_id>`` -> review decisions
    Anything else returns None and must be answered as unknown.
    """
    if not data:
        return None
    parts = data.split(":")
    if data in {"m:home", "m:topup", "m:status", "m:kredit", "m:kreditlist", "m:topupexcel", "m:help"}:
        return {"action": "menu", "target": data[2:]}
    if parts[0] in {"t", "s"}:
        if parts[1:] == ["a"]:
            return {"action": "topup_scope" if parts[0] == "t" else "status_scope", "scope": "all"}
        if len(parts) == 3 and parts[1] == "c" and parts[2] in CLUSTER_USERS:
            return {
                "action": "topup_scope" if parts[0] == "t" else "status_scope",
                "scope": "cluster",
                "cluster_id": parts[2],
            }
        return None
    if parts[0] == "k":
        if len(parts) == 3 and parts[1] == "a" and parts[2] == "m":
            return {"action": "kredit_period_menu", "scope": "all"}
        if len(parts) == 3 and parts[1] == "a" and parts[2] in {"1", "3", "7", "a", "x"}:
            return {"action": "kredit_scope", "scope": "all", "window": parts[2]}
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] == "m":
            return {
                "action": "kredit_period_menu",
                "scope": "cluster",
                "cluster_id": parts[2],
            }
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] in {"1", "3", "7", "a", "x"}:
            return {
                "action": "kredit_scope",
                "scope": "cluster",
                "cluster_id": parts[2],
                "window": parts[3],
            }
        return None
    if parts[0] == "l":
        if len(parts) == 3 and parts[1] == "a" and parts[2] == "m":
            return {"action": "kredit_report_period_menu", "scope": "all"}
        if len(parts) == 3 and parts[1] == "a" and parts[2] in {"1", "3", "7", "a", "x"}:
            return {"action": "kredit_report_scope", "scope": "all", "window": parts[2]}
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] == "m":
            return {
                "action": "kredit_report_period_menu",
                "scope": "cluster",
                "cluster_id": parts[2],
            }
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] in {"1", "3", "7", "a", "x"}:
            return {
                "action": "kredit_report_scope",
                "scope": "cluster",
                "cluster_id": parts[2],
                "window": parts[3],
            }
        if len(parts) == 4 and parts[1] == "a" and parts[2] == "p" and parts[3].isdigit():
            page = int(parts[3])
            if 0 <= page <= 10000:
                return {"action": "kredit_report_page", "scope": "all", "page": page}
        if len(parts) == 5 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] == "p" and parts[4].isdigit():
            page = int(parts[4])
            if 0 <= page <= 10000:
                return {
                    "action": "kredit_report_page",
                    "scope": "cluster",
                    "cluster_id": parts[2],
                    "page": page,
                }
        return None
    if parts[0] == "e":
        if len(parts) == 3 and parts[1] == "a" and parts[2] == "m":
            return {"action": "topup_export_period_menu", "scope": "all"}
        if len(parts) == 3 and parts[1] == "a" and parts[2] in {"1", "3", "7", "x"}:
            return {"action": "topup_export_scope", "scope": "all", "window": parts[2]}
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] == "m":
            return {
                "action": "topup_export_period_menu",
                "scope": "cluster",
                "cluster_id": parts[2],
            }
        if len(parts) == 4 and parts[1] == "c" and parts[2] in CLUSTER_USERS and parts[3] in {"1", "3", "7", "x"}:
            return {
                "action": "topup_export_scope",
                "scope": "cluster",
                "cluster_id": parts[2],
                "window": parts[3],
            }
        return None
    if parts[0] == "c" and len(parts) == 4:
        try:
            seq = int(parts[1])
            txn_id = int(parts[2])
        except ValueError:
            return None
        outlet = parts[3]
        if seq < 0 or txn_id <= 0 or not outlet or ":" in outlet:
            return None
        return {"action": "classify", "seq": seq, "txn_id": txn_id, "outlet": outlet}
    if parts[0] == "n" and len(parts) == 2:
        if parts[1] == "skip":
            return {"action": "skip"}
        if parts[1] == "done":
            return {"action": "done"}
    if parts[0] == "r" and len(parts) == 3 and parts[1] in {"a", "r", "d", "s"}:
        if not parts[2].isdigit():
            return None
        refresh_id = int(parts[2])
        if refresh_id <= 0 or len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
            return None
        if parts[1] in {"d", "s"}:
            return {"action": "details" if parts[1] == "d" else "summary", "refresh_id": refresh_id}
        return {
            "action": "approve" if parts[1] == "a" else "reject",
            "refresh_id": refresh_id,
        }
    return None


def build_outlet_keyboard_buttons(outlets, seq, txn_id):
    """Rows of (label, callback_data) tuples: outlets first, then Skip/Done."""
    buttons = []
    for outlet in outlets:
        buttons.append(
            (outlet["label"], make_classify_callback(seq, txn_id, outlet["code"]))
        )
    rows = [buttons[i:i + OUTLET_BUTTONS_PER_ROW] for i in range(0, len(buttons), OUTLET_BUTTONS_PER_ROW)]
    rows.append([(SKIP_LABEL, SKIP_CALLBACK), (DONE_LABEL, DONE_CALLBACK)])
    return rows


def format_txn_card(txn, remaining=None, progress=None):
    lines = [
        "TINJAU KREDIT",
        f"{cluster_label(txn['cluster_id'])} ({txn['cluster_id']})",
        f"Transaksi #{txn['txn_id']}",
        f"Tanggal: {format_date_label(txn['transaction_date'])}",
        f"Nilai: {format_idr(txn['amount'])}",
    ]
    remarks = (txn.get("remarks") or "").strip()
    if remarks:
        lines.append(f"Keterangan: {remarks}")
    if progress is not None:
        lines.append(f"Progress: {progress}")
    elif remaining is not None:
        lines.append(f"Tersisa setelah ini: {remaining}")
    lines.append("Pilih outlet:")
    return "\n".join(lines)


def format_assignment(txn, outlet_label):
    return f"Transaksi #{txn['txn_id']} ditandai sebagai {outlet_label}."


def format_nothing_to_classify(start_date, end_date):
    return f"Tidak ada Kredit yang belum ditandai pada {format_date_range(start_date, end_date)}."


def format_kredit_report_custom_prompt(cluster_id=None, command="/kreditlist"):
    scope = f"cluster {cluster_id}" if cluster_id else "all"
    return (
        "Rentang custom Kredit:\n"
        f"{command} {scope} custom YYYY-MM-DD YYYY-MM-DD"
    )


def format_topup_export_custom_prompt(cluster_id=None):
    scope = f"cluster {cluster_id}" if cluster_id else "all"
    return (
        "Rentang custom Excel Top Up:\n"
        f"/topupexcel {scope} custom YYYY-MM-DD YYYY-MM-DD"
    )


def format_kredit_report(rows, cluster_id, start_date, end_date, page, has_more):
    scope = cluster_label(cluster_id) if cluster_id else "Semua wilayah"
    lines = [
        "DAFTAR KREDIT",
        f"Wilayah: {scope}",
        f"Periode: {format_date_range(start_date, end_date)}",
        f"Halaman: {page + 1}",
    ]
    for row in rows:
        outlet = row.get("outlet_label") or row.get("outlet_code") or "Belum ditandai"
        lines.append(
            f"#{row['txn_id']} | {cluster_label(row['cluster_id'])} | "
            f"{format_date_label(row['transaction_date'])} | "
            f"{format_idr(row['amount'])} | {outlet}"
        )
    if not rows:
        lines.append("Tidak ada Kredit pada rentang ini.")
    if has_more:
        lines.append("Gunakan Berikutnya untuk melihat data selanjutnya.")
    return "\n".join(lines)


def format_walkthrough_done(remaining):
    tail = (
        "Semua transaksi pada rentang ini sudah ditangani."
        if remaining == 0
        else f"{remaining} Kredit masih belum ditandai pada rentang ini."
    )
    return f"Tinjau Kredit selesai. {tail}"


def format_kredit_done(assigned, deferred, remaining):
    return (
        "TINJAU KREDIT SELESAI\n"
        f"Ditandai: {assigned}\n"
        f"Ditunda: {deferred}\n"
        f"Belum ditandai: {remaining}"
    )


def format_status(unclassified_total, unclassified_recent, refresh):
    lines = [
        f"Kredit belum ditandai: {unclassified_total} total, {unclassified_recent} dalam 7 hari.",
    ]
    if refresh:
        lines.append(
            f"Refresh Top Up terakhir: {format_state(refresh['status'])} "
            f"({refresh['requested_start']}..{refresh['requested_end']}, "
            f"refresh {refresh['refresh_id']})."
        )
    else:
        lines.append("Belum ada refresh Top Up.")
    return "\n".join(lines)


def format_status_cluster(cluster_id, username, unclassified_total, unclassified_recent, refresh):
    """Render one cluster block without transaction rows."""
    lines = [
        f"{cluster_label(cluster_id)} ({cluster_id})",
        f"Pengguna DigiPOS: {username}",
        f"Kredit belum ditandai: {unclassified_total} total, {unclassified_recent} dalam 7 hari.",
    ]
    if not refresh:
        lines.append("Belum ada refresh Top Up.")
        return "\n".join(lines)

    lifecycle = refresh.get("lifecycle_status", refresh.get("status", "UNKNOWN"))
    verification = refresh.get(
        "verification_status",
        refresh.get("cluster_status", "UNKNOWN"),
    )
    period_start = refresh.get("checkpoint_as_of_date") or refresh.get("requested_start")
    lifecycle_label = (
        "Berhasil masuk"
        if verification == "COMMITTED"
        else "Menunggu keputusan"
        if lifecycle == "PENDING_REVIEW"
        else format_state(lifecycle)
    )
    lines.append(f"Refresh: {lifecycle_label} | #{refresh.get('refresh_id')}.")
    lines.append(
        f"Periode wilayah: {format_date_range(period_start, refresh.get('requested_end'))}."
    )
    if refresh.get("trigger_source"):
        lines.append(f"Pemicu: {refresh['trigger_source']}.")
    lines.append(f"Verifikasi: {format_state(verification)}.")
    if refresh.get("checkpoint_as_of_date"):
        lines.append(f"Checkpoint: {format_date_label(refresh['checkpoint_as_of_date'])}.")
    if refresh.get("staged_count"):
        lines.append(f"Menunggu persetujuan: {refresh['staged_count']} baris.")
    return "\n".join(lines)


def format_status_all(blocks):
    """Render one compact aggregate block per allowed cluster."""
    return "\n\n".join(blocks)


def default_window(today=None):
    today = today or date.today()
    return (
        (today - timedelta(days=WALKTHROUGH_WINDOW_DAYS - 1)).isoformat(),
        today.isoformat(),
    )


def parse_kredit_args(args, today=None):
    """Return (start_iso, end_iso, error). start/end None means open window ('all')."""
    today = today or date.today()
    if not args:
        return (*default_window(today), None)
    if len(args) == 1:
        token = args[0].lower()
        if token == "all":
            return None, None, None
        if token in {"1", "3", "7"}:
            end = today
            start = end - timedelta(days=int(token) - 1)
            return start.isoformat(), end.isoformat(), None
    if len(args) == 3 and args[0].lower() == "custom":
        return parse_kredit_args(args[1:], today)
    if len(args) == 2:
        try:
            start_d = date.fromisoformat(args[0])
            end_d = date.fromisoformat(args[1])
        except ValueError:
            return None, None, "Tanggal harus berformat ISO YYYY-MM-DD."
        if start_d > end_d:
            return None, None, "Tanggal mulai harus sebelum atau sama dengan tanggal akhir."
        if end_d > today:
            return None, None, "Tanggal tidak boleh di masa depan."
        return start_d.isoformat(), end_d.isoformat(), None
    return None, None, USAGE_TEXT


def parse_status_args(args):
    """Return ``(cluster_id, error)``; ``None`` cluster means all clusters."""
    args = list(args or [])
    if not args or (len(args) == 1 and args[0].lower() == "all"):
        return None, None
    if len(args) == 2 and args[0].lower() == "cluster":
        cluster_id = args[1]
        if cluster_id not in CLUSTER_USERS:
            return None, UNKNOWN_CLUSTER_TEXT
        return cluster_id, None
    return None, STATUS_USAGE


def parse_kredit_command_args(args, today=None):
    """Return ``(cluster_id, start_iso, end_iso, error)`` for `/kredit`."""
    args = list(args or [])
    today = today or date.today()
    if args and args[0].lower() == "cluster":
        if len(args) < 2:
            return None, None, None, USAGE_TEXT
        cluster_id = args[1]
        if cluster_id not in CLUSTER_USERS:
            return None, None, None, UNKNOWN_CLUSTER_TEXT
        scoped_args = args[2:]
        if not scoped_args:
            scoped_args = []
        elif len(scoped_args) == 1 and scoped_args[0].lower() == "all":
            return cluster_id, None, None, None
        elif len(scoped_args) not in {1, 2, 3}:
            return None, None, None, USAGE_TEXT
        start, end, error = parse_kredit_args(scoped_args, today)
        return cluster_id, start, end, error

    if args and args[0].lower() == "all" and len(args) > 1:
        start, end, error = parse_kredit_args(args[1:], today)
        return None, start, end, error

    start, end, error = parse_kredit_args(args, today)
    return None, start, end, error


def parse_refresh_args(args, today=None):
    """Reject the removed public date-window refresh route."""
    return None, None, REFRESH_USAGE


def parse_refresh_command_args(args, today=None):
    """Return ``(scope, None, None, error)`` for checkpoint-driven refreshes."""
    args = list(args or [])
    if len(args) == 1 and args[0].lower() == "all":
        return {"mode": "AUTO_ALL", "cluster_id": None, "user": None}, None, None, None
    if args and args[0].lower() == "cluster":
        if len(args) != 2:
            return None, None, None, REFRESH_USAGE
        cluster_id = args[1]
        if cluster_id not in CLUSTER_USERS:
            return None, None, None, UNKNOWN_CLUSTER_TEXT
        return {
            "mode": "AUTO_CLUSTER",
            "cluster_id": cluster_id,
            "user": CLUSTER_USERS[cluster_id],
        }, None, None, None
    return None, None, None, REFRESH_USAGE


def format_refresh_queued(
    execution_id,
    start,
    end,
    execution_url,
    requested_by=None,
    cluster_id=None,
    checkpoint_dates=None,
):
    """Plain-text success reply; a raw URL goes on its own line to auto-link."""
    lines = [
        "TOP-UP REFRESH",
        f"{cluster_label(cluster_id) if cluster_id else 'Semua wilayah'}",
        f"Status: Berjalan",
        "Catatan: data belum masuk ledger utama sebelum disetujui.",
    ]
    checkpoint_dates = checkpoint_dates or {}
    if cluster_id is None and len(checkpoint_dates) > 1:
        lines.append("Tanggal mulai per wilayah:")
        for current_cluster in CLUSTER_USERS:
            checkpoint = checkpoint_dates.get(current_cluster)
            if checkpoint:
                lines.append(f"- {cluster_label(current_cluster)}: {format_date_label(checkpoint)}")
        lines.append(f"Sampai: {format_date_label(end)}")
    else:
        lines.append(f"Periode: {format_date_range(start, end)}")
    lines.append(f"ID eksekusi: {execution_id}")
    if execution_url:
        lines.append(execution_url)
    return "\n".join(lines)


def format_refresh_busy(seconds_remaining, in_flight=False):
    seconds_remaining = max(int(seconds_remaining), 0)
    source = (
        "Refresh sedang berjalan di chat ini."
        if in_flight
        else "Refresh baru saja diterima."
    )
    return (
        f"{source} Coba lagi sekitar "
        f"{seconds_remaining // 60} menit {seconds_remaining % 60:02d} detik."
    )


def format_refresh_rejected(reason):
    """Generic refusal; never render Kestra response bodies or internal URLs."""
    return f"Refresh ditolak: {reason}"


def format_refresh_pending():
    return (
        "Refresh sebelumnya masih menunggu persetujuan. "
        "Setujui atau tolak refresh tersebut sebelum memulai yang baru."
    )


def format_refresh_unconfigured():
    return (
        "Pemicu Kestra belum dikonfigurasi. Minta operator memeriksa pengaturan bot."
    )


def format_review_result(action, result):
    """Render only the safe decision result returned by the review domain."""
    refresh_id = result.get("refresh_id")
    status = result.get("status")
    if status == "NOT_FOUND":
        return f"Refresh {refresh_id} tidak ditemukan."
    if status == "COMMITTED":
        if result.get("idempotent"):
            return f"Refresh {refresh_id} sudah disetujui dan masuk ledger utama."
        return (
            f"Refresh {refresh_id} disetujui. "
            f"{result.get('inserted', 0)} transaksi masuk ledger utama."
        )
    if status == "PENDING_REVIEW" and result.get("partial"):
        held = ", ".join(cluster_label(cluster_id) for cluster_id in result.get("held_clusters", []))
        return (
            f"Refresh {refresh_id} disetujui sebagian. "
            f"{result.get('inserted', 0)} transaksi dari wilayah terverifikasi masuk ledger utama. "
            f"Tetap menunggu pemeriksaan: {held or 'wilayah yang belum terverifikasi'}. "
            "Datanya tetap di staging."
        )
    if status == "PENDING_REVIEW" and result.get("decision") == "held_for_review":
        held = ", ".join(cluster_label(cluster_id) for cluster_id in result.get("held_clusters", []))
        return (
            f"Refresh {refresh_id} belum dapat dimasukkan. "
            f"Tetap di staging untuk diperiksa: {held or 'wilayah yang belum terverifikasi'}."
        )
    if status == "REJECTED":
        if result.get("idempotent"):
            return f"Refresh {refresh_id} sudah ditolak."
        return (
            f"Refresh {refresh_id} ditolak. "
            f"{result.get('purged', 0)} baris dihapus. Ledger utama tidak berubah."
        )
    return (
        f"Refresh {refresh_id} tidak menunggu persetujuan "
        f"(status: {status or 'tidak diketahui'}). Tidak ada perubahan."
    )


def format_review_error():
    return "Tindakan persetujuan gagal. Tidak ada perubahan."


def format_refresh_details(refresh):
    mode = {
        "AUTO_CLUSTER": "Otomatis per wilayah",
        "AUTO_ALL": "Otomatis semua wilayah",
        "FULL": "Penuh",
    }.get(refresh.get("refresh_mode"), refresh.get("refresh_mode") or "-")
    code = lambda value: f"<code>{escape(str(value), quote=True)}</code>"
    field = lambda label, value: f"<b>{escape(label)}:</b> {code(value)}"
    clusters = refresh.get("clusters", [])
    partially_committed = (
        refresh.get("status") == "PENDING_REVIEW"
        and any(cluster.get("status") == "COMMITTED" for cluster in clusters)
        and any(cluster.get("status") != "COMMITTED" for cluster in clusters)
    )
    status_label = (
        "Sebagian masuk, wilayah lain ditahan"
        if partially_committed
        else format_state(refresh.get("status"))
    )
    lines = [
        "<b>DETAIL TOP-UP</b>",
        "<code>--------------------</code>",
        field("Refresh", f"#{refresh.get('refresh_id')}"),
        field("Status", status_label),
        field("Mode", mode),
        field("Periode", format_date_range(refresh.get("requested_start"), refresh.get("requested_end"))),
    ]
    if partially_committed:
        lines.append(
            "<b>Hanya wilayah Terverifikasi yang masuk ledger utama; wilayah lain tetap di staging.</b>"
        )
    if refresh.get("kestra_execution_id"):
        lines.append(field("Eksekusi", refresh["kestra_execution_id"]))
    if refresh.get("requested_by"):
        lines.append(field("Diminta oleh", refresh["requested_by"]))
    if refresh.get("trigger_source"):
        lines.append(field("Pemicu", refresh["trigger_source"]))
    if refresh.get("reviewed_by"):
        lines.append(field("Ditinjau oleh", refresh["reviewed_by"]))
    if refresh.get("reviewed_at"):
        lines.append(field("Waktu tinjau", format_datetime_label(refresh["reviewed_at"])))
    for cluster in clusters:
        cluster_lines = [
            "",
            f"<b>{escape(cluster_label(cluster.get('cluster_id')))}</b> {code(cluster.get('cluster_id'))}",
            field("Status", format_state(cluster.get("status"))),
            field("Checkpoint", format_date_label(cluster.get("checkpoint_as_of_date"))),
            field("Data diunduh", f"{cluster.get('source_row_count') or 0} baris"),
            field("Baris dihitung", cluster.get("calculation_row_count") or 0),
            field("Saldo", format_idr(cluster.get("computed_saldo"))),
            field("CMS BUCKET", format_idr(cluster.get("bucket_topup_value"))),
            field("Selisih", format_idr(cluster.get("verification_diff"))),
            field("Masuk ledger", cluster.get("loaded_inserted") or 0),
            field(
                "Sumber",
                f"{format_datetime_label(cluster.get('source_transaction_min_at'))} - "
                f"{format_datetime_label(cluster.get('source_transaction_max_at'))}",
            ),
            field("CMS diambil", format_datetime_label(cluster.get("bucket_snapshot_at"))),
            field("Batas kalkulasi", format_datetime_label(cluster.get("calculation_cutoff_at"))),
            field("Dihitung", format_datetime_label(cluster.get("calculated_at"))),
        ]
        if cluster.get("checkpoint_as_of_date"):
            cluster_lines.insert(
                4,
                field(
                    "Periode wilayah",
                    format_date_range(
                        cluster.get("checkpoint_as_of_date"),
                        refresh.get("requested_end"),
                    ),
                ),
            )
        lines.extend(cluster_lines)
        match_fields = (
            "calculation_row_count",
            "matching_row_number",
            "matching_type",
            "matching_transaction_id",
            "matching_transaction_at",
            "matching_running_saldo",
        )
        match_type = cluster.get("matching_type")
        if not any(cluster.get(field) is not None for field in match_fields):
            lines.append(field("Bukti kecocokan", "Belum direkam pada refresh ini"))
        elif match_type == "TRANSACTION":
            lines.extend([
                field(
                    "Saldo cocok pada baris",
                    f"{cluster.get('matching_row_number')} dari {cluster.get('calculation_row_count') or 0}",
                ),
                field("ID transaksi cocok", cluster.get("matching_transaction_id")),
                field("Waktu transaksi cocok", format_datetime_label(cluster.get("matching_transaction_at"))),
                field("Running saldo cocok", format_idr(cluster.get("matching_running_saldo"))),
            ])
        elif match_type == "CHECKPOINT":
            lines.append(field("Saldo cocok", "Checkpoint sebelum baris 1"))
        elif match_type == "CUTOFF":
            lines.append(field("Saldo cocok", "Batas perhitungan CMS"))
        elif cluster.get("status") == "MISMATCH":
            lines.append("<b>Tidak ada baris yang cocok.</b>")
    return "\n".join(lines)
