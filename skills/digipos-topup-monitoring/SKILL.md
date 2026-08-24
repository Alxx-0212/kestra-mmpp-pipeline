---
name: digipos-topup-monitoring
description: Download DigiPOS CMS (digipos-cms.finpay.id) Monitoring Top Up data per cluster for a custom date range via the JSON API, output CSV for the Finpay Top Up Kestra pipeline. Trigger-based (finance-initiated), not scheduled.
version: 1.0.0
tags: [digipos, finpay, topup, monitoring-topup, csv, kestra, trigger]
---

# DigiPOS CMS — Monitoring Top Up Download (Trigger-Based)

Download **Monitoring Top Up** data from DigiPOS CMS for one or more clusters
over a **custom date range**, for hand-off to the Finpay Top Up Kestra workflow.

This is the **Top Up** extract. It is NOT the same as `digipos-cms-scraper`
(which pulls `monitoring-riwayat` / Riwayat Saldo Transaksi). Different endpoint,
different columns, different downstream.

## Target

- Login: `https://digipos-cms.finpay.id/login`
- Login endpoint: `POST /login-post`
- Data: `Deposit → Monitoring Top Up`
- Data API: `POST /deposit/monitoring-topup-detail` (DataTables JSON — **no browser needed**)

## Why JSON API, not Playwright

The Top Up page exposes a clean server-side JSON endpoint. Unlike
`monitoring-riwayat` (which needs Playwright + jQuery + Excel export), Top Up
can be pulled with `requests.Session`. This is more reliable and faster.

Confirmed live response shape:

```json
{
  "statusCode": "00",
  "recordsTotal": 162,
  "recordsFiltered": 162,
  "data": [
    {
      "trxdate": "2026-08-13 17:15:05",
      "sender": "628115209723",
      "receiver": "082250519745",
      "tipe": "Debit",
      "amount": "255150",
      "remarks": "Top Up balance via SF 255150"
    }
  ]
}
```

Row keys: `trxdate, sender, receiver, tipe, amount, remarks`.

## Required Users / Clusters

| Username | cluster_id |
|----------|------------|
| 411311_A | 411311 |
| 421306_A | 421306 |
| 421307_A | 421307 |
| 421315_A | 421315 |
| 421318_A | 421318 |
| 421320_A | 421320 |

## Output

Directory:
```text
finpay-topup-inbox/{cluster_id}/
```

Filename:
```text
finpay-topup-{cluster_id}(DD-MM-YYYYtoDD-MM-YYYY).csv
```

Columns:
```text
No, Transaction Date, Sender, Receiver, Transaction Type, Amount, Currency, Remarks
```

`Currency` is always `IDR`. `Sender`/`Receiver` are phone numbers — written as
plain strings so Excel/CSV keeps full digits (no scientific notation). Do NOT
cast them to numeric downstream.

## Usage

Single date (one cluster):
```bash
python3 scripts/download_topup.py --users 421306_A --start 2026-08-13 --end 2026-08-13
```

Date range, all clusters:
```bash
python3 scripts/download_topup.py --start 2026-08-01 --end 2026-08-13
```

Custom output dir:
```bash
python3 scripts/download_topup.py --start 2026-08-01 --end 2026-08-13 \
  --output-dir /path/to/inbox
```

Password (avoid logging): pass `--password` at runtime or set
`DIGIPOS_PASSWORD` in the process environment. Do not store passwords in logs
or source files.

## Validation Rules (fail loudly)

- Login `statusCode` must be `00`/`000`. Else hard fail.
- Top Up query `statusCode` must be `00`/`000` and `data` present.
- Row count fetched must reach `recordsTotal`. If loop under-collects, retry.
- If a user fails after `--max-attempts` (default 3), the run exits non-zero
  but other users' files are kept. Rerun only failed users.
- 0 rows for a date is VALID only if `recordsTotal == 0`. A query error is not
  "no data".

## Integration (Kestra, trigger-based)

This skill is invoked on demand (finance triggers it), not on a schedule:

1. Finance sets date range + clusters in a Google Sheet control board.
2. A Hermes poller (or manual trigger) detects the request.
3. Hermes runs this skill with the requested `--start --end --users`.
4. CSVs land in `finpay-topup-inbox/{cluster_id}/`.
5. Hermes (or the poller) POSTs each CSV to the Kestra Top Up flow
   (`finpay_topup_pipeline`) at `http://localhost:8081/api/v1/executions/...`.
6. Sheet row updated to DONE/ERROR with summary.

The server is behind a VPN, so Google Sheets cannot webhook INTO Kestra. Use the
**poll (egress) pattern**: Hermes/server pulls the Sheet, never inbound.

## Differences vs digipos-cms-scraper

| | digipos-cms-scraper | digipos-topup-monitoring |
|---|---|---|
| Menu | monitoring-riwayat | monitoring-topup-detail |
| Method | Playwright browser | requests JSON API |
| Format | Excel (.xlsx) | CSV |
| Columns | saldo movement (No, Saldo Awal, Kredit, Debet, Saldo Akhir, Nomor RS...) | topup (trxdate, sender, receiver, tipe, amount, remarks) |
| Trigger | scheduled (cron) | trigger-based (finance) |
| Downstream | Kestra v5 riwayat saldo | Kestra Top Up pipeline |

## Pitfalls

- `requests` library required (`pip install requests` if missing).
- CSRF `_token` is re-fetched from the detail page before each query; reuse the
  logged-in session across all users.
- Large date ranges: backend honors `length=20000`; pagination loop handles
  `recordsTotal > fetched`.
- `RemoteDisconnected` under load: the script retries with backoff per user.
- Keep the password in the runtime secret store; do not add it to the script,
  workflow YAML, CSV output, or command output.
