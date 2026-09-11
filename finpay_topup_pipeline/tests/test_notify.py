import unittest
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.notify import (
    format_cycle_alert,
    format_flow_failure_alert,
    review_keyboard,
    send_telegram_message,
)


class TestFormatCycleAlert(unittest.TestCase):
    def test_numeric_alert_requires_review_and_reports_provenance(self):
        verification = {
            "status": "VERIFIED",
            "clusters": {
                "411311": {
                    "status": "VERIFIED",
                    "computed_saldo": 130000,
                    "bucket_topup_value": 130000,
                    "verification_diff": 0,
                    "staged_rows": 7,
                    "downloaded_rows": 200,
                    "calculation_row_count": 137,
                    "source_transaction_min_at": "2026-08-11T09:00:00+00:00",
                    "source_transaction_max_at": "2026-08-11T10:00:00+00:00",
                    "bucket_snapshot_at": "2026-08-11T11:00:00+00:00",
                    "calculation_cutoff_at": "2026-08-11T11:00:00+00:00",
                    "calculated_at": "2026-08-11T11:00:01+00:00",
                    "matching_row_number": 137,
                    "matching_type": "TRANSACTION",
                    "matching_transaction_id": 9,
                    "matching_transaction_at": "2026-08-11T10:00:00+00:00",
                    "matching_running_saldo": 130000,
                    "matching_row": {
                        "txn_id": 9,
                        "cluster_id": "411311",
                        "transaction_date": "2026-08-11T10:00:00+00:00",
                        "sender": "A&B<sender>\"",
                        "receiver": "receiver",
                        "transaction_type": "Kredit",
                        "amount": 1000,
                        "currency": "IDR",
                        "remarks": "matching row",
                        "source_file": "unit.csv",
                        "row_hash": "hash-9",
                    },
                }
            },
        }
        text = format_cycle_alert(
            42, "2026-08-11", "2026-08-12", verification,
            staged=7,
        )
        self.assertIn("FINPAY TOP-UP", text)
        self.assertIn("<b>FINPAY TOP-UP</b>", text)
        self.assertIn("<b>Palangkaraya</b> <code>411311</code>", text)
        self.assertIn("<b>Status:</b> <code>Menunggu persetujuan</code>", text)
        self.assertIn("<b>Data diunduh:</b> <code>200 baris</code>", text)
        self.assertIn("<b>Baris dihitung:</b> <code>137</code>", text)
        self.assertIn("<b>Saldo berjalan:</b> <code>Rp 130.000</code>", text)
        self.assertIn("<b>CMS BUCKET:</b> <code>Rp 130.000</code>", text)
        self.assertIn("<b>Selisih:</b> <code>Rp 0</code>", text)
        self.assertIn("<b>Transaksi terakhir dihitung:</b> <code>11 Agu 2026 18:00:00</code>", text)
        self.assertIn("<b>CMS diambil:</b> <code>11 Agu 2026 19:00:00</code>", text)
        self.assertIn("<b>Cocok dengan CMS pada:</b> <code>11 Agu 2026 19:00:00</code>", text)
        self.assertIn("<b>Saldo cocok pada baris:</b> <code>137 dari 137</code>", text)
        self.assertIn("<pre>", text)
        self.assertIn("A&amp;B&lt;sender&gt;&quot;", text)
        self.assertNotIn("A&B<sender>\"", text)
        self.assertIn("<b>Baris ditahan:</b> <code>7</code>", text)
        self.assertNotIn("Total baris ditahan", text)
        self.assertIn("Data belum masuk ledger utama", text)

    def test_multi_cluster_alert_shows_checkpoint_specific_periods(self):
        verification = {
            "status": "VERIFIED",
            "clusters": {
                cluster_id: {
                    "status": "VERIFIED",
                    "computed_saldo": 1000,
                    "bucket_topup_value": 1000,
                    "verification_diff": 0,
                }
                for cluster_id in ("411311", "421315")
            },
        }
        text = format_cycle_alert(
            42,
            "2026-08-11",
            "2026-09-10",
            verification,
            checkpoint_dates={
                "411311": "2026-09-04",
                "421315": "2026-08-11",
            },
        )
        self.assertIn("Berbeda per wilayah", text)
        self.assertIn("Periode wilayah", text)
        self.assertIn("4 Sep 2026 - 10 Sep 2026", text)
        self.assertIn("11 Agu 2026 - 10 Sep 2026", text)

    def test_review_keyboard_uses_compact_callbacks(self):
        keyboard = review_keyboard(42)
        self.assertEqual(keyboard["inline_keyboard"][0], [
            {"text": "✅ Setujui & masukkan", "callback_data": "r:a:42"},
            {"text": "❌ Tolak & hapus", "callback_data": "r:r:42"},
        ])
        self.assertEqual(keyboard["inline_keyboard"][1], [
            {"text": "ℹ️ Lihat detail", "callback_data": "r:d:42"},
        ])

    def test_match_fallbacks_do_not_fabricate_transaction_rows(self):
        for match_type, expected in (
            ("CHECKPOINT", "Checkpoint sebelum baris 1"),
            ("CUTOFF", "Batas perhitungan CMS"),
        ):
            with self.subTest(match_type=match_type):
                text = format_cycle_alert(1, "2026-08-11", "2026-08-11", {
                    "status": "VERIFIED",
                    "clusters": {
                        "421318": {
                            "status": "VERIFIED",
                            "computed_saldo": 1000,
                            "bucket_topup_value": 1000,
                            "verification_diff": 0,
                            "calculation_row_count": 0,
                            "matching_type": match_type,
                        }
                    },
                })
                self.assertIn(expected, text)
                self.assertNotIn("<pre>", text)

        mismatch = format_cycle_alert(2, "2026-08-11", "2026-08-11", {
            "status": "MISMATCH",
            "clusters": {
                "421318": {
                    "status": "MISMATCH",
                    "computed_saldo": 900,
                    "bucket_topup_value": 1000,
                    "verification_diff": -100,
                    "calculation_row_count": 1,
                }
            },
        })
        self.assertIn("Tidak ada baris yang cocok", mismatch)
        self.assertIn("Wilayah berselisih tetap ditahan", mismatch)
        self.assertNotIn("<pre>", mismatch)

    def test_failed_alert_reports_purge(self):
        verification = {
            "status": "VERIFY_FAILED",
            "clusters": {
                "421318": {
                    "computed_saldo": 150000,
                    "bucket_topup_value": 140000,
                    "verification_diff": 10000.0,
                }
            },
        }
        text = format_cycle_alert(
            43, "2026-08-11", "2026-08-12", verification, purged=9, attempt=2,
        )
        self.assertIn("<b>Status:</b> <code>Gagal diverifikasi</code>", text)
        self.assertIn("<b>Baris dihapus:</b> <code>9</code>", text)
        self.assertIn("Ledger utama tidak berubah", text)

    def test_partial_verification_failure_can_still_be_reviewed(self):
        text = format_cycle_alert(
            45,
            "2026-08-11",
            "2026-08-12",
            {
                "status": "VERIFY_FAILED",
                "clusters": {
                    "411311": {"status": "VERIFIED", "computed_saldo": 1000},
                    "421318": {"status": "VERIFY_FAILED", "error": "CMS unavailable"},
                },
            },
            staged=4,
        )
        self.assertIn("Sebagian terverifikasi; wilayah lain ditahan", text)
        self.assertIn("Hanya wilayah Terverifikasi yang masuk ledger utama", text)
        self.assertIn("CMS unavailable", text)

    def test_skipped_status_mentions_no_action(self):
        text = format_cycle_alert(44, "2026-08-11", "2026-08-12", {"status": "SKIPPED"})
        self.assertIn("Tidak ada perubahan", text)


class TestFlowFailureAlert(unittest.TestCase):
    def test_contains_execution_coordinates(self):
        text = format_flow_failure_alert("exec-1", "finance.finpay", "finpay_topup_pipeline_v1")
        self.assertIn("<b>Eksekusi:</b> <code>exec-1</code>", text)


class TestSendTelegramMessage(unittest.TestCase):
    def test_missing_configuration_is_noop_false(self):
        self.assertFalse(send_telegram_message("", "", "hello"))
        self.assertFalse(send_telegram_message(None, "123", "hello"))

    @patch("finpay_topup_pipeline.notify.requests.post")
    def test_sends_json_to_bot_api(self, post):
        post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
        sent = send_telegram_message("tok", "123", "hello")
        self.assertTrue(sent)
        post.assert_called_once_with(
            "https://api.telegram.org/bottok/sendMessage",
            json={"chat_id": "123", "text": "hello"},
            timeout=30,
        )

    @patch("finpay_topup_pipeline.notify.requests.post")
    def test_review_keyboard_is_sent_as_bot_api_markup(self, post):
        post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
        markup = {"inline_keyboard": [[{"text": "Approve", "callback_data": "r:a:7"}]]}
        self.assertTrue(send_telegram_message("tok", "123", "review", reply_markup=markup))
        self.assertEqual(post.call_args.kwargs["json"]["reply_markup"], markup)

    @patch("finpay_topup_pipeline.notify.requests.post")
    def test_html_parse_mode_is_explicit(self, post):
        post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
        self.assertTrue(send_telegram_message("tok", "123", "<b>review</b>", parse_mode="HTML"))
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")

    @patch("finpay_topup_pipeline.notify.requests.post")
    def test_message_limit_is_rejected_before_http(self, post):
        with self.assertRaises(ValueError):
            send_telegram_message("tok", "123", "x" * 4097)
        post.assert_not_called()

    def test_large_multi_cluster_alert_stays_within_telegram_limit(self):
        verification = {
            "status": "VERIFIED",
            "clusters": {
                str(411300 + index): {
                    "status": "VERIFIED",
                    "computed_saldo": 1000,
                    "bucket_topup_value": 1000,
                    "verification_diff": 0,
                    "calculation_row_count": 1,
                    "matching_row_number": 1,
                    "matching_type": "TRANSACTION",
                    "matching_transaction_at": "2026-08-11T11:00:00+00:00",
                    "matching_running_saldo": 1000,
                    "matching_row": {
                        "txn_id": index,
                        "cluster_id": str(411300 + index),
                        "transaction_date": "2026-08-11T11:00:00+00:00",
                        "sender": "A" * 1000,
                        "receiver": "B" * 1000,
                        "transaction_type": "Kredit",
                        "amount": 1000,
                        "currency": "IDR",
                        "remarks": "R" * 1000,
                        "source_file": "unit.csv",
                        "row_hash": "hash",
                    },
                }
                for index in range(1, 7)
            },
        }

        text = format_cycle_alert(7, "2026-08-11", "2026-08-11", verification)

        self.assertLessEqual(len(text), 4096)
        self.assertTrue(text.endswith("</b>"))

    @patch("finpay_topup_pipeline.notify.requests.post")
    def test_http_error_raises(self, post):
        post.return_value.raise_for_status.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            send_telegram_message("tok", "123", "hello")


if __name__ == "__main__":
    unittest.main()
