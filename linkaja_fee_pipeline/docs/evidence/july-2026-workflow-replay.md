# LinkAja July 2026 Workflow Test Summary

Status: source-bundle replay passed on 2026-08-12; guarded PostgreSQL and Kestra
acceptance remain pending.

## Finance interpretation

The current calculation now treats a completed same-cluster reversal as if its
directly referenced original did not contribute a fee. The original and
reversal remain visible as ledger evidence, but both contribute zero to the
active fee after the reversal is available.

The four finance measures are deliberately distinct:

| Measure | Valid active rule |
|---|---|
| In-cluster Rp20 | Original `Digipos B2B Transfer In Cluster`, completed and not reversed, multiplied by Rp20. |
| Expected Rp200 | Original `Digipos B2B Transfer` with positive company Purchase Account credit, completed and not reversed, multiplied by Rp200. |
| Posted Digipos fee | Original `Digipos B2B Transfer Fee` whose transaction-grain source Fee is exactly Rp200, completed and not reversed. |
| PPOB/agent-telco fee | Original `General to Purchase B2B Transfer Agent Telco`, completed and not reversed; payable is its transaction-grain source Fee. |

The expected Rp200 measure is derived from `Digipos B2B Transfer`. It is not
the same measure as a posted `Digipos B2B Transfer Fee` transaction.

## Test scope

- Source: 43 CSV exports for clusters `411311`, `421306`, `421307`, `421315`,
  `421318`, and `421320`.
- Source rows read and retained: 1,560,180.
- Completed transaction facts: 999,898.
- July transaction facts: 770,592.
- Report month: July 2026.
- Final exclusive cutoff: `2026-08-10T00:00:00` WITA.
- Files were applied in numeric export-token order.
- The replay enforced the exact CSV header, transaction coherence, complete
  transaction status, transaction-level aggregation, company Purchase Account
  rule, whole-transaction replacement, and same-cluster exact
  `Original Transaction ID` reversal rule.

The replay uses a disposable local SQLite file only as an aggregation engine.
It does not replace the required guarded PostgreSQL integration run.

## July active fee result

| Cluster | Active in-cluster count | Rp20 fee | Active expected-Rp200 count | Expected Rp200 fee | Active posted-fee count | Posted Digipos payable | Active PPOB count | Known PPOB fee | PPOB release status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 411311 | 81,068 | Rp1,621,360 | 33,045 | Rp6,609,000 | 7,775 | Rp1,555,000 | 10,822 | Rp21,859,530 | Complete |
| 421306 | 82,094 | Rp1,641,880 | 26,813 | Rp5,362,600 | 4,787 | Rp957,400 | 14,379 | Rp31,159,480 | Complete |
| 421307 | 62,343 | Rp1,246,860 | 33,615 | Rp6,723,000 | 7,114 | Rp1,422,800 | 11,243 | Rp23,758,980 | Complete |
| 421315 | 68,667 | Rp1,373,340 | 31,324 | Rp6,264,800 | 4,599 | Rp919,800 | 15,741 | Rp31,611,450 | Complete |
| 421318 | 69,862 | Rp1,397,240 | 38,697 | Rp7,739,400 | 9,752 | Rp1,950,400 | 11,039 | Rp21,604,160 known | **Incomplete: one active Fee missing** |
| 421320 | 29,827 | Rp596,540 | 14,682 | Rp2,936,400 | 7,179 | Rp1,435,800 | 7,564 | Rp14,420,730 | Complete |
| **All** | **393,861** | **Rp7,877,220** | **178,176** | **Rp35,635,200** | **41,206** | **Rp8,241,200** | **70,788** | **Rp144,414,330 known** | **Incomplete** |

The incomplete PPOB transaction is:

| Cluster | Report date | Transaction ID | Problem |
|---|---|---|---|
| 421318 | 2026-07-21 | `FA5F57771X` | Active PPOB transaction has no source Fee. |

The known PPOB subtotal is retained as evidence, but official aggregate and
cluster `421318` PPOB payable must remain null until the missing Fee is supplied
or an approved disposition is recorded.

## Fee effect of resolved reversals

| Fee population | Gross transactions | Reversed originals | Active transactions | Gross fee | Reversed fee | Active fee |
|---|---:|---:|---:|---:|---:|---:|
| In-cluster Rp20 | 394,019 | 158 | 393,861 | Rp7,880,380 | Rp3,160 | Rp7,877,220 |
| Expected Rp200 | 179,390 | 1,214 | 178,176 | Rp35,878,000 | Rp242,800 | Rp35,635,200 |
| Posted Digipos fee | 41,214 | 8 | 41,206 | Rp8,242,800 | Rp1,600 | Rp8,241,200 |
| PPOB | 70,788 | 0 | 70,788 | Incomplete | Rp0 | Incomplete |

Multiple reversals do not subtract the original more than once: `is_reversed`
is a boolean active-state decision, while `reversal_count` preserves the number
of reversal events for investigation.

## Reversal result through the August 10 cutoff

| Cluster | Resolved | July unresolved | August 1-9 unresolved | Total unresolved |
|---|---:|---:|---:|---:|
| 411311 | 366 | 24 | 6 | 30 |
| 421306 | 283 | 114 | 35 | 149 |
| 421307 | 345 | 85 | 17 | 102 |
| 421315 | 287 | 126 | 20 | 146 |
| 421318 | 376 | 110 | 31 | 141 |
| 421320 | 180 | 16 | 4 | 20 |
| **All** | **1,837** | **475** | **113** | **588** |

Resolved reversals by original scenario:

| Original scenario | Resolved reversals |
|---|---:|
| `Digipos B2B Transfer` | 1,628 |
| `Digipos B2B Transfer In Cluster` | 198 |
| `Digipos B2B Transfer Fee` | 11 |

Of these, 457 are August reversals of August originals. No resolved August
reversal references a July original in this bundle. The resolved calendar-day
lag distribution is 1,829 same-day, seven one-day, and one two-day edge; the
maximum elapsed interval is 215,664 seconds.

Unresolved reversals remain a separate exception population. Their original
scenario and fee category cannot be proven from the current source bundle.
Daily MANDIRI or other settlement evidence may support manual investigation,
but must not create an inferred reversal link or automatically change a fee.

## Cutoff stability

| Exclusive WITA cutoff | Reversals in scope | Resolved | Unresolved | July facts | Fee comparison |
|---|---:|---:|---:|---:|---|
| 2026-08-01 00:00:00 | 1,855 | 1,380 | 475 | 770,592 | Baseline |
| 2026-08-05 00:00:00 | 2,109 | 1,579 | 530 | 770,592 | Identical for every cluster/category |
| 2026-08-10 00:00:00 | 2,425 | 1,837 | 588 | 770,592 | Identical for every cluster/category |

The stability is correct for this bundle because all added resolved August
reversals reference August originals. A synthetic acceptance fixture separately
proves that a July original reversed in August becomes inactive when the later
cutoff includes that exact reversal.

## Daily dashboard behavior

The current schema supports a read-only Taipy dashboard with daily reversal
updates:

- `linkaja_transactions_current_v` supplies current transaction detail,
  `is_reversed`, reversal IDs/count, resolution status, and unusual state.
- `queries/linkaja_daily_fee_report.sql` supplies active Rp20/Rp200 daily fees.
- `queries/linkaja_daily_detail_measures.sql` supplies scenario, ledger,
  reversal-event, and unresolved measures.
- `linkaja_monthly_fee_summary_v` supplies the latest frozen published monthly
  fee summary.
- `queries/linkaja_unresolved_reversals.sql` supplies the exception queue.
- `queries/linkaja_monthly_refresh_candidates.sql` identifies frozen months
  requiring explicit preview and republication after late data.

When the next daily file contains a reversal, daily ingestion expands the
affected transaction IDs to the linked original and returns both affected
posting dates. The live view changes immediately. A frozen monthly snapshot
does not change automatically; it must be previewed and republished.

## Acceptance status

Source-replay result: passed.

Finance release is not yet fully approved because:

1. PPOB transaction `FA5F57771X` has a missing active Fee.
2. The 588 unresolved reversals require separate review and cannot be assigned
   automatically to a fee category.
3. The guarded PostgreSQL integration suite and migration 007 have not been run
   by the integrator.
4. Source-bundle fingerprint/readiness gating is not yet part of publication.

Reproduce the source replay with:

```bash
python3 -m linkaja_fee_pipeline.july_acceptance \
  /path/to/linkaja_\(07-2026\) \
  --report-month 2026-07 \
  --calculation-cutoff 2026-08-10T00:00:00 \
  --output /tmp/linkaja_july_acceptance_2026-07.json
```
