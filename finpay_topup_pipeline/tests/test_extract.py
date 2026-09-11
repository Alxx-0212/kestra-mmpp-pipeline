import csv
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from finpay_topup_pipeline.extract import _merge_csv_files, run_extract


class TestChunkedExtraction(unittest.TestCase):
    def test_merge_strips_bom_before_quoted_no_header(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.csv"
            destination = Path(directory) / "merged.csv"
            source.write_text(
                '\ufeff"No",Transaction Date,Sender,Receiver,Transaction Type,Amount,Currency,Remarks\n'
                '1,2026-01-01 10:00:00,s,r,Kredit,100,IDR,ok\n',
                encoding="utf-8",
            )

            _merge_csv_files([source], destination)

            with destination.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(next(csv.reader(handle))[0], "No")

    def test_uses_per_user_starts_and_retries_only_failed_chunks(self):
        calls = []
        attempts = {}

        def fake_run(cmd, **kwargs):
            start = cmd[cmd.index("--start") + 1]
            end = cmd[cmd.index("--end") + 1]
            user = cmd[cmd.index("--users") + 1]
            output_dir = Path(cmd[cmd.index("--output-dir") + 1])
            key = (user, start, end)
            attempts[key] = attempts.get(key, 0) + 1
            calls.append(key)
            if user == "411311_A" and start == "2026-01-01" and attempts[key] == 1:
                return CompletedProcess(cmd, 1, "", "source timeout")
            target = output_dir / user[:-2]
            target.mkdir(parents=True, exist_ok=True)
            with (target / "part.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["No", "Transaction Date", "Sender", "Receiver", "Transaction Type", "Amount", "Currency", "Remarks"])
                writer.writerow(["1", f"{start} 10:00:00", "s", "r", "Kredit", "100", "IDR", "chunk"])
            return CompletedProcess(cmd, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, patch("finpay_topup_pipeline.extract.subprocess.run", side_effect=fake_run):
            result = run_extract(
                {"411311": "2026-01-01", "421306": "2026-01-03"},
                "2026-01-10",
                users=["411311_A", "421306_A"],
                output_dir=directory,
                max_attempts=2,
            )
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(calls.count(("411311_A", "2026-01-01", "2026-01-03")), 2)
            self.assertEqual(calls.count(("411311_A", "2026-01-04", "2026-01-06")), 1)
            self.assertEqual(calls.count(("411311_A", "2026-01-07", "2026-01-09")), 1)
            self.assertEqual(calls.count(("411311_A", "2026-01-10", "2026-01-10")), 1)
            self.assertEqual(calls.count(("421306_A", "2026-01-03", "2026-01-05")), 1)
            self.assertEqual(calls.count(("421306_A", "2026-01-06", "2026-01-08")), 1)
            self.assertEqual(calls.count(("421306_A", "2026-01-09", "2026-01-10")), 1)
            self.assertEqual(len(list(Path(directory).glob("*/finpay-topup-*.csv"))), 2)

            retry = run_extract(
                {"411311": "2026-01-01", "421306": "2026-01-03"},
                "2026-01-10",
                users=["411311_A", "421306_A"],
                output_dir=directory,
                max_attempts=2,
            )
            self.assertEqual(retry["returncode"], 0)
            self.assertEqual(len(calls), 8)

    def test_failed_user_does_not_discard_successful_user_artifact(self):
        calls = []

        def fake_run(cmd, **kwargs):
            user = cmd[cmd.index("--users") + 1]
            calls.append(user)
            if user == "411311_A":
                return CompletedProcess(cmd, 1, "", "timeout")
            output_dir = Path(cmd[cmd.index("--output-dir") + 1]) / user[:-2]
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "part.csv").write_text("header\nrow\n", encoding="utf-8")
            return CompletedProcess(cmd, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, patch("finpay_topup_pipeline.extract.subprocess.run", side_effect=fake_run):
            result = run_extract(
                "2026-01-01",
                "2026-01-01",
                users=["411311_A", "421306_A"],
                output_dir=directory,
                max_attempts=1,
            )
            self.assertEqual(result["returncode"], 1)
            self.assertIn("421306_A", calls)
            self.assertTrue(list(Path(directory).glob("421306/finpay-topup-*.csv")))

    def test_failure_keeps_downloader_diagnostic_when_stderr_is_empty(self):
        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 1, "[FAIL] 421315_A: server timeout", "")

        with tempfile.TemporaryDirectory() as directory, patch(
            "finpay_topup_pipeline.extract.subprocess.run", side_effect=fake_run
        ):
            result = run_extract(
                "2026-08-11",
                "2026-08-13",
                users=["421315_A"],
                output_dir=directory,
                max_attempts=1,
            )

        self.assertIn("server timeout", result["stderr"])

    def test_failed_multi_day_chunk_falls_back_to_one_day_requests(self):
        calls = []

        def fake_run(cmd, **kwargs):
            start = cmd[cmd.index("--start") + 1]
            end = cmd[cmd.index("--end") + 1]
            calls.append((start, end))
            if start == "2026-08-11" and end == "2026-08-13":
                return CompletedProcess(cmd, 1, "", "timeout")
            output_dir = Path(cmd[cmd.index("--output-dir") + 1]) / "421315"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "part.csv").write_text("header\nrow\n", encoding="utf-8")
            return CompletedProcess(cmd, 0, "ok", "")

        final_files = []
        with tempfile.TemporaryDirectory() as directory, patch(
            "finpay_topup_pipeline.extract.subprocess.run", side_effect=fake_run
        ):
            result = run_extract(
                "2026-08-11",
                "2026-08-13",
                users=["421315_A"],
                output_dir=directory,
                max_attempts=1,
            )
            final_files = list(Path(directory).glob("421315/finpay-topup-*.csv"))

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(calls[0], ("2026-08-11", "2026-08-13"))
        self.assertEqual(
            calls[1:],
            [("2026-08-11", "2026-08-11"), ("2026-08-12", "2026-08-12"), ("2026-08-13", "2026-08-13")],
        )
        self.assertTrue(final_files)
