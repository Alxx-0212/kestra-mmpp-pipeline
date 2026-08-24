# FinPay Top Up workflow and Superset reporting

This package downloads DigiPOS Monitoring Top Up rows, stores immutable
transaction facts in PostgreSQL, calculates a running saldo, and exposes
read-only reporting views for Apache Superset.

## Runtime flow

```text
DigiPOS Monitoring Top Up
        -> CSV per cluster
        -> finpay_topup_txn
        -> finpay_topup_saldo
        -> Superset PostgreSQL datasets
```

The source transaction table is not a finance editing surface. Manual finance
classification belongs in `finpay_topup_classification`, linked by `txn_id`.
The classification table can later be updated by a small internal form or
controlled CSV import because the Community edition of Superset is used as a
read-only dashboard surface.

## Reporting views

| View | Purpose |
|---|---|
| `finpay_topup_saldo` | Transaction detail, credit/debit amounts, running saldo, provenance, and classification. |
| `finpay_topup_daily` | One row per cluster and local report date for trend cards and daily tables. |
| `finpay_topup_cluster_summary` | One row per cluster for KPI cards and the dashboard landing page. |
| `finpay_topup_unclassified` | Only transactions that still need finance classification. |
| `finpay_topup_refresh_status` | Latest refresh audit, row-count evidence, BUCKET TOP UP comparison, and verification status. |
| `finpay_topup_bucket_compare` | Time series of DigiPOS CMS BUCKET TOP UP vs computed SALDO per cluster per report date, the daily reconciliation finance checks by hand. |
| `finpay_topup_bucket_compare_latest` (Superset virtual dataset) | Latest snapshot per cluster, used by the cluster KPI cards on the dashboard. |

`as_of_date` in `finpay_cluster_balance` means an opening balance before the
transactions on that calendar date. The refresh checkpoint is written for the
day after the latest transaction so it does not double-count the current day.

## Superset dataset setup

Use the official Apache Superset Docker Compose installation and pin it to an
official release tag. The current Context7 documentation identifies `6.0.0`
as the latest official quickstart tag at the time this guide was written.
Superset's metadata database must be separate from the FinPay source database.

Connect Superset to the FinPay PostgreSQL database with a dedicated read-only
database role. Do not reuse the Kestra or pipeline write credentials.

Create datasets for the four views above. For the main ledger, use Superset's
registered built-in `table` visualization with raw columns, a bounded row limit,
search, and server-side pagination when supported by the installed Superset
frontend. Do not use `ag_grid_table` unless that optional plugin has been
explicitly installed and registered. Apply the cluster and date filters before
requesting the table page; do not expose an unfiltered all-history card.

The default ledger keeps the finance-facing fields: cluster, transaction date,
sender, receiver, transaction type, `SETOR` (Kredit), `TOP UP` (Debit), running
`SALDO`, currency, remarks, and the finance-owned classification. Technical
fields such as `txn_id` and `source_file` remain hidden from the default table.

The finance dashboard is built around the daily DigiPOS CMS BUCKET TOP UP
reconciliation that finance performs by hand. It is a single Superset dashboard
with a native `cluster_id` filter for per-cluster drill-down (the filter is
deep-linkable via `?native_filters_key=…`):

- **Header** — a Superset **Markdown** tile with the title and a primary
  **Refresh All Clusters** button. The button is a Markdown link to the protected
  internal refresh page `http://<internal-host>:8095/refresh`; it submits
  yesterday→today for all six clusters and keeps Kestra credentials server-side.
  Do not put a Kestra webhook key in the URL. Use Markdown (not Handlebars): the
  deployment's strict CSP (`script-src 'self' 'strict-dynamic'`, no `unsafe-eval`)
  blocks Superset's Handlebars chart, which compiles templates with `eval`.
- **Cluster latest snapshot** — a Superset **table** of
  `finpay_topup_bucket_compare_latest` (latest snapshot per cluster) showing the
  BUCKET TOP UP value, the SALDO, the verification difference, the latest
  `captured_at` timestamp, and `bucket_status` (`VERIFIED` / `VERIFY_FAILED` /
  `VERIFY_SKIPPED`). With no cluster selected the grid shows all six clusters;
  selecting one cluster in the native filter narrows it to that cluster. (A
  Handlebars KPI-card grid was evaluated but is blocked by the CSP; relax the CSP
  only on an isolated dev instance if card visuals are required.)
- **BUCKET TOP UP vs SALDO trend** — an `xy` line chart of `bucket_topup_value`
  and `computed_saldo` over `report_date` (`finpay_topup_bucket_compare`). It is
  most meaningful per cluster, so scope it to the native `cluster_id` filter; with
  no filter it aggregates across all six clusters.
- **Reconciliation table** — `finpay_topup_bucket_compare`, sorted by
  `report_date DESC`, with `cluster_id`, `report_date`, `bucket_topup_value`,
  `computed_saldo`, `verification_diff`, and `bucket_status`. This is the time
  series finance checks by hand.

Keep technical provenance fields (`txn_id`, `source_file`) hidden from the
default cards/table; they remain available in the `finpay_topup_saldo` dataset.
Use red formatting for `VERIFY_FAILED` and `FAILED`; the ledger remains visible
in both states. The BUCKET TOP UP comparison is empty until the first refresh
populates `finpay_bucket_topup_snapshot` (the updated `verify.py` records a
snapshot on every refresh).

LinkAja and the existing FinPay pipeline should contribute their own validated
reporting views to a later shared dashboard dataset. Their business
calculations remain in their owning domains; this package must not reinterpret
LinkAja reversal or fee semantics as Top Up saldo.

## Validation boundary

Before connecting Superset, validate the top-up flow with:

- synthetic Kredit/Debit and rerun fixtures;
- pagination responses that reach and fail to reach `recordsTotal`;
- a legacy workbook reconciliation against an isolated disposable database;
- a read-only database role check;
- a bounded Superset table query with cluster and date filters.

Do not treat a YAML parse, an unfiltered table preview, or skipped PostgreSQL
tests as acceptance.

## Planned dashboard refresh and saldo verification

The operational goal is to replace the manual sheet refresh with a
finance-triggered refresh after cashier reports are ready. The default refresh
window is yesterday through today for all six FinPay clusters. The protected
refresh endpoint normalizes every submitted request so the `end` date is always
today in `Asia/Makassar`; this prevents an old dashboard tab from refreshing
only through a stale date.

Superset Community should remain read-only. The finalized trigger surface for
the first release is the protected internal refresh page, not Telegram and not
a direct browser call to Kestra. Telegram can be added later as a notification
or operator shortcut, but it should not become the required finance workflow.
The internal endpoint owns authentication, keeps the Kestra webhook/API key
server-side, passes `start`, `end`, `users`, and `dry_run` to
`finpay_topup_pipeline_v1`, and writes refresh audit status back to PostgreSQL.
Run the prototype endpoint with:

```bash
FINPAY_REFRESH_HOST=127.0.0.1 FINPAY_REFRESH_PORT=8095 \
KESTRA_BASE_URL=http://localhost:8080 \
KESTRA_BASIC_AUTH_USERNAME=admin@example.com \
FINPAY_REFRESH_BASIC_USER=finance \
python -m finpay_topup_pipeline refresh-service
```

Then add the **Refresh All Clusters** button on the dashboard as a Handlebars
`<a>` link (or Superset Markdown link) to `http://<internal-host>:8095/refresh`.
Set `FINPAY_REFRESH_BASIC_PASSWORD` from the runtime secret store, or protect
this endpoint with the same internal network or reverse-proxy authentication
used for Superset. Set `KESTRA_BASIC_AUTH_PASSWORD` or `KESTRA_API_TOKEN` from
the runtime secret store so the service can submit executions to Kestra. Do not
put a Kestra webhook key or Kestra password in the dashboard URL.
For local Compose testing, `docker compose up -d superset finpay-topup-refresh`
starts the dashboard and the protected refresh endpoint.

Kestra Open Source resolves `{{ secret('DIGIPOS_PASSWORD') }}` from a
base64-encoded environment variable named `SECRET_DIGIPOS_PASSWORD` loaded by
`.env_encoded`. If that key is missing, the refresh button can submit the
execution successfully, but the first Kestra task fails before any download or
dashboard audit row is written. Add the encoded secret to `.env_encoded` and
restart Kestra before running live DigiPOS verification.

The refresh page returns a finance-readable submission screen with links back
to Superset and the Kestra execution. The Superset status view exposes
`refresh_message`, `refresh_in_progress`, `refresh_usable`,
`seconds_since_update`, `duplicate_rows`, and the BUCKET comparison columns so
finance can tell whether the table is current before using the SALDO.

The trusted acceptance condition is not only "download completed". A refresh is
trusted only when source row counts match, immutable rows load idempotently, the
legacy saldo calculation succeeds, and the computed cluster SALDO matches the
DigiPOS CMS home `BUCKET TOP UP` snapshot. For refreshes ending today, the
workflow compares BUCKET TOP UP to the computed saldo before today's
transactions because the CMS home value represents yesterday closing data.
The workflow records the source transaction date and CMS snapshot time. On a
mismatch, it downloads the requested window once more to absorb an in-flight
transaction, then retains either the verified evidence or `VERIFY_FAILED`
evidence for finance review. Loaded rows are never rolled back. A live,
in-progress report date does not create a next-day opening-balance checkpoint;
only a verified historical end date can advance that checkpoint.

Implementation guidance for future agents is captured in
`skills/finpay-topup-saldo-refresh/SKILL.md`.

## Local data-correctness restore before UI polish

When the Compose dashboard shows dummy rows or an empty refresh status, restore
the finance dataset from explicit inputs instead of editing PostgreSQL by hand.
For the MOROWALI repair path, seed the legacy workbook as cluster `421318`,
stop legacy rows before the live DigiPOS extract starts, then load the live
Top Up CSV inbox idempotently:

```bash
python -m finpay_topup_pipeline restore-dashboard-data \
  --dsn "host=localhost port=5433 dbname=finpay user=finpay password=finpay" \
  --reset-topup-data \
  --legacy-xlsx "data/FINPAY MOROWALI.xlsx" \
  --legacy-cluster-id 421318 \
  --opening-as-of-date 2026-08-01 \
  --opening-balance 67967075 \
  --legacy-from-date 2026-08-01 \
  --legacy-before-date 2026-08-11 \
  --inbox tmp/finpay-topup-live-421318-20260811-20260818-recheck \
  --refresh-start 2026-08-11 \
  --refresh-end 2026-08-18 \
  --users 421318_A
```

`--reset-topup-data` truncates only the FinPay Top Up prototype tables:
`finpay_topup_txn`, `finpay_topup_classification`,
`finpay_cluster_balance`, and refresh audit tables. It does not touch the
existing FinPay invoice/transaction tables that share the Compose database.
When no live BUCKET TOP UP value is supplied, the command marks the audit as
`VERIFY_SKIPPED` so finance can see that the ledger is loaded but not yet
trusted against the CMS home bucket.
