---
name: finpay-topup-saldo-refresh
description: Plan or implement the FinPay Top Up finance refresh workflow: download yesterday-through-today DigiPOS data, calculate legacy SALDO from SETOR/Kredit and TOPUP/Debit movements, validate source row counts, compare computed saldo with DigiPOS CMS home BUCKET TOP UP, and expose a safe dashboard-triggered Kestra refresh.
---

# FinPay Top Up Saldo Refresh

Use this skill when work touches the finance-facing FinPay Top Up monitoring
flow, especially requests for dashboard refresh buttons, Kestra-triggered
downloads, legacy SALDO reconciliation, or DigiPOS CMS BUCKET TOP UP checks.

## Required Context

Read these first:

- `AGENTS.md` for lane boundaries and production-safety rules.
- `finpay_topup_pipeline/README.md` for the current reporting contract.
- `skills/digipos-topup-monitoring/SKILL.md` for Monitoring Top Up JSON
  extraction.
- `skills/digipos-topup-monitoring/SKILL.md` for DigiPOS CMS login, row-count
  validation, API retry behavior, and CSV extraction.

Do not hard-code, print, or commit DigiPOS, Kestra, database, or dashboard
credentials. Treat live DigiPOS and Kestra execution as production-affecting
unless the user explicitly authorizes it.

## Finance Daily Flow

Default business flow:

1. Finance waits until cashier reports are complete.
2. Finance starts a refresh for yesterday through today, normally all six
   clusters.
3. The refresh downloads fresh DigiPOS source data, validates row counts, loads
   immutable rows, recalculates saldo, compares the latest downloaded saldo to
   the DigiPOS CMS BUCKET TOP UP snapshot, and records an audit status.
4. Superset remains a read-only reporting surface. It shows the ledger,
   classification fields, refresh status, row-count evidence, and saldo
   verification result.

## Source Responsibilities

- Use `digipos-topup-monitoring` to download Monitoring Top Up rows from
  `/deposit/monitoring-topup-detail`.
- Use the DigiPOS CMS scraper patterns for any browser-only checks, especially
  `/home` BUCKET TOP UP extraction and table/export row-count validation.
- Validate `recordsTotal == fetched rows` for Top Up JSON before loading.
- If using Riwayat Saldo Transaksi exports, validate DigiPOS table total entries
  exactly against Excel data rows before posting or loading.
- Treat `statusCode=05`, loading overlays, `NaN` totals, stale zero-entry
  tables, and download timeouts as transient DigiPOS failures. Retry or fail
  loudly; do not silently accept partial data.

## Saldo Contract

Preserve the legacy workbook semantics:

- `Kredit` / `SETOR` increases saldo.
- `Debit` / `TOPUP` decreases saldo.
- `SALDO = opening_balance + cumulative SETOR - cumulative TOPUP` per cluster,
  ordered by transaction timestamp and a deterministic tie-breaker.
- Opening balance must come from an explicit checkpoint or verified DigiPOS
  BUCKET TOP UP snapshot. Do not infer an opening balance from missing history.
- Advance an opening-balance checkpoint only after row-count validation and
  BUCKET TOP UP comparison pass for a completed report date. Never treat a
  matching mid-day BUCKET snapshot as a next-day opening balance.

When changing the calculation, test against the legacy workbook and current
pipeline fixtures before touching live data.

## BUCKET TOP UP Verification

Compare the latest downloaded cluster saldo with the BUCKET TOP UP value
displayed on `https://digipos-cms.finpay.id/home` for the same logged-in
cluster account. Do not permanently assume that BUCKET excludes today's
transactions: the MOROWALI recheck matched BUCKET only after an Aug 18 Top Up
row was downloaded.

Implementation guidance:

- Capture the BUCKET TOP UP snapshot after login and record snapshot time,
  username, cluster ID, displayed text, parsed numeric value, and page URL.
- Record the latest source transaction timestamp used in the computed saldo and
  the BUCKET snapshot time. If the first comparison differs, re-download the
  requested window once, reload idempotently, and compare again.
- Use a small currency tolerance only for formatting/rounding, not for missing
  transactions.
- If computed saldo differs from BUCKET TOP UP, mark the refresh
  `VERIFY_FAILED`, keep the evidence, and avoid advancing the trusted opening
  balance checkpoint.
- Do not write a next-day opening checkpoint while the requested end date is
  still in progress. A matching live snapshot is not proof of an end-of-day
  closing balance.

## Dashboard Trigger Pattern

Superset Community should stay read-only. Do not put a secret-bearing Kestra
webhook URL directly into a dashboard markdown tile.

Recommended pattern:

1. Add a small protected internal refresh endpoint or app next to Superset.
2. Put a Superset Markdown link/button to that internal endpoint.
3. The endpoint collects `start`, `end`, and `clusters`, then calls Kestra
   server-side with either:
   - `POST /api/v1/main/executions/{namespace}/{flowId}` and form inputs, or
   - `POST /api/v1/main/executions/webhook/{namespace}/{flowId}/{key}` when the
     webhook key is stored server-side only.
4. The endpoint polls Kestra until terminal state or returns an execution link
   plus a pending status.
5. Superset reads refresh status from PostgreSQL views; it does not mutate data.

Minimal acceptable prototype: a Superset Markdown button linking to a protected
internal refresh page. Avoid a direct browser request to a Kestra webhook key.

## Audit Tables and Views

For implementation, add append-only refresh metadata instead of only printing
Kestra logs:

- Requested window, selected clusters, requested_by, requested_at.
- Per-cluster DigiPOS row counts from every source.
- Load counts: seen, inserted, duplicates.
- Latest computed saldo, source transaction date, BUCKET TOP UP snapshot, and
  verification attempt count.
- Status: `REQUESTED`, `DOWNLOADED`, `LOADED`, `VERIFIED`, `VERIFY_FAILED`,
  `VERIFY_SKIPPED`, `FAILED`.
- Error message and retry count.

Expose a read-only Superset status view so finance can see whether the dashboard
is fresh before trusting the table.

## Validation Checklist

Before acceptance:

- Run unit tests for CSV parsing, row hashing, loading, saldo calculation, and
  workbook reconciliation.
- Run a disposable PostgreSQL end-to-end load with duplicate rerun behavior.
- Verify Top Up `recordsTotal` equals fetched/exported rows for every requested
  cluster.
- Verify BUCKET TOP UP comparison behavior for pass and fail cases with mocked
  snapshots before live use.
- Verify the dashboard refresh link does not expose secrets in the browser,
  URL, Superset metadata, logs, or Git.
