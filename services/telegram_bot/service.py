"""Telegram update handling for the Kredit classification walkthrough.

Every turn re-derives the remaining transactions from Postgres; the only
in-memory state is convenience (skip-set, current seq) that may be lost on
restart without corrupting data.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update

from finpay_topup_pipeline.refresh_service import (
    execution_id_from_response,
    kestra_execution_web_url,
    submit_kestra_refresh,
)

from . import store
from .callbacks import (
    HELP_TEXT,
    REFUSE_TEXT,
    UpdateIdCache,
    build_outlet_keyboard_buttons,
    build_home_keyboard,
    build_kredit_keyboard,
    build_kredit_period_keyboard,
    build_kredit_report_keyboard,
    build_kredit_report_page_keyboard,
    build_kredit_report_period_keyboard,
    build_review_keyboard,
    build_status_keyboard,
    build_topup_export_keyboard,
    build_topup_export_period_keyboard,
    build_topup_keyboard,
    CLUSTER_USERS,
    format_assignment,
    format_kredit_report,
    format_kredit_report_custom_prompt,
    format_kredit_done,
    format_nothing_to_classify,
    format_refresh_busy,
    format_refresh_queued,
    format_refresh_rejected,
    format_refresh_unconfigured,
    format_refresh_details,
    format_refresh_pending,
    format_review_error,
    format_review_result,
    format_status_all,
    format_status_cluster,
    format_topup_export_custom_prompt,
    format_txn_card,
    HOME_TEXT,
    KREDIT_MENU_TEXT,
    KREDIT_REPORT_MENU_TEXT,
    KREDIT_REPORT_PERIOD_TEXT,
    STATUS_MENU_TEXT,
    TOPUP_MENU_TEXT,
    TOPUP_EXPORT_MENU_TEXT,
    TOPUP_EXPORT_PERIOD_TEXT,
    TOPUP_EXPORT_USAGE,
    is_allowed,
    parse_callback_data,
    parse_kredit_command_args,
    parse_status_args,
    parse_refresh_command_args,
)

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Makassar")


def _buttons_to_markup(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows]
    )


class ClassificationService:
    REFRESH_HTTP_TIMEOUT = 30
    REQUESTED_BY_PATTERN = re.compile(r"[A-Za-z0-9_]{5,32}")

    def __init__(
        self,
        settings,
        connect=None,
        refresh_submitter=submit_kestra_refresh,
        monotonic=time.monotonic,
    ):
        self.settings = settings
        self._connect = connect
        self._refresh_submitter = refresh_submitter
        self._monotonic = monotonic
        self.bot = None
        self.updates = UpdateIdCache()
        self.sessions = {}
        self.report_sessions = {}
        self._refresh_in_flight: set[int] = set()
        self._last_refresh_at: dict[int, float] = {}

    def connection(self):
        if self._connect is not None:
            return closing(self._connect())
        import psycopg
        return closing(psycopg.connect(**self.settings.db_kwargs))

    async def handle_webhook_update(self, payload, bot=None):
        bot = bot or self.bot
        update = Update.de_json(payload, bot)
        if update is None:
            logger.warning("unparseable webhook payload dropped")
            return
        if update.update_id is not None and self.updates.seen(update.update_id):
            logger.info("duplicate update_id %s ignored", update.update_id)
            return
        chat = update.effective_chat
        user = update.effective_user
        chat_id = chat.id if chat else None
        user_id = user.id if user else None
        if not is_allowed(
            chat_id,
            user_id,
            self.settings.allowed_chat_ids,
            self.settings.allowed_user_ids,
        ):
            logger.warning("rejected sender chat=%s user=%s", chat_id, user_id)
            return
        try:
            if update.callback_query:
                await self._handle_callback(update.callback_query, chat_id, user)
                return
            await self._handle_message(update.effective_message, chat_id, user)
        except Exception:
            logger.exception("update %s failed", update.update_id)

    async def _run_db(self, fn, *args):
        def _with_conn():
            with self.connection() as conn:
                return fn(conn, *args)
        return await asyncio.to_thread(_with_conn)

    async def _send(self, chat_id, text, reply_markup=None):
        await self.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup
        )

    async def _send_document(self, chat_id, content, filename, caption):
        await self.bot.send_document(
            chat_id=chat_id,
            document=InputFile(content, filename=filename),
            caption=caption,
        )

    async def _reply(self, message, text):
        await message.reply_text(text)

    async def _show_home(self, chat_id):
        await self._send(chat_id, HOME_TEXT, reply_markup=_buttons_to_markup(build_home_keyboard()))

    async def _show_topup_menu(self, chat_id):
        await self._send(chat_id, TOPUP_MENU_TEXT, reply_markup=_buttons_to_markup(build_topup_keyboard()))

    async def _show_status_menu(self, chat_id):
        await self._send(chat_id, STATUS_MENU_TEXT, reply_markup=_buttons_to_markup(build_status_keyboard()))

    async def _show_kredit_menu(self, chat_id):
        await self._send(chat_id, KREDIT_MENU_TEXT, reply_markup=_buttons_to_markup(build_kredit_keyboard()))

    async def _show_kredit_report_menu(self, chat_id):
        await self._send(
            chat_id,
            KREDIT_REPORT_MENU_TEXT,
            reply_markup=_buttons_to_markup(build_kredit_report_keyboard()),
        )

    async def _show_topup_export_menu(self, chat_id):
        await self._send(
            chat_id,
            TOPUP_EXPORT_MENU_TEXT,
            reply_markup=_buttons_to_markup(build_topup_export_keyboard()),
        )

    async def _show_kredit_period_menu(self, query, cluster_id=None):
        text = f"TINJAU KREDIT\n{cluster_id or 'Semua wilayah'}\nPilih rentang data."
        await self._edit(
            query,
            text,
            reply_markup=_buttons_to_markup(build_kredit_period_keyboard(cluster_id)),
        )

    async def _edit(self, query, text, reply_markup=None, parse_mode=None):
        if query.message is None:
            await query.answer(text[:200])
            return
        kwargs = {"reply_markup": reply_markup}
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        await query.message.edit_text(text, **kwargs)

    @staticmethod
    def _classified_by(user):
        return getattr(user, "username", None) or str(getattr(user, "id", "unknown"))

    async def _handle_message(self, message, chat_id, user=None):
        text = (message.text or "").strip() if message else ""
        if not text.startswith("/"):
            await self._reply(message, REFUSE_TEXT)
            return
        command, _, arg_string = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = arg_string.split()
        if command == "/start":
            await self._show_home(chat_id)
        elif command == "/topup":
            await self._show_topup_menu(chat_id)
        elif command == "/kredit":
            if args and args[0].lower() in {"list", "report"}:
                await self._cmd_kredit_report(chat_id, args[1:])
            else:
                await (self._cmd_kredit(chat_id, args) if args else self._show_kredit_menu(chat_id))
        elif command == "/kreditlist":
            await (self._cmd_kredit_report(chat_id, args) if args else self._show_kredit_report_menu(chat_id))
        elif command == "/topupexcel":
            await (self._cmd_topup_excel(chat_id, args) if args else self._show_topup_export_menu(chat_id))
        elif command == "/status":
            await (self._cmd_status(chat_id, args) if args else self._show_status_menu(chat_id))
        elif command == "/refresh":
            await self._cmd_refresh(chat_id, user, args)
        elif command == "/help":
            await self._send(chat_id, HELP_TEXT, reply_markup=_buttons_to_markup(build_home_keyboard()))
        else:
            await self._show_home(chat_id)

    async def _cmd_kredit(self, chat_id, args):
        today = datetime.now(TZ).date()
        cluster_id, start_date, end_date, error = parse_kredit_command_args(args, today)
        if error:
            await self._send(chat_id, error)
            return

        def _load(conn):
            return (
                store.list_active_outlets(conn, cluster_id),
                store.list_unclassified(
                    conn,
                    start_date=start_date,
                    end_date=end_date,
                    cluster_id=cluster_id,
                ),
            )

        try:
            outlets, txns = await self._run_db(_load)
        except Exception:
            logger.exception("Kredit listing failed chat=%s cluster=%s", chat_id, cluster_id)
            await self._send(chat_id, "Kredit belum dapat dimuat sekarang.")
            return
        self.sessions[chat_id] = {
            "skipped": set(),
            "seq": 0,
            "start": start_date,
            "end": end_date,
            "cluster_id": cluster_id,
            "total_count": len(txns),
        }
        if not outlets:
            await self._send(chat_id, "Belum ada outlet aktif yang dikonfigurasi.")
            return
        if not txns:
            await self._send(
                chat_id,
                format_nothing_to_classify(start_date, end_date),
            )
            return
        try:
            await self._present_next(chat_id, txns)
        except Exception:
            logger.exception("Kredit card render failed chat=%s cluster=%s", chat_id, cluster_id)
            await self._send(chat_id, "Kartu Kredit belum dapat ditampilkan sekarang.")

    async def _render_kredit_report(self, chat_id, query=None):
        session = self.report_sessions.get(chat_id)
        if session is None:
            if query:
                await query.answer("Daftar sudah kedaluwarsa")
            else:
                await self._send(chat_id, "Daftar Kredit sudah kedaluwarsa.")
            return
        try:
            result = await self._run_db(
                lambda conn: store.list_kredit_page(
                    conn,
                    start_date=session["start"],
                    end_date=session["end"],
                    cluster_id=session["cluster_id"],
                    page=session["page"],
                )
            )
        except Exception:
            logger.exception(
                "Kredit report failed chat=%s cluster=%s page=%s",
                chat_id,
                session["cluster_id"],
                session["page"],
            )
            text = "Daftar Kredit belum dapat dimuat sekarang."
            if query:
                await self._edit(query, text)
            else:
                await self._send(chat_id, text)
            return
        text = format_kredit_report(
            result["rows"],
            session["cluster_id"],
            session["start"],
            session["end"],
            session["page"],
            result["has_more"],
        )
        markup = _buttons_to_markup(
            build_kredit_report_page_keyboard(
                session["cluster_id"], session["page"], result["has_more"]
            )
        )
        if query:
            await self._edit(query, text, reply_markup=markup)
        else:
            await self._send(chat_id, text, reply_markup=markup)

    async def _cmd_kredit_report(self, chat_id, args, query=None):
        today = datetime.now(TZ).date()
        cluster_id, start_date, end_date, error = parse_kredit_command_args(args, today)
        if error:
            if query:
                await self._edit(query, error)
            else:
                await self._send(chat_id, error)
            return
        self.report_sessions[chat_id] = {
            "cluster_id": cluster_id,
            "start": start_date,
            "end": end_date,
            "page": 0,
        }
        await self._render_kredit_report(chat_id, query=query)

    async def _cmd_topup_excel(self, chat_id, args, query=None):
        today = datetime.now(TZ).date()
        cluster_id, start_date, end_date, error = parse_kredit_command_args(args, today)
        if error or start_date is None or end_date is None:
            if query:
                await self._edit(query, TOPUP_EXPORT_USAGE)
            else:
                await self._send(chat_id, TOPUP_EXPORT_USAGE)
            return
        cluster_ids = [cluster_id] if cluster_id else list(CLUSTER_USERS)
        try:
            report = await self._run_db(
                lambda conn: store.build_topup_excel(
                    conn,
                    start_date,
                    end_date,
                    cluster_ids,
                )
            )
            await self._send_document(
                chat_id,
                report["content"],
                report["filename"],
                (
                    f"Excel Top Up {report['start']} - {report['end']}\n"
                    f"{report['row_count']:,} baris · {len(report['cluster_counts'])} wilayah"
                ),
            )
            if query:
                await self._edit(query, "Excel Top Up berhasil dikirim.")
        except Exception:
            logger.exception("Top Up Excel export failed chat=%s clusters=%s", chat_id, cluster_ids)
            text = "Excel Top Up belum dapat dibuat sekarang."
            if query:
                await self._edit(query, text)
            else:
                await self._send(chat_id, text)

    def _session(self, chat_id):
        return self.sessions.setdefault(
            chat_id,
            {"skipped": set(), "seq": 0, "start": None, "end": None, "total_count": 0},
        )

    async def _window_txns(self, chat_id):
        session = self.sessions.get(chat_id) or {}
        return await self._run_db(
            lambda conn: store.list_unclassified(
                conn,
                start_date=session.get("start"),
                end_date=session.get("end"),
                cluster_id=session.get("cluster_id"),
            )
        )

    def _pending(self, txns, session):
        return [t for t in txns if t["txn_id"] not in session.get("skipped", set())]

    async def _present_next(self, chat_id, txns):
        """Send one editable message per txn: the outlet keyboard card."""
        session = self._session(chat_id)
        pending = self._pending(txns, session)
        if not pending:
            deferred = len(session.get("skipped", set()))
            assigned = max(session.get("total_count", len(txns)) - len(txns), 0)
            remaining = max(len(txns) - deferred, 0)
            self.sessions.pop(chat_id, None)
            await self._send(chat_id, format_kredit_done(assigned, deferred, remaining))
            return
        txn = pending[0]
        outlets = await self._run_db(
            lambda conn: store.list_active_outlets(conn, txn["cluster_id"])
        )
        session["seq"] += 1
        session["current_txn"] = txn["txn_id"]
        session["current_cluster_id"] = txn["cluster_id"]
        assigned = max(session.get("total_count", len(txns)) - len(txns), 0)
        progress = f"{assigned + len(session.get('skipped', set())) + 1} dari {session.get('total_count', len(txns))}"
        markup = _buttons_to_markup(
            build_outlet_keyboard_buttons(outlets, session["seq"], txn["txn_id"])
        )
        await self._send(
            chat_id,
            format_txn_card(txn, progress=progress),
            reply_markup=markup,
        )

    async def _handle_callback(self, query, chat_id, user):
        parsed = parse_callback_data(query.data)
        if parsed is None:
            await query.answer("Tindakan tidak dikenal")
            return
        action = parsed["action"]

        if action == "menu":
            await query.answer()
            target = parsed["target"]
            if target == "home":
                await self._edit(query, HOME_TEXT, _buttons_to_markup(build_home_keyboard()))
            elif target == "topup":
                await self._edit(query, TOPUP_MENU_TEXT, _buttons_to_markup(build_topup_keyboard()))
            elif target == "status":
                await self._edit(query, STATUS_MENU_TEXT, _buttons_to_markup(build_status_keyboard()))
            elif target == "kredit":
                await self._edit(query, KREDIT_MENU_TEXT, _buttons_to_markup(build_kredit_keyboard()))
            elif target == "kreditlist":
                await self._edit(query, KREDIT_REPORT_MENU_TEXT, _buttons_to_markup(build_kredit_report_keyboard()))
            elif target == "topupexcel":
                await self._edit(query, TOPUP_EXPORT_MENU_TEXT, _buttons_to_markup(build_topup_export_keyboard()))
            else:
                await self._edit(query, HELP_TEXT, _buttons_to_markup(build_home_keyboard()))
            return

        if action == "kredit_period_menu":
            await query.answer()
            await self._show_kredit_period_menu(query, parsed.get("cluster_id"))
            return

        if action == "kredit_scope":
            cluster_id = parsed.get("cluster_id") if parsed["scope"] == "cluster" else None
            window = parsed["window"]
            if window == "x":
                await query.answer()
                await self._edit(
                    query,
                    format_kredit_report_custom_prompt(cluster_id, command="/kredit"),
                    reply_markup=_buttons_to_markup(build_kredit_period_keyboard(cluster_id)),
                )
                return
            await query.answer("Memuat Kredit...")
            args = []
            if cluster_id:
                args.extend(["cluster", cluster_id])
            args.append("all" if window == "a" else window)
            await self._cmd_kredit(chat_id, args)
            return

        if action == "kredit_report_period_menu":
            await query.answer()
            await self._edit(
                query,
                KREDIT_REPORT_PERIOD_TEXT,
                reply_markup=_buttons_to_markup(
                    build_kredit_report_period_keyboard(parsed.get("cluster_id"))
                ),
            )
            return

        if action == "kredit_report_scope":
            cluster_id = parsed.get("cluster_id") if parsed["scope"] == "cluster" else None
            window = parsed["window"]
            if window == "x":
                await query.answer()
                await self._edit(
                    query,
                    format_kredit_report_custom_prompt(cluster_id),
                    reply_markup=_buttons_to_markup(
                        build_kredit_report_period_keyboard(cluster_id)
                    ),
                )
                return
            await query.answer("Memuat daftar Kredit...")
            args = []
            if cluster_id:
                args.extend(["cluster", cluster_id])
            args.append("all" if window == "a" else window)
            await self._cmd_kredit_report(chat_id, args, query=query)
            return

        if action == "kredit_report_page":
            session = self.report_sessions.get(chat_id)
            expected_cluster = session.get("cluster_id") if session else None
            if session is None or expected_cluster != parsed.get("cluster_id"):
                await query.answer("Daftar sudah kedaluwarsa")
                return
            session["page"] = parsed["page"]
            await query.answer("Memuat halaman...")
            await self._render_kredit_report(chat_id, query=query)
            return

        if action == "topup_export_period_menu":
            await query.answer()
            await self._edit(
                query,
                TOPUP_EXPORT_PERIOD_TEXT,
                reply_markup=_buttons_to_markup(
                    build_topup_export_period_keyboard(parsed.get("cluster_id"))
                ),
            )
            return

        if action == "topup_export_scope":
            cluster_id = parsed.get("cluster_id") if parsed["scope"] == "cluster" else None
            if parsed["window"] == "x":
                await query.answer()
                await self._edit(
                    query,
                    format_topup_export_custom_prompt(cluster_id),
                    reply_markup=_buttons_to_markup(build_topup_export_period_keyboard(cluster_id)),
                )
                return
            await query.answer("Membuat Excel...")
            args = []
            if cluster_id:
                args.extend(["cluster", cluster_id])
            else:
                args.append("all")
            args.append(parsed["window"])
            await self._cmd_topup_excel(chat_id, args, query=query)
            return

        if action == "topup_scope":
            await query.answer("Menyusun refresh...")
            await self._cmd_refresh(
                chat_id,
                user,
                ["all"] if parsed["scope"] == "all" else ["cluster", parsed["cluster_id"]],
            )
            return

        if action == "status_scope":
            await query.answer()
            await self._cmd_status(
                chat_id,
                ["all"] if parsed["scope"] == "all" else ["cluster", parsed["cluster_id"]],
            )
            return

        if action == "details":
            await self._cmd_review_details(query, parsed["refresh_id"])
            return

        if action in {"approve", "reject"}:
            await self._cmd_review(query, chat_id, user, action, parsed["refresh_id"])
            return

        session = self.sessions.get(chat_id)

        if action == "done":
            try:
                txns = await self._window_txns(chat_id)
            except Exception:
                logger.exception("Kredit completion count failed chat=%s", chat_id)
                txns = []
            current = self.sessions.get(chat_id) or {}
            deferred = len(current.get("skipped", set()))
            assigned = max(current.get("total_count", len(txns)) - len(txns), 0)
            remaining = max(len(txns) - deferred, 0)
            self.sessions.pop(chat_id, None)
            await self._edit(query, format_kredit_done(assigned, deferred, remaining))
            await query.answer("Tinjauan selesai")
            return

        if action == "skip":
            current = (session or {}).get("current_txn")
            if current:
                self._session(chat_id)["skipped"].add(current)
            try:
                txns = await self._window_txns(chat_id)
            except Exception:
                logger.exception("Kredit skip refresh failed chat=%s", chat_id)
                await query.answer("Kartu berikutnya gagal dimuat")
                return
            await query.answer("Ditunda")
            try:
                await self._present_next(chat_id, txns)
            except Exception:
                logger.exception("Kredit card render after skip failed chat=%s", chat_id)
                await query.answer("Kartu berikutnya gagal ditampilkan")
            return

        seq = parsed["seq"]
        if session is None:
            await query.answer("Kartu sudah kedaluwarsa")
            return
        elif seq != session["seq"]:
            await query.answer("Kartu lama, gunakan kartu terbaru")
            return

        outlet_code = parsed["outlet"]
        txn_id = parsed["txn_id"]
        if txn_id != session.get("current_txn"):
            await query.answer("Kartu lama, gunakan kartu terbaru")
            return
        by = self._classified_by(user)
        try:
            outlet = await self._run_db(
                store.classify_for_cluster,
                txn_id,
                session.get("current_cluster_id") or session.get("cluster_id"),
                outlet_code,
                by,
            )
        except Exception:
            logger.exception(
                "Kredit classification failed chat=%s cluster=%s txn_id=%s",
                chat_id,
                session.get("cluster_id"),
                txn_id,
            )
            await query.answer("Penandaan gagal, tidak ada perubahan")
            return
        if outlet is None:
            await query.answer("Kartu kedaluwarsa atau sudah ditandai")
            return
        session["skipped"].discard(txn_id)
        await query.answer(f"Ditandai: {outlet['label']}")
        await self._edit(query, format_assignment({"txn_id": txn_id}, outlet["label"]))

        try:
            txns = await self._window_txns(chat_id)
        except Exception:
            logger.exception("Kredit listing after classification failed chat=%s", chat_id)
            await query.answer("Sudah ditandai, tetapi kartu berikutnya gagal dimuat")
            return
        try:
            await self._present_next(chat_id, txns)
        except Exception:
            logger.exception("Kredit card render after classification failed chat=%s", chat_id)

    async def _cmd_review(self, query, chat_id, user, action, refresh_id):
        reviewed_by = self._refresh_requested_by(user)
        operation = store.approve_refresh if action == "approve" else store.reject_refresh
        try:
            result = await self._run_db(operation, refresh_id, reviewed_by)
        except Exception:
            logger.error(
                "refresh review failed chat=%s refresh_id=%s action=%s",
                chat_id,
                refresh_id,
                action,
            )
            await query.answer("Tindakan gagal")
            await self._edit(query, format_review_error())
            return
        reply_markup = (
            _buttons_to_markup(build_review_keyboard(refresh_id))
            if result.get("status") == "PENDING_REVIEW"
            else None
        )
        await self._edit(query, format_review_result(action, result), reply_markup=reply_markup)
        await query.answer("Tindakan tersimpan")

    async def _cmd_review_details(self, query, refresh_id):
        try:
            result = await self._run_db(store.refresh_details, refresh_id)
        except Exception:
            logger.error("refresh details failed refresh_id=%s", refresh_id)
            await query.answer("Detail tidak dapat dimuat")
            return
        if result is None:
            await query.answer("Refresh tidak ditemukan")
            return
        await self._edit(
            query,
            format_refresh_details(result),
            reply_markup=_buttons_to_markup(build_review_keyboard(refresh_id)),
            parse_mode="HTML",
        )
        await query.answer("Detail dimuat")

    async def _cmd_status(self, chat_id, args=None):
        cluster_id, error = parse_status_args(args)
        if error:
            await self._send(chat_id, error)
            return

        def _load(conn):
            cluster_ids = [cluster_id] if cluster_id else sorted(CLUSTER_USERS)
            rows = []
            for current_cluster in cluster_ids:
                total, recent = store.counts(conn, cluster_id=current_cluster)
                rows.append(
                    format_status_cluster(
                        current_cluster,
                        CLUSTER_USERS[current_cluster],
                        total,
                        recent,
                        store.latest_refresh(conn, cluster_id=current_cluster),
                    )
                )
            return rows

        try:
            blocks = await self._run_db(_load)
        except Exception:
            logger.exception("status load failed chat=%s cluster=%s", chat_id, cluster_id)
            await self._send(chat_id, "Status belum dapat dimuat sekarang.")
            return
        await self._send(chat_id, format_status_all(blocks))

    @classmethod
    def _refresh_requested_by(cls, user):
        """Telegram username only when fully valid; otherwise numeric user id."""
        username = getattr(user, "username", None) or ""
        if cls.REQUESTED_BY_PATTERN.fullmatch(username):
            return username
        return str(getattr(user, "id", "unknown"))

    def _refresh_failure_detail(self, body):
        detail = " ".join(str(body or "").split())
        for value in (
            self.settings.kestra_basic_user,
            self.settings.kestra_basic_password,
        ):
            if value:
                detail = detail.replace(value, "[redacted]")
        return detail[:500]

    async def _cmd_refresh(self, chat_id, user, args):
        scope, start_iso, end_iso, error = parse_refresh_command_args(
            args,
        )
        if error:
            await self._send(chat_id, error)
            return
        if not self.settings.selective_refresh_enabled:
            await self._send(chat_id, "Penyegaran otomatis sedang dinonaktifkan.")
            return
        if not self.settings.kestra_configured:
            await self._send(chat_id, format_refresh_unconfigured())
            return
        now = self._monotonic()
        if chat_id in self._refresh_in_flight:
            await self._send(chat_id, format_refresh_busy(0, in_flight=True))
            return
        last = self._last_refresh_at.get(chat_id)
        if last is not None:
            elapsed = now - last
            remaining = self.settings.refresh_cooldown_seconds - elapsed
            if remaining > 0:
                await self._send(chat_id, format_refresh_busy(remaining))
                return
        # Reserve before the first await so a double-tap cannot race through.
        self._refresh_in_flight.add(chat_id)
        requested_by = self._refresh_requested_by(user)
        try:
            try:
                cluster_ids = (
                    [scope["cluster_id"]]
                    if scope["mode"] == "AUTO_CLUSTER"
                    else list(CLUSTER_USERS)
                )
                resolved = await self._run_db(
                    store.resolve_refresh_scope,
                    cluster_ids,
                )
                if not isinstance(resolved, dict):
                    raise ValueError("malformed refresh scope")
                start_iso = resolved["start"]
                end_iso = resolved["end"]
                users = resolved["users"]
                checkpoint_dates = resolved["checkpoint_dates"]
            except Exception as exc:
                logger.error(
                    "refresh checkpoint resolution failed chat=%s mode=%s error_type=%s",
                    chat_id,
                    scope["mode"],
                    type(exc).__name__,
                )
                message = (
                    format_refresh_pending()
                    if getattr(exc, "code", None) == "pending"
                    else format_refresh_rejected("Checkpoint refresh tidak tersedia.")
                )
                await self._send(
                    chat_id,
                    message,
                )
                return

            try:
                result = await asyncio.to_thread(
                    self._refresh_submitter,
                    base_url=self.settings.kestra_base_url,
                    namespace=self.settings.kestra_namespace,
                    flow_id=self.settings.kestra_flow_id,
                    start=start_iso,
                    end=end_iso,
                    users=users,
                    dry_run=False,
                    requested_by=requested_by,
                    trigger_source="telegram-bot",
                    verify_bucket_topup=True,
                    refresh_mode=scope["mode"],
                    checkpoint_dates=checkpoint_dates,
                    basic_user=self.settings.kestra_basic_user,
                    basic_password=self.settings.kestra_basic_password,
                    timeout=self.REFRESH_HTTP_TIMEOUT,
                )
            except Exception as exc:
                logger.error(
                    "kestra refresh submission raised chat=%s error_type=%s",
                    chat_id,
                    type(exc).__name__,
                )
                await self._send(
                    chat_id,
                    format_refresh_rejected(
                        "Kestra tidak menerima permintaan refresh."
                    ),
                )
                return
            execution_id = execution_id_from_response(result)
            try:
                status_code = int(result.get("status_code") or 0) if isinstance(result, dict) else 0
            except (TypeError, ValueError):
                status_code = 0
            if 200 <= status_code < 300 and execution_id:
                self._last_refresh_at[chat_id] = self._monotonic()
                execution_url = ""
                if self.settings.kestra_public_url:
                    execution_url = kestra_execution_web_url(
                        self.settings.kestra_public_url,
                        self.settings.kestra_namespace,
                        self.settings.kestra_flow_id,
                        execution_id,
                    )
                await self._send(
                    chat_id,
                    format_refresh_queued(
                        execution_id,
                        start_iso,
                        end_iso,
                        execution_url,
                        requested_by,
                        cluster_id=scope["cluster_id"],
                        checkpoint_dates=checkpoint_dates,
                    ),
                )
                return
            body = self._refresh_failure_detail(
                result.get("body") if isinstance(result, dict) else "malformed response"
            )
            logger.warning(
                "kestra refresh rejected chat=%s status=%s body=%.500s",
                chat_id,
                status_code,
                body[:500],
            )
            await self._send(
                chat_id,
                format_refresh_rejected(
                    "Kestra tidak menerima permintaan refresh."
                ),
            )
        finally:
            self._refresh_in_flight.discard(chat_id)
