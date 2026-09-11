#!/usr/bin/env python3
"""Download DigiPOS CMS Monitoring Top Up data per cluster with custom date range.

Site:   https://digipos-cms.finpay.id
Menu:   Deposit -> Monitoring Top Up
API:    POST /deposit/monitoring-topup-detail  (DataTables JSON, no browser needed)

This is the active Top Up extract, not the historical Riwayat Saldo Transaksi
extract. It uses a different endpoint and column contract.

Output: one CSV per user under OUTPUT_DIR/{cluster_id}/, named:
    finpay-topup-{cluster_id}(DD-MM-YYYYtoDD-MM-YYYY).csv

Use for the Finpay Top Up Kestra pipeline (trigger-based, not scheduled).

Columns written:
    No, Transaction Date, Sender, Receiver, Transaction Type, Amount, Currency, Remarks

NOTE on scientific notation: sender/receiver are phone numbers. We write them
as quoted strings so Excel/CSV keeps full digits. Do NOT let downstream convert
to numeric.
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime

import requests

BASE_URL = "https://digipos-cms.finpay.id"
DEFAULT_USERS = ["411311_A", "421306_A", "421307_A", "421315_A", "421318_A", "421320_A"]
USER_BY_CLUSTER = {username[:-2]: username for username in DEFAULT_USERS}
OUTPUT_DIR = os.environ.get("FINPAY_TOPUP_OUTPUT_DIR", "finpay-topup-inbox")

CSV_COLUMNS = [
    "No",
    "Transaction Date",
    "Sender",
    "Receiver",
    "Transaction Type",
    "Amount",
    "Currency",
    "Remarks",
]


def cluster_id(username: str) -> str:
    return username[:-2] if username.endswith("_A") else username


def normalize_username(value: str) -> str:
    """Accept the documented username or its six-digit cluster ID."""
    value = str(value).strip()
    if value in DEFAULT_USERS:
        return value
    if value in USER_BY_CLUSTER:
        return USER_BY_CLUSTER[value]
    raise ValueError(f"Unsupported FinPay cluster/user: {value!r}")


def dmy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


def target_path_for(username: str, start_str: str, end_str: str, output_dir: str) -> str:
    cid = cluster_id(username)
    d1 = dmy(start_str)
    d2 = dmy(end_str)
    return os.path.join(output_dir, cid, f"finpay-topup-{cid}({d1}to{d2}).csv")


def _get_token(session: requests.Session) -> str | None:
    r = session.get(f"{BASE_URL}/login", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="_token"[^>]*value="([^"]+)"', r.text)
    return m.group(1) if m else None


def login(session: requests.Session, username: str, password: str) -> dict:
    token = _get_token(session)
    if not token:
        raise RuntimeError("No CSRF _token on login page")
    r = session.post(
        f"{BASE_URL}/login-post",
        data={"_token": token, "username": username, "password": password},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": token,
            "Referer": f"{BASE_URL}/login",
        },
        timeout=30,
    )
    r.raise_for_status()
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Login did not return JSON (status {r.status_code})")
    if j.get("statusCode") not in ("00", "000"):
        raise RuntimeError(f"Login failed: {j.get('statusDesc')} (code {j.get('statusCode')})")
    return j


def _detail_token(session: requests.Session) -> str:
    r = session.get(f"{BASE_URL}/deposit/monitoring-topup-detail", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="_token"[^>]*value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("No CSRF _token on Monitoring Top Up page")
    return m.group(1)


def fetch_topup(
    session: requests.Session,
    username: str,
    start_str: str,
    end_str: str,
    token: str,
    max_rows: int = 20000,
) -> list[dict]:
    """Fetch all Top Up rows for one account in a date range."""
    rows: list[dict] = []
    start = 0
    page = 1
    while True:
        r = session.post(
            f"{BASE_URL}/deposit/monitoring-topup-detail",
            data={
                "startDate": start_str,
                "endDate": end_str,
                "trxType": "",
                "adNo": username,
                "start": start,
                "length": max_rows,
                "page": page,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-TOKEN": token,
                "Referer": f"{BASE_URL}/deposit/monitoring-topup-detail",
            },
            timeout=60,
        )
        r.raise_for_status()
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"Top Up detail not JSON (status {r.status_code}): {r.text[:200]}")
        if j.get("statusCode") not in ("00", "000"):
            raise RuntimeError(f"Top Up query failed: {j.get('statusDesc')} (code {j.get('statusCode')})")
        if "data" not in j or not isinstance(j["data"], list):
            raise RuntimeError("Top Up detail response has no list-valued data field")
        data = j["data"]
        rows.extend(data)
        try:
            total = int(j["recordsTotal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Top Up detail response has invalid recordsTotal") from exc
        if total < 0:
            raise RuntimeError(f"Top Up detail response has negative recordsTotal: {total}")
        if len(rows) >= total:
            break
        if len(data) == 0:
            raise RuntimeError(
                f"Top Up pagination stopped at {len(rows)} rows but recordsTotal is {total}"
            )
        start += len(data)
        page += 1
        time.sleep(0.5)
    if len(rows) != total:
        raise RuntimeError(f"Top Up pagination fetched {len(rows)} rows; expected {total}")
    return rows


def rows_to_csv(path: str, rows: list[dict]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(CSV_COLUMNS)
        for i, rec in enumerate(rows, start=1):
            # Quote phone-like fields so Excel keeps full digits
            sender = str(rec.get("sender") or "")
            receiver = str(rec.get("receiver") or "")
            amount = str(rec["amount"]) if rec.get("amount") is not None else ""
            w.writerow([
                i,
                rec.get("trxdate") or "",
                sender,
                receiver,
                rec.get("tipe") or "",
                amount,
                "IDR",
                rec.get("remarks") or "",
            ])
    return len(rows)


def download_for_user(session, username, start_str, end_str, output_dir, max_attempts=3):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            tok = _detail_token(session)
            rows = fetch_topup(session, username, start_str, end_str, tok or "")
            path = target_path_for(username, start_str, end_str, output_dir)
            n = rows_to_csv(path, rows)
            print(f"  [OK] {username}: {n} rows -> {path}", flush=True)
            return path, n
        except Exception as e:
            last_err = e
            print(f"  [retry {attempt}/{max_attempts}] {username}: {e}", flush=True)
            time.sleep(3 * attempt)
    raise RuntimeError(f"{username} failed after {max_attempts} attempts: {last_err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download DigiPOS CMS Monitoring Top Up per cluster")
    ap.add_argument("--users", nargs="+", default=DEFAULT_USERS, help="Usernames (default: all 6)")
    ap.add_argument(
        "--password",
        default=os.environ.get("DIGIPOS_PASSWORD"),
        help="DigiPOS password; may be supplied through DIGIPOS_PASSWORD (never log it)",
    )
    ap.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    ap.add_argument("--output-dir", default=OUTPUT_DIR)
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()

    # Validate dates
    try:
        datetime.strptime(args.start, "%Y-%m-%d")
        datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError as e:
        print(f"ERROR bad date: {e}", file=sys.stderr)
        return 2
    if args.start > args.end:
        print("ERROR end date must not be earlier than start date", file=sys.stderr)
        return 2
    if not args.password:
        print("ERROR DigiPOS password is required via --password or DIGIPOS_PASSWORD", file=sys.stderr)
        return 2
    if args.max_attempts < 1:
        print("ERROR max-attempts must be at least 1", file=sys.stderr)
        return 2
    try:
        users = [normalize_username(value) for value in args.users]
    except ValueError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    print(f"Top Up download: {args.start}..{args.end} users={users}", flush=True)
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    login(s, users[0], args.password)  # one login session reused for all users

    results = {}
    for user in users:
        try:
            path, n = download_for_user(
                s, user, args.start, args.end, args.output_dir, args.max_attempts
            )
            results[user] = {"path": path, "rows": n, "status": "ok"}
        except Exception as e:
            print(f"  [FAIL] {user}: {e}", flush=True)
            results[user] = {"status": "error", "error": str(e)}

    # Summary
    print("\n=== SUMMARY ===", flush=True)
    ok = sum(1 for v in results.values() if v["status"] == "ok")
    for user, v in results.items():
        if v["status"] == "ok":
            print(f"  {user}: {v['rows']} rows", flush=True)
        else:
            print(f"  {user}: ERROR {v['error']}", flush=True)
    print(f"Done: {ok}/{len(users)} users succeeded", flush=True)
    return 0 if ok == len(users) else 1


if __name__ == "__main__":
    raise SystemExit(main())
