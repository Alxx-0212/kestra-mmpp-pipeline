import unittest

from services.telegram_bot.callbacks import (
    CALLBACK_MAX_BYTES,
    REFUSE_TEXT,
    UpdateIdCache,
    build_home_keyboard,
    build_kredit_keyboard,
    build_kredit_period_keyboard,
    build_kredit_report_keyboard,
    build_kredit_report_page_keyboard,
    build_kredit_report_period_keyboard,
    build_outlet_keyboard_buttons,
    build_review_keyboard,
    build_status_keyboard,
    build_topup_export_keyboard,
    build_topup_export_period_keyboard,
    build_topup_keyboard,
    default_window,
    format_datetime_label,
    format_kredit_report,
    format_kredit_report_custom_prompt,
    format_kredit_done,
    format_nothing_to_classify,
    format_refresh_details,
    format_review_result,
    format_state,
    format_status,
    format_status_all,
    format_status_cluster,
    format_topup_export_custom_prompt,
    format_txn_card,
    format_walkthrough_done,
    is_allowed,
    make_classify_callback,
    parse_callback_data,
    parse_kredit_command_args,
    parse_kredit_args,
    parse_refresh_command_args,
    parse_status_args,
)
from datetime import date


OUTLETS = [
    {"code": "OUTLET_1", "label": "Outlet 1"},
    {"code": "OUTLET_2", "label": "Outlet 2"},
    {"code": "OUTLET_3", "label": "Outlet 3"},
]


class TestParseCallbackData(unittest.TestCase):
    def test_classify_contract_from_plan(self):
        self.assertEqual(
            parse_callback_data("c:3:8451:OUTLET_2"),
            {"action": "classify", "seq": 3, "txn_id": 8451, "outlet": "OUTLET_2"},
        )

    def test_navigation_callbacks(self):
        self.assertEqual(parse_callback_data("n:skip"), {"action": "skip"})
        self.assertEqual(parse_callback_data("n:done"), {"action": "done"})

    def test_menu_callbacks_are_scoped_to_allowlisted_choices(self):
        self.assertEqual(parse_callback_data("m:topup"), {"action": "menu", "target": "topup"})
        self.assertEqual(parse_callback_data("m:kreditlist"), {"action": "menu", "target": "kreditlist"})
        self.assertEqual(parse_callback_data("m:topupexcel"), {"action": "menu", "target": "topupexcel"})
        self.assertEqual(
            parse_callback_data("t:c:421318"),
            {"action": "topup_scope", "scope": "cluster", "cluster_id": "421318"},
        )
        self.assertEqual(
            parse_callback_data("k:c:421318:7"),
            {"action": "kredit_scope", "scope": "cluster", "cluster_id": "421318", "window": "7"},
        )
        self.assertEqual(
            parse_callback_data("k:c:421318:m"),
            {"action": "kredit_period_menu", "scope": "cluster", "cluster_id": "421318"},
        )
        self.assertEqual(
            parse_callback_data("l:a:3"),
            {"action": "kredit_report_scope", "scope": "all", "window": "3"},
        )
        self.assertEqual(
            parse_callback_data("l:c:421318:p:2"),
            {"action": "kredit_report_page", "scope": "cluster", "cluster_id": "421318", "page": 2},
        )
        self.assertEqual(
            parse_callback_data("e:c:421318:3"),
            {"action": "topup_export_scope", "scope": "cluster", "cluster_id": "421318", "window": "3"},
        )
        self.assertEqual(
            parse_callback_data("e:a:m"),
            {"action": "topup_export_period_menu", "scope": "all"},
        )
        self.assertIsNone(parse_callback_data("t:c:999999"))
        self.assertIsNone(parse_callback_data("k:c:421318:30"))
        self.assertIsNone(parse_callback_data("l:c:421318:p:10001"))

    def test_review_callbacks_require_positive_numeric_refresh_id(self):
        self.assertEqual(
            parse_callback_data("r:a:42"),
            {"action": "approve", "refresh_id": 42},
        )
        self.assertEqual(
            parse_callback_data("r:r:42"),
            {"action": "reject", "refresh_id": 42},
        )
        for raw in ("r:a:0", "r:r:-1", "r:x:42", "r:a:abc", "r:a:42:extra"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_callback_data(raw))

    def test_invalid_payloads_return_none(self):
        for raw in (
            None,
            "",
            "x:1:2:OUTLET_1",
            "c:a:b:c",
            "c:1",
            "c:1:2:",
            "n:nope",
            "c:-1:5:OUTLET_1",
            "c:1:0:OUTLET_1",
            "c:1:2:OUTLET_1:extra",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_callback_data(raw))


class TestCallbackBudget(unittest.TestCase):
    def test_make_classify_callback_within_telegram_limit(self):
        data = make_classify_callback(99999, 2147483647, "OUTLET_3")
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_MAX_BYTES)

    def test_oversized_outlet_code_is_rejected(self):
        with self.assertRaises(ValueError):
            make_classify_callback(1, 2, "X" * 80)

    def test_outlet_code_cannot_break_callback_shape(self):
        with self.assertRaises(ValueError):
            make_classify_callback(1, 2, "OUTLET:1")

    def test_keyboard_rows_are_bounded_and_terminated_by_controls(self):
        rows = build_outlet_keyboard_buttons(OUTLETS, 4, 77)
        flat = [pair for row in rows for pair in row]
        self.assertEqual(flat[-2:], [("Lewati", "n:skip"), ("Selesai", "n:done")])
        outlet_pairs = flat[:-2]
        self.assertEqual([p[1] for p in outlet_pairs], [
            "c:4:77:OUTLET_1", "c:4:77:OUTLET_2", "c:4:77:OUTLET_3",
        ])
        self.assertTrue(all(len(row) <= 3 for row in rows[:-1]))


class TestAuthFilter(unittest.TestCase):
    def test_fail_closed_when_lists_missing(self):
        self.assertFalse(is_allowed(1, 1, frozenset(), frozenset({1})))
        self.assertFalse(is_allowed(1, 1, frozenset({1}), frozenset()))

    def test_both_whitelists_must_match(self):
        chats = frozenset({100})
        users = frozenset({7})
        self.assertTrue(is_allowed(100, 7, chats, users))
        self.assertFalse(is_allowed(101, 7, chats, users))
        self.assertFalse(is_allowed(100, 8, chats, users))


class TestUpdateIdCache(unittest.TestCase):
    def test_first_seen_false_then_true(self):
        cache = UpdateIdCache()
        self.assertFalse(cache.seen(10))
        self.assertTrue(cache.seen(10))
        self.assertFalse(cache.seen(11))

    def test_evicts_oldest_beyond_capacity(self):
        cache = UpdateIdCache(capacity=2)
        cache.seen(1)
        cache.seen(2)
        cache.seen(3)
        self.assertFalse(cache.seen(1))
        self.assertTrue(cache.seen(3))


class TestKreditArgs(unittest.TestCase):
    def test_default_is_last_seven_days(self):
        today = date(2026, 8, 25)
        start, end, err = parse_kredit_args([], today)
        self.assertIsNone(err)
        self.assertEqual((start, end), ("2026-08-19", "2026-08-25"))

    def test_all_opens_window(self):
        start, end, err = parse_kredit_args(["all"])
        self.assertIsNone(err)
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_short_windows_and_custom_window(self):
        today = date(2026, 8, 25)
        self.assertEqual(parse_kredit_args(["1"], today)[:2], ("2026-08-25", "2026-08-25"))
        self.assertEqual(parse_kredit_args(["3"], today)[:2], ("2026-08-23", "2026-08-25"))
        self.assertEqual(
            parse_kredit_args(["custom", "2026-08-01", "2026-08-03"], today)[:2],
            ("2026-08-01", "2026-08-03"),
        )

    def test_explicit_range_validated(self):
        start, end, err = parse_kredit_args(["2026-08-01", "2026-08-07"])
        self.assertIsNone(err)
        self.assertEqual((start, end), ("2026-08-01", "2026-08-07"))
        _, _, err = parse_kredit_args(["2026-08-07", "2026-08-01"])
        self.assertIsNotNone(err)
        _, _, err = parse_kredit_args(["20260807", "x"])
        self.assertIsNotNone(err)

    def test_wrong_arity_returns_usage(self):
        _, _, err = parse_kredit_args(["2026-08-01"])
        self.assertIn("Format:", err)

    def test_cluster_scope_defaults_and_all(self):
        self.assertEqual(
            parse_kredit_command_args(["cluster", "421318"], date(2026, 8, 25)),
            ("421318", "2026-08-19", "2026-08-25", None),
        )
        self.assertEqual(
            parse_kredit_command_args(["cluster", "421318", "all"], date(2026, 8, 25)),
            ("421318", None, None, None),
        )
        self.assertEqual(
            parse_kredit_command_args(["cluster", "421318", "1"], date(2026, 8, 25)),
            ("421318", "2026-08-25", "2026-08-25", None),
        )
        self.assertEqual(
            parse_kredit_command_args(
                ["cluster", "421318", "custom", "2026-08-20", "2026-08-22"],
                date(2026, 8, 25),
            ),
            ("421318", "2026-08-20", "2026-08-22", None),
        )
        self.assertEqual(
            parse_kredit_command_args(["all", "1"], date(2026, 8, 25)),
            (None, "2026-08-25", "2026-08-25", None),
        )
        cluster, start, end, error = parse_kredit_command_args(
            ["cluster", "421318", "2026-08-20", "2026-08-25"],
            date(2026, 8, 25),
        )
        self.assertEqual((cluster, start, end, error), ("421318", "2026-08-20", "2026-08-25", None))

    def test_cluster_scope_rejects_unknown_or_future_dates(self):
        self.assertIsNotNone(parse_kredit_command_args(["cluster", "999999"], date(2026, 8, 25))[3])
        self.assertIsNotNone(
            parse_kredit_command_args(
                ["cluster", "421318", "2026-08-20", "2026-08-26"],
                date(2026, 8, 25),
            )[3]
        )


class TestScopedCommandArgs(unittest.TestCase):
    def test_status_scope(self):
        self.assertEqual(parse_status_args([]), (None, None))
        self.assertEqual(parse_status_args(["all"]), (None, None))
        self.assertEqual(parse_status_args(["cluster", "421318"]), ("421318", None))
        self.assertIsNotNone(parse_status_args(["cluster", "999999"])[1])
        self.assertIsNotNone(parse_status_args(["421318"])[1])

    def test_refresh_scope_and_legacy_forms_rejection(self):
        scope, start, end, error = parse_refresh_command_args(
            ["cluster", "421318"], date(2026, 8, 25)
        )
        self.assertIsNone(error)
        self.assertEqual(
            scope,
            {
                "mode": "AUTO_CLUSTER",
                "cluster_id": "421318",
                "user": "421318_A",
            },
        )
        self.assertEqual((start, end), (None, None))

        scope, start, end, error = parse_refresh_command_args(
            ["all"], date(2026, 8, 25)
        )
        self.assertEqual(scope["mode"], "AUTO_ALL")
        self.assertEqual((start, end, error), (None, None, None))
        self.assertEqual(scope, {"mode": "AUTO_ALL", "cluster_id": None, "user": None})
        for args in (
            [],
            ["421318"],
            ["full"],
            ["now"],
            ["yesterday"],
            ["2026-08-20", "2026-08-25"],
            ["cluster", "421318", "2026-08-20", "2026-08-25"],
        ):
            with self.subTest(args=args):
                self.assertIsNotNone(parse_refresh_command_args(args, date(2026, 8, 25))[3])


class TestRendering(unittest.TestCase):
    def test_card_contains_fields_and_remaining(self):
        card = format_txn_card(
            {
                "txn_id": 91,
                "cluster_id": "411311",
                "transaction_date": "2026-08-20T09:30:00",
                "amount": 1500000,
                "remarks": "SETOR kasir A",
            },
            remaining=4,
        )
        self.assertIn("#91", card)
        self.assertIn("411311", card)
        self.assertIn("Rp 1.500.000", card)
        self.assertIn("Keterangan: SETOR kasir A", card)
        self.assertIn("Tersisa setelah ini: 4", card)

    def test_done_message_varies_with_remaining(self):
        self.assertIn("Semua transaksi", format_walkthrough_done(0))
        self.assertIn("3 Kredit masih belum ditandai", format_walkthrough_done(3))
        self.assertIn("Ditandai: 4", format_kredit_done(4, 1, 2))

    def test_empty_listing_message(self):
        text = format_nothing_to_classify("2026-08-19", "2026-08-25")
        self.assertIn("19 Agu 2026 - 25 Agu 2026", text)

    def test_refusal_text_mentions_buttons_only(self):
        self.assertIn("Gunakan tombol", REFUSE_TEXT)

    def test_default_window_helper_matches_parser(self):
        start, end = default_window(date(2026, 8, 25))
        self.assertEqual(start, "2026-08-19")
        self.assertEqual(end, "2026-08-25")

    def test_status_block_separates_lifecycle_and_verification(self):
        text = format_status_cluster(
            "421318",
            "421318_A",
            4,
            2,
            {
                "refresh_id": 9,
                "status": "PENDING_REVIEW",
                "verification_status": "MISMATCH",
                "requested_start": "2026-08-20",
                "requested_end": "2026-08-25",
                "checkpoint_as_of_date": "2026-08-11",
                "trigger_source": "telegram-bot",
                "staged_count": 3,
            },
        )
        self.assertIn("Morowali (421318)", text)
        self.assertIn("Pengguna DigiPOS: 421318_A", text)
        self.assertIn("Refresh: Menunggu keputusan", text)
        self.assertIn("Pemicu: telegram-bot", text)
        self.assertIn("Verifikasi: Ada selisih", text)
        self.assertIn("Periode wilayah: 11 Agu 2026 - 25 Agu 2026", text)
        self.assertIn("Menunggu persetujuan: 3", text)
        self.assertEqual(format_status_all(["one", "two"]), "one\n\ntwo")

    def test_partial_approval_explains_held_region(self):
        text = format_review_result("approve", {
            "refresh_id": 42,
            "status": "PENDING_REVIEW",
            "partial": True,
            "inserted": 2,
            "held_clusters": ["411311"],
        })
        self.assertIn("disetujui sebagian", text)
        self.assertIn("Palangkaraya", text)
        self.assertIn("tetap di staging", text)

        details = format_refresh_details({
            "refresh_id": 42,
            "status": "PENDING_REVIEW",
            "refresh_mode": "AUTO_ALL",
            "requested_start": "2026-08-11",
            "requested_end": "2026-08-25",
            "clusters": [
                {"cluster_id": "411311", "status": "MISMATCH", "checkpoint_as_of_date": "2026-08-11"},
                {"cluster_id": "421318", "status": "COMMITTED", "checkpoint_as_of_date": "2026-08-11"},
            ],
        })
        self.assertIn("Sebagian masuk, wilayah lain ditahan", details)
        self.assertIn("wilayah lain tetap di staging", details)

    def test_review_actions_keep_compact_callbacks_and_use_cues(self):
        self.assertEqual(
            build_review_keyboard(42),
                [
                    [("✅ Setujui & masukkan", "r:a:42"), ("❌ Tolak & hapus", "r:r:42")],
                [("ℹ️ Lihat detail", "r:d:42")],
            ],
        )

    def test_status_and_timestamp_renderers_are_indonesian(self):
        self.assertIn("Kredit belum ditandai", format_status(2, 1, None))
        self.assertIn("Ada selisih", format_state("MISMATCH"))
        self.assertEqual(
            format_datetime_label("2026-08-25T03:30:00+00:00"),
            "25 Agu 2026 11:30:00",
        )

    def test_menu_builders_expose_only_guided_actions(self):
        callbacks = [button[1] for row in build_home_keyboard() for button in row]
        self.assertEqual(callbacks, ["m:topup", "m:status", "m:kredit", "m:kreditlist", "m:topupexcel", "m:help"])
        self.assertEqual(
            [button[1] for row in build_topup_keyboard() for button in row],
            [
                "t:c:411311", "t:c:421306", "t:c:421307", "t:c:421315",
                "t:c:421318", "t:c:421320", "t:a", "m:home",
            ],
        )
        self.assertEqual(
            [button[1] for row in build_status_keyboard() for button in row],
            [
                "s:c:411311", "s:c:421306", "s:c:421307", "s:c:421315",
                "s:c:421318", "s:c:421320", "s:a", "m:home",
            ],
        )
        self.assertEqual(build_topup_keyboard()[-1], [("Kembali", "m:home")])
        self.assertEqual(build_status_keyboard()[-1], [("Kembali", "m:home")])
        self.assertEqual(build_kredit_keyboard()[-1], [("Kembali", "m:home")])
        self.assertEqual(build_kredit_report_keyboard()[-1], [("Kembali", "m:home")])
        self.assertEqual(build_topup_export_keyboard()[-1], [("Kembali", "m:home")])
        self.assertEqual(build_kredit_period_keyboard("421318")[0], [
            ("1 hari", "k:c:421318:1"), ("3 hari", "k:c:421318:3")
        ])
        self.assertEqual(build_kredit_report_period_keyboard()[2], [("Rentang custom", "l:a:x")])
        self.assertEqual(
            build_kredit_report_page_keyboard("421318", 1, True)[0],
            [("Sebelumnya", "l:c:421318:p:0"), ("Berikutnya", "l:c:421318:p:2")],
        )
        self.assertEqual(
            build_topup_export_period_keyboard("421318")[0],
            [("1 hari", "e:c:421318:1"), ("3 hari", "e:c:421318:3")],
        )
        self.assertIn("/topupexcel all custom", format_topup_export_custom_prompt())

    def test_kredit_report_includes_outlet_classification(self):
        text = format_kredit_report(
            [{
                "txn_id": 8,
                "cluster_id": "421318",
                "transaction_date": "2026-08-25T09:00:00",
                "amount": 150000,
                "outlet_code": "OUTLET_2",
                "outlet_label": "Outlet 2",
            }],
            "421318",
            "2026-08-25",
            "2026-08-25",
            0,
            False,
        )
        self.assertIn("Outlet 2", text)
        self.assertIn("Rp 150.000", text)
        self.assertIn("Rentang custom", format_kredit_report_custom_prompt("421318"))


if __name__ == "__main__":
    unittest.main()
