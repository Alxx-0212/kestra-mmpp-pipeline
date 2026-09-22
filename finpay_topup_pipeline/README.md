# FinPay Top Up workflow and Superset reporting

This package downloads DigiPOS Monitoring Top Up rows, stages them for
verification, stores immutable transaction facts in PostgreSQL after finance
approval, calculates a running saldo, and exposes read-only
reporting views for Apache Superset.

## Runtime flow

```text
DigiPOS Monitoring Top Up
        -> prepare_refresh (resolve checkpoint + reserve audit row)
        -> extract_topup (per-cluster 3-day chunks, 1-day fallback, retry failed chunks only)
        -> ingest_topup (validate + checkpoint-filter + row_hash dedupe -> staging)
        -> refresh_saldo (CMS snapshot + committed UNION ALL staged verification)
        -> optional reextract_topup -> restage_topup -> reverify_saldo
        -> finalize_refresh (purge technical failure or preserve review state)
        -> notify_review (review alert only)
        -> summary_topup (read-only ledger summary)
        -> Superset PostgreSQL datasets
```

The source transaction table is not a finance editing surface. Manual finance
classification belongs in `finpay_topup_classification`, linked by `txn_id`.
Finance assigns outlets from the `finpay_outlet` lookup (`code`, `label`,
`active`) through the Telegram classification bot or a controlled CSV import;
the Community edition of Superset stays a read-only dashboard surface.

## Stage-first verification

`ingest_topup` never writes to the committed ledger. `extract_topup` owns
per-cluster checkpoint-aware acquisition, three-day chunking with a one-day
fallback for timeout-prone ranges, artifact reuse, and failed-chunk retry;
`ingest_topup` owns parsing, validation, checkpoint
filtering, and deduplication. It loads rows into `finpay_topup_txn_staging`
keyed by `refresh_id`. Restaging the same refresh replaces its staged rows,
hashes already present in `finpay_topup_txn` are dropped, and duplicates inside
a batch collapse to their first occurrence.

### Kestra task boundaries

The flow uses one Kestra `WorkingDirectory` so the explicit sequential tasks
share the extracted CSV inbox. Its `outputFiles` declaration captures the CSV
artifacts without making database state implicit between tasks. Child scripts
resolve the absolute path from Kestra's injected `WORKING_DIR` at runtime,
because task-property rendering happens before the child working-directory
context exists. Each task has one responsibility:

| Task | Responsibility | Database effect |
|---|---|---|
| `prepare_refresh` | Resolve the checkpoint-driven window and reserve the audit row. | Inserts `REQUESTED` refresh/cluster audit rows. |
| `extract_topup` | Download each cluster from its own checkpoint in three-day chunks, fall back to one-day chunks after a failed request, and retry only missing chunks. | Records per-cluster `DOWNLOADED`/`FAILED` evidence; marks the refresh `FAILED` only when a required chunk cannot complete. |
| `ingest_topup` | Parse, validate, checkpoint-filter, batch-dedupe, and exclude ledger hashes. | Replaces only this refresh's staging rows; never commits ledger rows. |
| `refresh_saldo` | Read CMS BUCKET TOP UP and compare it with committed plus this refresh's staging rows. | Writes cluster verification evidence and `PENDING_REVIEW`/`VERIFY_FAILED`. |
| `export_staging_review` | When a cluster has a numeric mismatch, create a read-only daily-gap workbook for only those mismatched clusters. | No database writes; the workbook is delivered with the review alert. |
| `reextract_topup` / `restage_topup` / `reverify_saldo` | Recover a technical verification failure with a fresh source snapshot. | Replaces only the selected refresh staging and rewrites its evidence. |
| `finalize_refresh` | Preserve reviewable partial verification and purge only a technical failure with no verified cluster. | `VERIFY_SKIPPED` promotes rows; partial verification retains staging for review. |
| `notify_verification_failure` | Send a one-way alert for handled CMS verification failure. | No database writes. |
| `notify_review` | Send a numeric review alert with approval callbacks. | No database writes. |
| `summary_topup` | Read the committed ledger summary for observability. | No database writes. |
| `cleanup_failed_refresh` (errors) | Clear partial staging and close an audit row stranded by a task-level failure. | Marks only non-reviewable partial refreshes `FAILED`. |

`finpay_topup_txn.row_hash` is the final global uniqueness boundary. The
staging table is isolated by `refresh_id`; overlapping active scopes are
rejected by the database-backed `start_refresh` guard before another refresh
can stage the same cluster.

### Legacy workbook normalization

`legacy_normalization.apply_legacy_workbooks()` is the explicit bootstrap
operation for the six files under `data/finpay_topup`. It loads only rows before
the configured last-complete-looking boundary, registers outlet labels in
`finpay_outlet`, and records cluster scope in `finpay_cluster_outlet`.

The high-confidence legacy outlet map is:

| Cluster | Normalized outlets |
|---|---|
| `411311` | `PALANGKARAYA`, `KATINGAN`, `GUNUNG MAS` |
| `421306` | `TOBELO-KAO`, `MOROTAI`, `JAILOLO`, `SUBAIM-BULI` |
| `421307` | `SOFIFI-TIDORE`, `BACAN-OBI`, `WEDA` |
| `421315` | `LUWUK`, `AMPANA`, `BALUT` |
| `421318` | `BAHODOPI`, `BETELEME`, `BUNGKU`, `POSO` |
| `421320` | `TERNATE`, `SANANA-TALIABU` |

This is the finance-authoritative production allowlist supplied on 2026-09-09.
New Kredit rows remain unclassified until finance assigns one of the mapped
outlets for that cluster.

The release baseline opening checkpoints used for the controlled production
refresh are:

| Cluster | Last validated close | Next `as_of_date` | Opening balance |
|---|---|---|---:|
| `411311` | 2026-09-03 | 2026-09-04 | Rp 73.626.600 |
| `421306` | 2026-09-03 | 2026-09-04 | Rp 54.076.000 |
| `421307` | 2026-09-03 | 2026-09-04 | Rp 39.133.701 |
| `421315` | 2026-08-10 | 2026-08-11 | Rp 36.302.500 |
| `421318` | 2026-09-03 | 2026-09-04 | Rp 62.562.200 |
| `421320` | 2026-09-03 | 2026-09-04 | Rp 38.061.838 |

Refresh `3` subsequently downloaded all six regions and was approved through
the Telegram review action on 2026-09-10. The current database checkpoint is
therefore 2026-09-10 for every region; Palangkaraya was accepted with a
`Rp 3.539.650` CMS difference and remains a finance follow-up item. The live
database, not this baseline table, is authoritative for the next refresh.

Legacy workbook transaction rows are not part of the production foundation;
the regular workflow stores new rows from these opening boundaries forward.

### Manual adjustments

Manual reconciliation rows use `finpay_topup_manual_adjustment`. A proposed
adjustment is excluded from saldo until a separate approver marks it
`APPROVED`; `VOID` removes it from future calculations. It remains outside the
immutable DigiPOS ledger and carries a stable adjustment hash, effective
timestamp, reason, source reference, and creator/approver audit.

For a manually corrected opening balance, use
`finpay_topup_checkpoint_override` rather than editing
`finpay_cluster_balance`. Overrides are also proposed/approved and cannot move
the effective checkpoint backward. Give an inserted adjustment a distinct
`effective_at` when its position among `transaction_date ASC` rows matters;
equal timestamps intentionally have no secondary ordering key.

### Operator correction flow

`finpay_topup_correction_v1` is a manual-only Kestra flow with no Telegram
trigger. Restrict its execute permission to the Top-Up operator role and set
the `FINPAY_TOPUP_OPERATOR_ALLOWLIST` Kestra secret to the approved operator
names. It supports `RESTAGE` for one pending cluster/date range,
`PROPOSE_ADJUSTMENT` for an audited `PROPOSED` adjustment, and
`APPROVE_ADJUSTMENT` for the operator-controlled approval followed by CMS
re-verification. Every operation requires a reason and a `refresh:<id>` source
reference. The flow never commits ledger rows; unresolved mismatches remain in
staging and produce a new analysis workbook.

`refresh_saldo` then verifies against the DigiPOS CMS BUCKET TOP UP using a
running saldo computed over committed rows UNION ALL staged rows for that
refresh (`saldo._compute_running_saldo(..., include_refresh_id=...)`):

- **numeric match or mismatch** — the result is recorded as `VERIFIED` or
  `MISMATCH`, the refresh becomes `PENDING_REVIEW`, and staging remains isolated
  for finance. The alert includes the CMS capture timestamp, calculation cutoff,
  source transaction range, signed difference, and inline Setujui/Tolak buttons.
  Approval promotes only `VERIFIED` cluster staging with the existing `row_hash`
  conflict protection, records `reviewed_by`/`reviewed_at`, and advances only
  those cluster checkpoints in the same transaction. In an all-cluster refresh,
  mismatched or incomplete clusters remain in staging and keep the refresh
  `PENDING_REVIEW`. Rejection records the review and marks `REJECTED` while
  purging only the remaining staging for that refresh.
   A new refresh is blocked when its cluster/user scope overlaps any nonterminal
   refresh (`REQUESTED`, `STAGED`, `PENDING_REVIEW`, or active verification),
   not only a refresh already awaiting review.
 - **technical verification failure** — after one automatic restage-and-reverify,
  a cycle with no verified cluster is purged and becomes `VERIFY_FAILED`. If at
  least one cluster verifies, its rows remain reviewable while failed clusters
  stay in staging and the refresh becomes `PENDING_REVIEW`. The committed ledger
  and trusted checkpoints remain untouched until approval.
- **verification skipped** (`verify_bucket_topup=false`) — staged rows are
  committed without advancing any checkpoint so manual unverified loads stay
  visible as legacy `VERIFY_SKIPPED` runs.

### Running-saldo match evidence

Verification is a complete calculation from the latest trusted opening
checkpoint through the CMS cutoff. It is not a comparison against one selected
source row. Current-day runs include committed rows plus the current refresh's
staged rows through the inclusive CMS capture timestamp; closed historical runs
use the documented exclusive next-day cutoff.

The verifier also traces that same input once to explain a numeric match:

- `source_row_count` is the raw number of rows downloaded before checkpoint
  filtering and hash deduplication;
- `calculation_row_count` is the committed-plus-staged row count actually used
  by the saldo calculation;
- trace order is exactly `transaction_date ASC`, with no secondary ordering
  key;
- when multiple rows match within the existing `0.5` IDR tolerance, the last
  matching row in that ascending scan is reported;
- `CHECKPOINT` means the opening balance matched before row 1, while `CUTOFF`
  is the defensive fallback when the final cutoff balance matches without a
  transaction-row match;
- a mismatch never presents an intermediate row as the final reconciliation
  match.

Telegram receives only the selected matching row, not every downloaded row and
not a paginated dump. Standard Bot API HTML mode provides bold labels and code
backgrounds without Telegram Premium. Dynamic database values are HTML-escaped.
The review message follows this shape:

```html
<b>FINPAY TOP-UP</b>
<b>Data diunduh:</b> <code>200 baris</code>
<b>Baris dihitung:</b> <code>137</code>
<b>Saldo cocok pada baris:</b> <code>137 dari 137</code>
<b>Cocok dengan CMS pada:</b> <code>4 Sep 2026 16:07:37</code>
<pre>ID: 12345
Waktu: 4 Sep 2026 14:12:33
Tipe: Kredit
Nilai: Rp 100.000
Running saldo: Rp 65.645.685</pre>
```

Alerting needs `TELEGRAM_ALERT_CHAT_ID` and the webhook-owning FinPay bot token
(`FINPAY_TELEGRAM_BOT_TOKEN`, injected from the bot Docker Secret) for both
interactive review and one-way technical failure alerts. Missing configuration
or a Telegram outage is logged and never fails a cycle. Flow-level crashes
additionally fire the one-way `notify_flow_failure` errors task. Local E2E uses
`SECRET_TELEGRAM_ALERT_CHAT_ID` for the temporary private chat; production can
replace that secret with the review group chat ID after adding the bot.

## Telegram Kredit classification bot

`services/telegram_bot/` (Compose service `telegram-bot`, port 8096) exposes a
scoped catalog with `/start`, `/topup`, `/status`, `/kredit`, `/kreditlist`, and
`/topupexcel`, and `/help`.
Guided menus select the refresh/status scope or Kredit date window; the strict
`/refresh cluster <cluster_id>` and `/refresh all` forms remain available for
advanced users. Kredit cards use only active `finpay_outlet` rows and carry
`c:<seq>:<txn_id>:<outlet>` callback data; `Lewati` defers a row and `Selesai`
reports the deferred count. A callback writes only after one transaction
revalidates cluster membership, Kredit type, unclassified state, and active
outlet.
Review alerts use `✅ Setujui`, `❌ Tolak`, and `ℹ️ Lihat detail`; all-cluster
alerts show the checkpoint-based period separately for every region, and the
details view contains the refresh audit ID and timestamp provenance.
`/kreditlist` is read-only and supports `1`, `3`, `7`, or `all` days plus
`custom YYYY-MM-DD YYYY-MM-DD`, scoped to any configured cluster or all
clusters. Results are paginated and show the outlet classification, or
`Belum ditandai` when no outlet has been assigned.

`/topupexcel` is a read-only finance export. The guided menu selects one cluster
or all six, then `1`, `3`, `7`, or a custom date range. One workbook is returned
through Telegram with one sheet per selected cluster and finance columns:
`Transaction Date`, `Sender`, `Receiver`, `Transaction Type`, `SETOR`, `TOPUP`,
`SALDO`, `Currency`, `Remarks`, `Outlet`, and `Source`. SALDO is recalculated
from the latest checkpoint at or before the range, including approved manual
adjustments; only rows inside the selected range are displayed. Export spans are
limited to 366 days and 200,000 displayed rows to stay inside runtime and
Telegram file limits.
`classified_by` stores the Telegram username. Pending/classified truth lives
only in Postgres — the bot derives remaining rows from the DB every turn, keeps
only convenience state in memory, and dedupes replayed `update_id`s. Access
fails closed unless both `TELEGRAM_ALLOWED_CHAT_IDS` and
`TELEGRAM_ALLOWED_USER_IDS` are set, and webhook calls must carry the
`X-Telegram-Bot-Api-Secret-Token` header value stored in
`./secrets/telegram_webhook_secret.txt`. Local Compose runs the pinned ngrok
agent and a webhook-register sidecar discovers its HTTPS endpoint through the
ngrok API. Production can replace that service with a remotely managed stable
Cloudflare hostname and tunnel token.

Swapping the placeholder `OUTLET_1..3` enum for the real outlet list is a data
change in `finpay_outlet` only — no redeploy.

### Telegram `/refresh` command

The same bot submits the existing
`finance.finpay.finpay_topup_pipeline_v1` execution API via
`finpay_topup_pipeline.refresh_service.submit_kestra_refresh`:

- `/refresh full` and the legacy `/refresh`, `/refresh now`,
  `/refresh yesterday`, and date-window forms are rejected. The public routes
  are `/refresh all` and `/refresh cluster <cluster_id>` only.
- `/refresh all` resolves all six stored checkpoints and records
  `refresh_mode=AUTO_ALL`; the Telegram confirmation lists each checkpoint
  start instead of displaying one misleading global start date.
  `/refresh cluster <cluster_id>` resolves that
  cluster's latest `as_of_date`, uses today in `Asia/Makassar` as the end date,
  maps only the fixed cluster-to-user allowlist, and records
  `refresh_mode=AUTO_CLUSTER`. Enable the automatic routes with
  `TELEGRAM_SELECTIVE_REFRESH_ENABLED=true` after validation.
- All refreshes use `dry_run=false`, `verify_bucket_topup=true`, and the fixed
  namespace/flow. The bot passes the resolved checkpoint dates and the flow
  revalidates them before extraction. Chat input cannot provide dates,
  arbitrary users, or SQL.
  The flow keeps exactly its Webhook + Schedule YAML trigger types; Telegram
  remains an external execution source.
- A successful submission answers once with the execution id, window, and an
  optional raw Kestra UI link (only when `KESTRA_PUBLIC_URL` is configured);
  the existing cycle/review/failure alerts report the terminal result. Numeric
  `VERIFIED` and `MISMATCH` alerts retain staging and expose Setujui/Tolak
  buttons for finance; ℹ️ Lihat detail shows audit provenance.
- One in-flight refresh per chat plus a 10-minute per-chat cooldown after a
  successful submission guard against double-taps (`TELEGRAM_REFRESH_COOLDOWN_SECONDS`);
  both reset on bot restart. Failures clear the in-flight guard and start no
  cooldown.
- The bot reuses the standalone deployment's global Kestra Basic-auth
  environment values (`KESTRA_BASIC_AUTH_USERNAME` /
  `KESTRA_BASIC_AUTH_PASSWORD`); a dedicated namespace-scoped service user
  would require a future Kestra IAM migration.

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

`as_of_date` in `finpay_cluster_balance` is the first local calendar date whose
transactions are recalculated from `opening_balance`, which is the trusted
balance immediately before that date. An accepted current-day refresh writes
the same date as its opening boundary so a newer same-day refresh can re-pull
and hash-dedupe that date. An accepted closed historical refresh writes the
following day. A newer refresh may update the same boundary; an older refresh
never moves the latest checkpoint backward.

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
  **Refresh All Clusters** action is owned by the Telegram/Kestra Webhook or
  Schedule path. Keep Kestra credentials server-side. Use Markdown (not
  Handlebars): the
  deployment's strict CSP (`script-src 'self' 'strict-dynamic'`, no `unsafe-eval`)
  blocks Superset's Handlebars chart, which compiles templates with `eval`.
- **Cluster latest snapshot** — a Superset **table** of
  `finpay_topup_bucket_compare_latest` (latest snapshot per cluster) showing the
  BUCKET TOP UP value, the SALDO, the verification difference, the latest
  `captured_at` timestamp, and `bucket_status` (`VERIFIED` / `MISMATCH` /
  `PENDING_REVIEW` / `VERIFY_FAILED` / `VERIFY_SKIPPED`). With no cluster selected the grid shows all six clusters;
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

Set `FINPAY_TOPUP_TEST_DSN` only to a disposable PostgreSQL database named
`finpay_test`; destructive integration tests refuse any other database.

Do not treat a YAML parse, an unfiltered table preview, or skipped PostgreSQL
tests as acceptance.

## Current refresh operation

The active trigger surface is the Telegram guided menu plus the
`finance.finpay.finpay_topup_pipeline_v1` Kestra Webhook and Schedule. The
optional dashboard profile is read-only and does not submit refreshes. Kestra
credentials and webhook keys remain server-side.

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
ready for finance review only when source row counts match, immutable row hashes
are deduplicated, the legacy saldo calculation succeeds, and the computed
cluster SALDO is compared with the DigiPOS CMS home `BUCKET TOP UP` snapshot.
For current-day runs, calculation includes rows through the exact CMS snapshot
timestamp instead of excluding the whole calendar date. The workflow records
the source transaction range, CMS capture time, calculation cutoff, calculated
time, and signed difference. Both numeric matches and mismatches retain staging
in `PENDING_REVIEW`; approval commits and advances or updates only verified
clusters, while incomplete clusters stay in staging and the refresh remains
reviewable. Rejection purges the selected staging. Technical failures purge staging
without changing the ledger or trusted checkpoints. Approval of a current-day
refresh advances the opening boundary to that current date; a later refresh can
safely re-read the date and rely on `row_hash` deduplication.

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
