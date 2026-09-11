"""Tests for the Telegram /refresh command: parser, renderers, and wiring."""
import asyncio
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from finpay_topup_pipeline.refresh_service import DEFAULT_USERS

from services.telegram_bot.callbacks import (
    HELP_TEXT,
    REFRESH_USAGE,
    format_refresh_busy,
    format_refresh_details,
    format_refresh_queued,
    format_refresh_rejected,
    format_refresh_unconfigured,
    parse_refresh_args,
)
from services.telegram_bot.config import Settings, load_settings
from services.telegram_bot.service import ClassificationService
from services.telegram_bot import store as bot_store


TODAY = date(2026, 8, 26)


def make_settings(**overrides):
    values = dict(
        bot_token="token",
        webhook_secret="secret",
        kestra_base_url="http://kestra:8080",
        kestra_namespace="finance.finpay",
        kestra_flow_id="finpay_topup_pipeline_v1",
        kestra_public_url="https://kestra.example.com",
        kestra_basic_user="admin@example.com",
        kestra_basic_password="pw",
        refresh_cooldown_seconds=600,
        selective_refresh_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def ok_result(execution_id="exec-1"):
    payload = {"id": execution_id}
    return {"status_code": 200, "body": f'{{"id":"{execution_id}"}}', "json": payload}


def make_service(settings=None, submitter=None, clock=None):
    service = ClassificationService(
        settings if settings is not None else make_settings(),
        refresh_submitter=submitter if submitter is not None else MagicMock(return_value=ok_result()),
        monotonic=clock if clock is not None else Clock(),
    )
    service.bot = MagicMock()
    service.bot.send_message = AsyncMock()
    service.bot.send_document = AsyncMock()
    service._run_db = AsyncMock(return_value={
        "start": "2026-08-20",
        "end": "2026-08-26",
        "users": list(DEFAULT_USERS),
        "checkpoint_dates": {
            user[:-2]: "2026-08-20" for user in DEFAULT_USERS
        },
    })
    return service


def sent_texts(service):
    return [call.kwargs["text"] for call in service.bot.send_message.await_args_list]


async def wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class ParseRefreshArgsTest(unittest.TestCase):
    def test_date_window_refresh_forms_are_removed(self):
        for args in ([], ["now"], ["yesterday"], ["2026-08-01", "2026-08-25"]):
            with self.subTest(args=args):
                start, end, err = parse_refresh_args(args, TODAY)
                self.assertEqual((start, end, err), (None, None, REFRESH_USAGE))
        self.assertIn("/refresh all", HELP_TEXT)


class RefreshRenderersTest(unittest.TestCase):
    def test_queued_reply_contains_id_window_sender_and_own_line_url(self):
        text = format_refresh_queued(
            "exec-9",
            "2026-08-20",
            "2026-08-26",
            "https://kestra.example.com/ui/executions/finance.finpay/finpay_topup_pipeline_v1/exec-9",
            "finance_ops",
        )
        lines = text.splitlines()
        self.assertIn("exec-9", text)
        self.assertIn("20 Agu 2026 - 26 Agu 2026", text)
        self.assertNotIn("finance_ops", text)
        self.assertEqual(lines[-2], "ID eksekusi: exec-9")
        self.assertEqual(
            lines[-1],
            "https://kestra.example.com/ui/executions/finance.finpay/"
            "finpay_topup_pipeline_v1/exec-9",
        )

    def test_queued_reply_omits_link_when_unconfigured(self):
        text = format_refresh_queued("exec-9", "2026-08-26", "2026-08-26", "", "12345")
        self.assertNotIn("http", text)

    def test_all_cluster_reply_shows_each_checkpoint_start(self):
        text = format_refresh_queued(
            "exec-all",
            "2026-08-11",
            "2026-09-10",
            "",
            checkpoint_dates={
                "411311": "2026-09-04",
                "421315": "2026-08-11",
            },
        )
        self.assertIn("Tanggal mulai per wilayah", text)
        self.assertIn("Palangkaraya: 4 Sep 2026", text)
        self.assertIn("Banggai: 11 Agu 2026", text)
        self.assertIn("Sampai: 10 Sep 2026", text)
        self.assertNotIn("Periode: 11 Agu 2026 - 10 Sep 2026", text)

    def test_busy_messages_distinguish_in_flight_from_cooldown(self):
        self.assertIn("sedang berjalan", format_refresh_busy(0, in_flight=True))
        self.assertIn("baru saja diterima", format_refresh_busy(125))
        self.assertIn("2 menit 05 detik", format_refresh_busy(125))
        self.assertIn("0 menit 00 detik", format_refresh_busy(-3))

    def test_rejected_and_unconfigured_are_safe(self):
        text = format_refresh_rejected("Kestra did not accept the refresh request.")
        self.assertIn("Refresh ditolak", text)
        self.assertIn(
            "Pemicu Kestra belum dikonfigurasi",
            format_refresh_unconfigured(),
        )


class LoadSettingsKestraTest(unittest.TestCase):
    def test_load_settings_reads_kestra_and_cooldown_values(self):
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": "tok",
                "TELEGRAM_WEBHOOK_SECRET": "sec",
                "KESTRA_BASE_URL": "http://kestra:8080/",
                "KESTRA_NAMESPACE": "finance.finpay",
                "KESTRA_FLOW_ID": "finpay_topup_pipeline_v1",
                "KESTRA_PUBLIC_URL": "https://kestra.example.com/",
                "KESTRA_BASIC_AUTH_USERNAME": "admin@example.com",
                "KESTRA_BASIC_AUTH_PASSWORD": "pw",
                "TELEGRAM_REFRESH_COOLDOWN_SECONDS": "60",
                "TELEGRAM_SELECTIVE_REFRESH_ENABLED": "true",
            }
        )
        self.assertTrue(settings.kestra_configured)
        self.assertEqual(settings.kestra_base_url, "http://kestra:8080")
        self.assertEqual(settings.kestra_public_url, "https://kestra.example.com")
        self.assertEqual(settings.kestra_basic_user, "admin@example.com")
        self.assertEqual(settings.kestra_basic_password, "pw")
        self.assertEqual(settings.refresh_cooldown_seconds, 60)
        self.assertTrue(settings.selective_refresh_enabled)

    def test_kestra_unconfigured_when_any_required_value_missing(self):
        base = {
            "bot_token": "tok",
            "webhook_secret": "sec",
            "kestra_base_url": "http://kestra:8080",
            "kestra_namespace": "finance.finpay",
            "kestra_flow_id": "finpay_topup_pipeline_v1",
            "kestra_basic_user": "u",
            "kestra_basic_password": "p",
        }
        self.assertTrue(Settings(**base).kestra_configured)
        for field_name in (
            "kestra_base_url",
            "kestra_namespace",
            "kestra_flow_id",
            "kestra_basic_user",
            "kestra_basic_password",
        ):
            values = dict(base)
            values[field_name] = ""
            with self.subTest(field=field_name):
                self.assertFalse(Settings(**values).kestra_configured)

    def test_invalid_cooldown_fails_startup(self):
        for bad in ("-5", "soon", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    load_settings({"TELEGRAM_REFRESH_COOLDOWN_SECONDS": bad})

    def test_invalid_selective_refresh_flag_fails_startup(self):
        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_SELECTIVE_REFRESH_ENABLED": "sometimes"})


class RefreshCommandRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_routes_without_kredit_or_status_regression(self):
        service = make_service()
        service._cmd_kredit = AsyncMock()
        service._cmd_status = AsyncMock()
        service._cmd_refresh = AsyncMock()

        for command, handler in (
            ("/topup", "_show_topup_menu"),
            ("/kredit", "_show_kredit_menu"),
            ("/kreditlist", "_show_kredit_report_menu"),
            ("/topupexcel", "_show_topup_export_menu"),
            ("/status", "_show_status_menu"),
        ):
            message = MagicMock()
            message.text = command
            setattr(service, handler, AsyncMock())
            await service._handle_message(message, 100, SimpleNamespace(username="finance_ops", id=7))
            getattr(service, handler).assert_awaited_once()

    async def test_unconfigured_kestra_affects_only_refresh(self):
        service = make_service(settings=make_settings(kestra_base_url=""))
        service._cmd_kredit = AsyncMock()
        service._cmd_status = AsyncMock()

        await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])
        texts = sent_texts(service)
        self.assertEqual(len(texts), 1)
        self.assertIn("Pemicu Kestra belum dikonfigurasi", texts[0])

        message = MagicMock()
        message.text = "/status"
        service._show_status_menu = AsyncMock()
        await service._handle_message(message, 100, None)
        service._show_status_menu.assert_awaited_once()

        message.text = "/kredit"
        service._show_kredit_menu = AsyncMock()
        await service._handle_message(message, 100, None)
        service._show_kredit_menu.assert_awaited_once()


class ScopedReadAndClassificationTest(unittest.IsolatedAsyncioTestCase):
    def _query(self, data):
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.message.edit_text = AsyncMock()
        return query

    async def test_cluster_kredit_listing_keeps_scope_in_session(self):
        service = make_service()
        service._run_db = AsyncMock(
            return_value=(
                [{"code": "OUTLET_1", "label": "Outlet 1"}],
                [{
                    "txn_id": 7,
                    "cluster_id": "421318",
                    "transaction_date": "2026-08-25T09:00:00",
                    "amount": 100,
                    "remarks": "credit",
                }],
            )
        )
        service._present_next = AsyncMock()

        await service._cmd_kredit(100, ["cluster", "421318", "all"])

        self.assertEqual(service.sessions[100]["cluster_id"], "421318")
        service._present_next.assert_awaited_once()

    async def test_kredit_report_lists_classification_with_scope_and_page(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "rows": [{
                "txn_id": 7,
                "cluster_id": "421318",
                "transaction_date": "2026-08-25T09:00:00",
                "amount": 100,
                "outlet_code": "OUTLET_1",
                "outlet_label": "Outlet 1",
            }],
            "has_more": True,
        })

        await service._cmd_kredit_report(
            100,
            ["cluster", "421318", "custom", "2026-08-25", "2026-08-25"],
        )

        self.assertEqual(service.report_sessions[100], {
            "cluster_id": "421318",
            "start": "2026-08-25",
            "end": "2026-08-25",
            "page": 0,
        })
        self.assertIn("Outlet 1", sent_texts(service)[0])
        self.assertIn("Berikutnya", service.bot.send_message.await_args.kwargs["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"])

    async def test_kredit_report_custom_callback_shows_scoped_command(self):
        service = make_service()
        query = self._query("l:c:421318:x")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        query.answer.assert_awaited_once_with()
        text = query.message.edit_text.await_args.args[0]
        self.assertIn("/kreditlist cluster 421318 custom", text)

    async def test_kredit_report_page_reuses_chat_scope(self):
        service = make_service()
        service.report_sessions[100] = {
            "cluster_id": "421318",
            "start": "2026-08-25",
            "end": "2026-08-25",
            "page": 0,
        }
        service._run_db = AsyncMock(return_value={"rows": [], "has_more": False})
        query = self._query("l:c:421318:p:1")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertEqual(service.report_sessions[100]["page"], 1)
        query.answer.assert_awaited_once_with("Memuat halaman...")
        self.assertIn("Tidak ada Kredit", query.message.edit_text.await_args.args[0])

    async def test_topup_excel_generates_finance_workbook_for_custom_scope(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "content": b"xlsx",
            "filename": "finpay-topup-421318.xlsx",
            "row_count": 25,
            "cluster_counts": {"421318": 25},
            "start": "2026-08-20",
            "end": "2026-08-22",
        })

        await service._cmd_topup_excel(
            100,
            ["cluster", "421318", "custom", "2026-08-20", "2026-08-22"],
        )

        call = service.bot.send_document.await_args.kwargs
        self.assertEqual(call["chat_id"], 100)
        self.assertEqual(call["document"].filename, "finpay-topup-421318.xlsx")
        self.assertIn("25 baris", call["caption"])

    async def test_topup_excel_custom_callback_shows_scoped_command(self):
        service = make_service()
        query = self._query("e:c:421318:x")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        query.answer.assert_awaited_once_with()
        self.assertIn(
            "/topupexcel cluster 421318 custom",
            query.message.edit_text.await_args.args[0],
        )

    async def test_skip_defers_current_row_and_presents_next_row(self):
        service = make_service()
        service.sessions[100] = {
            "skipped": set(),
            "seq": 1,
            "current_txn": 7,
            "current_cluster_id": "421318",
            "cluster_id": "421318",
            "start": "2026-08-20",
            "end": "2026-08-25",
            "total_count": 2,
        }
        service._run_db = AsyncMock(side_effect=[
            [
                {"txn_id": 7, "cluster_id": "421318", "transaction_date": "2026-08-25", "amount": 100},
                {"txn_id": 8, "cluster_id": "421318", "transaction_date": "2026-08-25", "amount": 200},
            ],
            [{"code": "OUTLET_1", "label": "Outlet 1"}],
        ])
        query = self._query("n:skip")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertEqual(service.sessions[100]["skipped"], {7})
        query.answer.assert_awaited_once_with("Ditunda")
        self.assertIn("#8", sent_texts(service)[-1])
        self.assertIn("2 dari 2", sent_texts(service)[-1])

    async def test_done_reports_assigned_deferred_and_remaining_counts(self):
        service = make_service()
        service.sessions[100] = {
            "skipped": {7},
            "seq": 1,
            "current_txn": 8,
            "cluster_id": "421318",
            "start": "2026-08-20",
            "end": "2026-08-25",
            "total_count": 2,
        }
        service._run_db = AsyncMock(return_value=[
            {"txn_id": 8, "cluster_id": "421318", "transaction_date": "2026-08-25", "amount": 200},
        ])
        query = self._query("n:done")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertNotIn(100, service.sessions)
        text = query.message.edit_text.await_args.args[0]
        self.assertIn("Ditandai: 1", text)
        self.assertIn("Ditunda: 1", text)
        self.assertIn("Belum ditandai: 0", text)
        query.answer.assert_awaited_once_with("Tinjauan selesai")

    async def test_classification_callback_passes_session_cluster_to_guard(self):
        service = make_service()
        service.sessions[100] = {
            "skipped": set(),
            "seq": 1,
            "current_txn": 7,
            "cluster_id": "421318",
            "start": "2026-08-20",
            "end": "2026-08-25",
        }
        service._run_db = AsyncMock(
            side_effect=[
                {"txn_id": 7, "code": "OUTLET_1", "label": "Outlet 1"},
                [],
            ]
        )
        query = self._query("c:1:7:OUTLET_1")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        called_fn, txn_id, cluster_id, outlet_code, classified_by = (
            service._run_db.await_args_list[0].args
        )
        self.assertEqual(called_fn.__name__, "classify_for_cluster")
        self.assertEqual((txn_id, cluster_id, outlet_code), (7, "421318", "OUTLET_1"))
        self.assertEqual(classified_by, "finance_ops")

    async def test_stale_classification_callback_is_not_written(self):
        service = make_service()
        service.sessions[100] = {
            "skipped": set(),
            "seq": 1,
            "current_txn": 7,
            "cluster_id": "421318",
        }
        service._run_db = AsyncMock()
        query = self._query("c:1:8:OUTLET_1")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        service._run_db.assert_not_awaited()
        query.answer.assert_awaited_once_with("Kartu lama, gunakan kartu terbaru")

    async def test_scoped_read_database_errors_are_generic(self):
        service = make_service()
        service._run_db = AsyncMock(side_effect=RuntimeError("database password"))

        await service._cmd_status(100, ["cluster", "421318"])
        await service._cmd_kredit(100, ["cluster", "421318"])

        texts = sent_texts(service)
        self.assertIn("Status belum dapat dimuat", texts[0])
        self.assertIn("Kredit belum dapat dimuat", texts[1])
        self.assertNotIn("database password", " ".join(texts))

    async def test_status_cluster_queries_only_requested_cluster(self):
        service = make_service()
        counts = MagicMock(return_value=(4, 2))
        latest = MagicMock(return_value=None)
        service._run_db = AsyncMock(side_effect=lambda fn, *args: fn(object(), *args))
        with patch.object(bot_store, "counts", counts), patch.object(
            bot_store, "latest_refresh", latest
        ):
            await service._cmd_status(100, ["cluster", "421318"])

        counts.assert_called_once_with(ANY, cluster_id="421318")
        latest.assert_called_once_with(ANY, cluster_id="421318")
        text = sent_texts(service)[0]
        self.assertIn("Morowali (421318)", text)
        self.assertNotIn("411311", text)


class RefreshScopeStoreTest(unittest.TestCase):
    def _conn(self, pending=()):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = list(pending)
        return conn

    def test_scope_resolves_one_checkpoint_and_today(self):
        conn = self._conn()
        with patch.object(
            bot_store,
            "latest_checkpoints",
            return_value={"421318": {"as_of_date": date(2026, 8, 11), "opening_balance": 68837100}},
        ):
            result = bot_store.resolve_refresh_scope(
                conn, ["421318"], today=date(2026, 8, 28)
            )

        self.assertEqual(result["start"], "2026-08-11")
        self.assertEqual(result["end"], "2026-08-28")
        self.assertEqual(result["users"], ["421318_A"])
        self.assertEqual(result["checkpoint_dates"], {"421318": "2026-08-11"})

    def test_scope_rejects_missing_future_and_pending_checkpoints(self):
        cases = [
            ({}, (), "missing"),
            ({"421318": {"as_of_date": date(2026, 8, 29)}}, (), "future"),
            (
                {"421318": {"as_of_date": date(2026, 8, 11)}},
                ((9, ["421318_A"]),),
                "overlapping",
            ),
        ]
        for checkpoints, pending, expected in cases:
            with self.subTest(expected=expected):
                conn = self._conn(pending)
                with patch.object(bot_store, "latest_checkpoints", return_value=checkpoints):
                    with self.assertRaises(bot_store.RefreshScopeError) as raised:
                        bot_store.resolve_refresh_scope(
                            conn, ["421318"], today=date(2026, 8, 28)
                        )
                self.assertIn(expected, str(raised.exception))

    def test_refresh_details_exposes_source_and_cms_provenance(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(
            42,
            "PENDING_REVIEW",
            date(2026, 8, 20),
            date(2026, 8, 25),
            ["421318_A"],
            "AUTO_CLUSTER",
            "exec-42",
            "421318",
            date(2026, 8, 11),
            "VERIFIED",
            2,
            2,
            0,
            1000,
            1000,
            0,
            "2026-08-25T03:30:00+00:00",
            "2026-08-25T03:30:00+00:00",
            "2026-08-25T03:30:01+00:00",
            None,
            "2026-08-25T03:00:00+00:00",
            "2026-08-25T03:15:00+00:00",
            2,
            2,
            "TRANSACTION",
            8,
            "hash-8",
            "2026-08-25T03:15:00+00:00",
            1000,
            "finance_ops",
            "telegram-bot",
            "reviewer",
            "2026-08-25T04:00:00+00:00",
        )]

        result = bot_store.refresh_details(conn, 42)

        self.assertEqual(result["clusters"][0]["source_transaction_min_at"], "2026-08-25T03:00:00+00:00")
        self.assertEqual(result["clusters"][0]["source_transaction_max_at"], "2026-08-25T03:15:00+00:00")
        self.assertEqual(result["clusters"][0]["matching_row_number"], 2)
        self.assertEqual(result["clusters"][0]["matching_transaction_id"], 8)
        self.assertEqual(result["clusters"][0]["matching_row_hash"], "hash-8")
        self.assertEqual(result["requested_by"], "finance_ops")
        self.assertEqual(result["reviewed_by"], "reviewer")


class RefreshSubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_submission_sends_fixed_scope_and_settings(self):
        captured = {}

        def submitter(**kwargs):
            captured.update(kwargs)
            return ok_result("exec-42")

        service = make_service(submitter=submitter)
        user = SimpleNamespace(username="finance_ops", id=7)
        await service._cmd_refresh(100, user, ["all"])

        self.assertEqual(captured["base_url"], "http://kestra:8080")
        self.assertEqual(captured["namespace"], "finance.finpay")
        self.assertEqual(captured["flow_id"], "finpay_topup_pipeline_v1")
        self.assertEqual(captured["start"], "2026-08-20")
        self.assertEqual(captured["end"], "2026-08-26")
        self.assertEqual(captured["users"], DEFAULT_USERS)
        self.assertEqual(len(captured["users"]), 6)
        self.assertFalse(captured["dry_run"])
        self.assertTrue(captured["verify_bucket_topup"])
        self.assertEqual(captured["trigger_source"], "telegram-bot")
        self.assertEqual(captured["refresh_mode"], "AUTO_ALL")
        self.assertEqual(
            captured["checkpoint_dates"],
            {user[:-2]: "2026-08-20" for user in DEFAULT_USERS},
        )
        self.assertEqual(captured["requested_by"], "finance_ops")
        self.assertEqual(captured["basic_user"], "admin@example.com")
        self.assertEqual(captured["basic_password"], "pw")
        self.assertEqual(captured["timeout"], 30)

    async def test_username_validation_with_numeric_fallback(self):
        cases = [
            ("finance_ops", "finance_ops"),
            ("ab_1", None),
            ("has space", None),
            ("x" * 33, None),
            (None, None),
        ]
        for username, expected_name in cases:
            with self.subTest(username=username):
                captured = {}
                submitter = MagicMock(side_effect=lambda **kw: captured.update(kw) or ok_result())
                service = make_service(submitter=submitter)
                await service._cmd_refresh(
                    100,
                    SimpleNamespace(username=username, id=987654),
                    ["all"],
                )
                self.assertEqual(
                    captured["requested_by"],
                    expected_name or "987654",
                )

    async def test_accepted_execution_reply_includes_public_link(self):
        service = make_service(submitter=MagicMock(return_value=ok_result("exec-42")))
        await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])
        texts = sent_texts(service)
        self.assertEqual(len(texts), 1)
        self.assertIn("exec-42", texts[0])
        self.assertIn("https://kestra.example.com/ui/executions/", texts[0])

    async def test_missing_public_url_omits_link(self):
        service = make_service(
            settings=make_settings(kestra_public_url=""),
            submitter=MagicMock(return_value=ok_result("exec-42")),
        )
        await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])
        texts = sent_texts(service)
        self.assertNotIn("http", texts[0])

    async def test_selective_refresh_requires_feature_flag(self):
        submitter = MagicMock(return_value=ok_result("should-not-run"))
        service = make_service(
            settings=make_settings(selective_refresh_enabled=False),
            submitter=submitter,
        )

        await service._cmd_refresh(
            100,
            SimpleNamespace(username="finance_ops", id=7),
            ["cluster", "421318"],
        )

        submitter.assert_not_called()
        self.assertIn("Penyegaran otomatis sedang dinonaktifkan", sent_texts(service)[0])

    async def test_selective_refresh_submits_one_mapped_user_and_refresh_mode(self):
        captured = {}

        def submitter(**kwargs):
            captured.update(kwargs)
            return ok_result("exec-cluster")

        service = make_service(
            settings=make_settings(selective_refresh_enabled=True),
            submitter=submitter,
        )
        service._run_db.return_value = {
            "start": "2026-08-11",
            "end": "2026-08-26",
            "users": ["421318_A"],
            "checkpoint_dates": {"421318": "2026-08-11"},
        }
        await service._cmd_refresh(
            100,
            SimpleNamespace(username="finance_ops", id=7),
            ["cluster", "421318"],
        )

        self.assertEqual(captured["users"], ["421318_A"])
        self.assertEqual(captured["refresh_mode"], "AUTO_CLUSTER")
        self.assertEqual(captured["start"], "2026-08-11")
        self.assertEqual(captured["end"], "2026-08-26")
        self.assertEqual(captured["checkpoint_dates"], {"421318": "2026-08-11"})
        text = sent_texts(service)[0]
        self.assertIn("Morowali", text)
        self.assertIn("exec-cluster", text)
        for forbidden in ("Unclassified Kredit", "Kredit #", "Last top-up cycle", "Outlet 1"):
            self.assertNotIn(forbidden, text)

    async def test_checkpoint_failure_does_not_submit_or_start_cooldown(self):
        submitter = MagicMock(return_value=ok_result("should-not-run"))
        service = make_service(submitter=submitter)
        service._run_db = AsyncMock(side_effect=RuntimeError("database password"))

        await service._cmd_refresh(
            100,
            SimpleNamespace(username="finance_ops", id=7),
            ["all"],
        )

        submitter.assert_not_called()
        self.assertNotIn(100, service._last_refresh_at)
        text = sent_texts(service)[0]
        self.assertIn("Checkpoint refresh tidak tersedia", text)
        self.assertNotIn("database password", text)

    async def test_pending_scope_explains_previous_review(self):
        submitter = MagicMock(return_value=ok_result("should-not-run"))
        service = make_service(submitter=submitter)
        service._run_db = AsyncMock(
            side_effect=bot_store.RefreshScopeError("overlap", code="pending")
        )

        await service._cmd_refresh(
            100,
            SimpleNamespace(username="finance_ops", id=7),
            ["cluster", "421318"],
        )

        submitter.assert_not_called()
        self.assertIn("menunggu persetujuan", sent_texts(service)[0])
        self.assertIn("Setujui atau tolak", sent_texts(service)[0])

    async def test_refresh_does_not_call_status_or_kredit_stores(self):
        service = make_service()
        with patch.object(bot_store, "counts") as counts, patch.object(
            bot_store, "list_unclassified"
        ) as list_unclassified:
            await service._cmd_refresh(
                100,
                SimpleNamespace(username="finance_ops", id=7),
                ["all"],
            )

        counts.assert_not_called()
        list_unclassified.assert_not_called()


class RefreshFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_execution_id_rejects_without_cooldown(self):
        calls = []

        def submitter(**kwargs):
            calls.append(kwargs)
            return {"status_code": 200, "body": '{"state":"RUNNING"}', "json": {"state": "RUNNING"}}

        service = make_service(submitter=submitter)
        user = SimpleNamespace(username="finance_ops", id=7)
        await service._cmd_refresh(100, user, ["all"])
        await service._cmd_refresh(100, user, ["all"])

        self.assertEqual(len(calls), 2)
        self.assertTrue(all("Refresh ditolak" in t for t in sent_texts(service)))

    async def test_http_error_statuses_reject_generically_without_body_leak(self):
        for status in (401, 500):
            with self.subTest(status=status):
                secret_body = '{"message": "bad password for admin@example.com"}'
                result = {"status_code": status, "body": secret_body, "json": None}
                service = make_service(submitter=MagicMock(return_value=result))
                await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])
                texts = sent_texts(service)
                self.assertEqual(len(texts), 1)
                self.assertIn("Refresh ditolak", texts[0])
                self.assertNotIn("admin@example.com", texts[0])
                self.assertNotIn(secret_body, texts[0])
                self.assertNotIn(str(status), texts[0])

    async def test_unreachable_kestra_rejects_and_starts_no_cooldown(self):
        calls = []

        def submitter(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"status_code": 0, "body": "Kestra unreachable: connection refused", "json": None}
            return ok_result("exec-later")

        service = make_service(submitter=submitter)
        user = SimpleNamespace(username="finance_ops", id=7)
        await service._cmd_refresh(100, user, ["all"])
        await service._cmd_refresh(100, user, ["all"])

        self.assertEqual(len(calls), 2)
        self.assertTrue(any("Refresh ditolak" in t for t in sent_texts(service)))
        self.assertTrue(any("exec-later" in t for t in sent_texts(service)))

    async def test_submitter_exception_rejects_and_clears_reservation(self):
        service = make_service(
            submitter=MagicMock(side_effect=RuntimeError("secret-password"))
        )
        await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])

        self.assertNotIn(100, service._refresh_in_flight)
        self.assertIn("Refresh ditolak", sent_texts(service)[0])
        self.assertNotIn("secret-password", sent_texts(service)[0])

    async def test_failure_clears_in_flight_reservation(self):
        service = make_service(
            submitter=MagicMock(return_value={"status_code": 502, "body": "bad gateway", "json": None})
        )
        await service._cmd_refresh(100, SimpleNamespace(username="finance_ops", id=7), ["all"])
        self.assertNotIn(100, service._refresh_in_flight)


class RefreshGuardsTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_refuses_then_allows_after_expiry(self):
        clock = Clock(start=10_000.0)
        calls = []

        def submitter(**kwargs):
            calls.append(kwargs)
            return ok_result(f"exec-{len(calls)}")

        service = make_service(submitter=submitter, clock=clock)
        user = SimpleNamespace(username="finance_ops", id=7)

        await service._cmd_refresh(100, user, ["all"])
        clock.now += 500
        await service._cmd_refresh(100, user, ["all"])
        texts = sent_texts(service)
        self.assertEqual(len(texts), 2)
        self.assertIn("baru saja diterima", texts[1])
        self.assertIn("1 menit 40 detik", texts[1])

        clock.now += 101
        await service._cmd_refresh(100, user, ["all"])
        texts = sent_texts(service)
        self.assertIn("exec-2", texts[-1])

    async def test_success_after_full_cooldown(self):
        clock = Clock(start=0.0)
        calls = []

        def submitter(**kwargs):
            calls.append(kwargs)
            return ok_result(f"exec-{len(calls)}")

        service = make_service(submitter=submitter, clock=clock)
        user = SimpleNamespace(username="finance_ops", id=7)
        await service._cmd_refresh(100, user, ["all"])
        clock.now += 600
        await service._cmd_refresh(100, user, ["all"])
        self.assertEqual(len(calls), 2)

    async def test_concurrent_double_tap_is_refused_while_in_flight(self):
        class BlockingSubmitter:
            def __init__(self):
                self.release = threading.Event()
                self.calls = 0

            def __call__(self, **kwargs):
                self.calls += 1
                self.release.wait(timeout=5)
                return ok_result("exec-blocking")

        submitter = BlockingSubmitter()
        service = make_service(submitter=submitter)
        user = SimpleNamespace(username="finance_ops", id=7)

        first = asyncio.create_task(service._cmd_refresh(100, user, ["all"]))
        self.assertTrue(await wait_until(lambda: 100 in service._refresh_in_flight))

        await service._cmd_refresh(100, user, ["all"])
        submitter.release.set()
        await first

        texts = sent_texts(service)
        self.assertEqual(submitter.calls, 1)
        self.assertTrue(any("sedang berjalan" in t for t in texts))
        self.assertEqual(sum("exec-blocking" in t for t in texts), 1)
        self.assertNotIn(100, service._refresh_in_flight)


class RefreshReviewCallbackTest(unittest.IsolatedAsyncioTestCase):
    def _query(self, data):
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.message.edit_text = AsyncMock()
        return query

    async def test_approve_callback_uses_store_and_edits_safe_result(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "refresh_id": 42,
            "status": "COMMITTED",
            "decision": "approved",
            "idempotent": False,
            "inserted": 3,
        })
        query = self._query("r:a:42")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        called_fn, refresh_id, reviewed_by = service._run_db.await_args.args
        self.assertEqual(called_fn.__name__, "approve_refresh")
        self.assertEqual((refresh_id, reviewed_by), (42, "finance_ops"))
        query.message.edit_text.assert_awaited_once()
        self.assertIn("disetujui", query.message.edit_text.await_args.args[0])
        query.answer.assert_awaited_once_with("Tindakan tersimpan")

    async def test_details_callback_uses_refresh_audit_store(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "refresh_id": 42,
            "status": "PENDING_REVIEW",
            "requested_start": "2026-08-20",
            "requested_end": "2026-08-26",
            "refresh_mode": "AUTO_CLUSTER",
            "kestra_execution_id": "exec-42",
            "requested_by": "finance_ops",
            "trigger_source": "telegram-bot",
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-25T04:00:00+00:00",
            "clusters": [{
                "cluster_id": "421318",
                "status": "VERIFIED",
                "checkpoint_as_of_date": "2026-08-11",
                "computed_saldo": 1000,
                "bucket_topup_value": 1000,
                "verification_diff": 0,
                "source_row_count": 2,
                "loaded_inserted": 2,
            }],
        })
        query = self._query("r:d:42")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        called_fn, refresh_id = service._run_db.await_args.args
        self.assertEqual(called_fn.__name__, "refresh_details")
        self.assertEqual(refresh_id, 42)
        self.assertIn("DETAIL TOP-UP", query.message.edit_text.await_args.args[0])
        self.assertEqual(query.message.edit_text.await_args.kwargs["parse_mode"], "HTML")
        query.answer.assert_awaited_once_with("Detail dimuat")

    async def test_partial_approve_keeps_review_keyboard_for_held_clusters(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "refresh_id": 42,
            "status": "PENDING_REVIEW",
            "decision": "partially_approved",
            "partial": True,
            "inserted": 3,
            "held_clusters": ["411311"],
        })
        query = self._query("r:a:42")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertIn("disetujui sebagian", query.message.edit_text.await_args.args[0])
        self.assertIsNotNone(query.message.edit_text.await_args.kwargs["reply_markup"])

    async def test_details_renderer_includes_audit_timestamps(self):
        text = format_refresh_details({
            "refresh_id": 42,
            "status": "PENDING_REVIEW",
            "requested_start": "2026-08-20",
            "requested_end": "2026-08-26",
            "refresh_mode": "AUTO_CLUSTER",
            "kestra_execution_id": "exec-42",
            "requested_by": "finance_ops",
            "trigger_source": "telegram-bot",
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-25T04:00:00+00:00",
            "clusters": [{
                "cluster_id": "421318",
                "status": "VERIFIED",
                "checkpoint_as_of_date": "2026-08-11",
                "computed_saldo": 1000,
                "bucket_topup_value": 1000,
                "verification_diff": 0,
                "source_row_count": 2,
                "loaded_inserted": 2,
                "source_transaction_min_at": "2026-08-25T03:00:00+00:00",
                "source_transaction_max_at": "2026-08-25T03:15:00+00:00",
                "bucket_snapshot_at": "2026-08-25T03:30:00+00:00",
                "calculation_cutoff_at": "2026-08-25T03:30:00+00:00",
                "calculated_at": "2026-08-25T03:30:01+00:00",
                "calculation_row_count": 2,
                "matching_row_number": 2,
                "matching_type": "TRANSACTION",
                "matching_transaction_id": 8,
                "matching_transaction_at": "2026-08-25T03:15:00+00:00",
                "matching_running_saldo": 1000,
            }],
        })

        self.assertIn("<b>Mode:</b> <code>Otomatis per wilayah</code>", text)
        self.assertIn("<b>Diminta oleh:</b> <code>finance_ops</code>", text)
        self.assertIn("<b>Ditinjau oleh:</b> <code>reviewer</code>", text)
        self.assertIn("<b>Waktu tinjau:</b> <code>25 Agu 2026 12:00:00</code>", text)
        self.assertIn("<b>Sumber:</b> <code>25 Agu 2026 11:00:00 - 25 Agu 2026 11:15:00</code>", text)
        self.assertIn("<b>CMS diambil:</b> <code>25 Agu 2026 11:30:00</code>", text)
        self.assertIn("<b>Dihitung:</b> <code>25 Agu 2026 11:30:01</code>", text)
        self.assertIn("<b>Saldo cocok pada baris:</b> <code>2 dari 2</code>", text)
        self.assertIn("<b>ID transaksi cocok:</b> <code>8</code>", text)

    async def test_repeated_reject_is_reported_as_existing_decision(self):
        service = make_service()
        service._run_db = AsyncMock(return_value={
            "refresh_id": 42,
            "status": "REJECTED",
            "decision": "rejected",
            "idempotent": True,
        })
        query = self._query("r:r:42")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertIn("sudah ditolak", query.message.edit_text.await_args.args[0])

    async def test_review_database_error_is_generic(self):
        service = make_service()
        service._run_db = AsyncMock(side_effect=RuntimeError("database password"))
        query = self._query("r:a:42")

        await service._handle_callback(query, 100, SimpleNamespace(username="finance_ops", id=7))

        self.assertIn("Tindakan persetujuan gagal", query.message.edit_text.await_args.args[0])
        self.assertNotIn("database password", query.message.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
