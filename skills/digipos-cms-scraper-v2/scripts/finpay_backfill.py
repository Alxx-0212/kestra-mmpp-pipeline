#!/usr/bin/env python3
"""Finpay backfill task.

For each cluster: download every date from start through end, then post those files to
Kestra chronologically, waiting for each execution to finish before posting the next.
Default dry_run=false, so Kestra target sheet/data WILL be updated.
"""

import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

DEFAULT_USERS = ["411311_A", "421306_A", "421307_A", "421315_A", "421318_A", "421320_A"]
DEFAULT_START = "2026-06-01"
DEFAULT_OUTPUT_DIR = "/home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox"
BASE_DIR = "/home/mmpp/.hermes/profiles/alex/skills"
DOWNLOAD_SCRIPT = f"{BASE_DIR}/digipos-cms-scraper/scripts/download_digipos.py"
KESTRA_SCRIPT = f"{BASE_DIR}/data-pipeline/kestra-finpay-pipeline/scripts/trigger_kestra.py"
DEFAULT_KESTRA_URL = "http://localhost:8080/api/v1/executions/finance.finpay/finpay_daily_pipeline"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_dates(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run_cmd(cmd, *, dry_run_command=False):
    printable = " ".join(cmd)
    print(f"RUN: {printable}", flush=True)
    if dry_run_command:
        return 0
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end="", flush=True)
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr, flush=True)
    return proc.returncode


def expected_csv(output_dir: str, user: str, d: date) -> str:
    cluster = user.replace("_A", "")
    ddmmyyyy = d.strftime("%d-%m-%Y")
    name = f"finpay-{cluster}({ddmmyyyy}to{ddmmyyyy}).csv"
    return os.path.join(output_dir, cluster, name)


def csv_data_row_count(path: str) -> int:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="finpay-backfill: per cluster download all dates, then post to Kestra sequentially")
    parser.add_argument("--start", default=DEFAULT_START, help="Start date YYYY-MM-DD (default: 2026-06-01)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--users", nargs="+", default=DEFAULT_USERS)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", default="false", choices=["true", "false"], help="Pass through to Kestra. false updates sheet/data. Default: false")
    parser.add_argument("--kestra-url", default=DEFAULT_KESTRA_URL, help="Kestra execution API URL for target flow")
    parser.add_argument("--no-post", action="store_true", help="Download only; do not trigger Kestra")
    parser.add_argument("--command-dry-run", action="store_true", help="Print commands only; do not run downloads/posts")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Kestra poll interval when posting")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Max wait for each Kestra execution")
    parser.add_argument("--post-attempts", type=int, default=3, help="Retry failed Kestra execution for same CSV before failing cluster")
    parser.add_argument("--post-retry-sleep", type=int, default=30, help="Seconds to wait before retrying failed Kestra execution")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else (date.today() - timedelta(days=1))
    if end < start:
        raise SystemExit(f"Invalid range: end {end} before start {start}")

    print("TASK: finpay-backfill", flush=True)
    print(f"Date range: {start} -> {end}", flush=True)
    print(f"Users: {', '.join(args.users)}", flush=True)
    print(f"Output: {args.output_dir}", flush=True)
    print(f"Kestra dry_run: {args.dry_run}", flush=True)
    print(f"Kestra URL: {args.kestra_url}", flush=True)
    print(f"No post: {args.no_post}", flush=True)
    print("Order: cluster ascending as configured; within each cluster date ascending; wait each Kestra execution", flush=True)
    print("", flush=True)

    os.makedirs(args.output_dir, exist_ok=True)
    ok_download = ok_post = skipped = failed = 0

    for user in args.users:
        cluster = user.replace("_A", "")
        print(f"===== CLUSTER {cluster} ({user}) =====", flush=True)
        downloaded_files = []
        cluster_failed = False

        # Step 1: login/download this cluster for every date before moving to next cluster.
        for d in iter_dates(start, end):
            dstr = d.strftime("%Y-%m-%d")
            print(f"--- DOWNLOAD {cluster} / {dstr} ---", flush=True)
            csv_path = expected_csv(args.output_dir, user, d)
            if not args.command_dry_run and os.path.exists(csv_path):
                os.remove(csv_path)
                print(f"Removed stale target before download: {csv_path}", flush=True)
            dl_cmd = ["python3", DOWNLOAD_SCRIPT, "--users", user, "--date", dstr, "--output-dir", args.output_dir]
            rc = run_cmd(dl_cmd, dry_run_command=args.command_dry_run)
            if rc != 0:
                print(f"DOWNLOAD FAILED: {dstr} {user} rc={rc}", flush=True)
                failed += 1
                cluster_failed = True
                break

            csv_path = expected_csv(args.output_dir, user, d)
            if args.command_dry_run:
                downloaded_files.append((d, csv_path))
                continue
            if not os.path.exists(csv_path):
                print(f"MISSING REQUIRED CSV: {csv_path}", flush=True)
                failed += 1
                cluster_failed = True
                break

            row_count = csv_data_row_count(csv_path)
            if row_count <= 0:
                print(f"INVALID REQUIRED CSV (HEADER-ONLY): {csv_path}", flush=True)
                os.remove(csv_path)
                failed += 1
                cluster_failed = True
                break

            ok_download += 1
            print(f"CSV validated before post: {csv_path} rows={row_count}", flush=True)
            downloaded_files.append((d, csv_path))

        expected_dates = list(iter_dates(start, end))
        downloaded_dates = [d for d, _ in downloaded_files]
        if cluster_failed or downloaded_dates != expected_dates:
            missing = [d.strftime("%Y-%m-%d") for d in expected_dates if d not in downloaded_dates]
            print(f"CLUSTER {cluster} INCOMPLETE — no post allowed. missing={missing}", flush=True)
            failed += 0 if cluster_failed else 1
            continue

        # Step 2: post this cluster's downloaded files chronologically, waiting each one.
        if args.no_post:
            for _, csv_path in downloaded_files:
                print(f"POST SKIPPED (--no-post): {csv_path}", flush=True)
            continue

        for d, csv_path in sorted(downloaded_files, key=lambda x: x[0]):
            dstr = d.strftime("%Y-%m-%d")
            print(f"--- POST {cluster} / {dstr} ---", flush=True)
            post_cmd = [
                "python3", KESTRA_SCRIPT,
                "--url", args.kestra_url,
                "--file", csv_path,
                "--dry-run", args.dry_run,
                "--wait",
                "--poll-seconds", str(args.poll_seconds),
                "--timeout-seconds", str(args.timeout_seconds),
            ]
            rc = 1
            for post_attempt in range(1, args.post_attempts + 1):
                print(f"POST ATTEMPT {post_attempt}/{args.post_attempts}: {cluster} / {dstr}", flush=True)
                rc = run_cmd(post_cmd, dry_run_command=args.command_dry_run)
                if rc == 0:
                    break
                print(f"POST ATTEMPT FAILED: {dstr} {user} attempt={post_attempt} rc={rc}", flush=True)
                if post_attempt < args.post_attempts and not args.command_dry_run:
                    time.sleep(args.post_retry_sleep)
            if rc != 0:
                print(f"POST FAILED AFTER RETRIES: {dstr} {user} rc={rc}", flush=True)
                failed += 1
                break
            ok_post += 1

    print("", flush=True)
    print("SUMMARY finpay-backfill", flush=True)
    print(f"downloads_ok={ok_download}", flush=True)
    print(f"posts_ok={ok_post}", flush=True)
    print(f"skipped_no_data={skipped}", flush=True)
    print(f"failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
