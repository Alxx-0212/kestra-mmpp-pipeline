# LinkAja Agent Guide

Read `README.md` in this directory before changing LinkAja behavior. It is the
canonical workflow, schema, calculation, and operating reference.

## Scope

- Daily workflow: `../linkaja_fee_pipeline.yml`
- Monthly workflow: `../linkaja_monthly_materialization.yml`
- Python compatibility entry point: `../linkaja_pipeline.py`
- PostgreSQL migrations: `migrations/`
- Read-only operational reports: `queries/`
- Learning-only dbt project: `dbt/`

The dbt project is not called by Kestra yet. PostgreSQL migrations, Python
persistence, and the published monthly snapshot remain the production path.

## Required invariants

- Treat the export as ledger rows and aggregate business events by
  `cluster_id + transaction_id`.
- Keep raw storage and derived views scenario-agnostic. New scenarios must not
  disappear merely because reporting does not classify them yet.
- Resolve reversals only through `Original Transaction ID` in the same cluster.
  Never infer a reversal from equal amount, date, organization, or outlet.
- Keep reversals on their actual finalized timestamps. Do not move them back to
  the original transaction date or apply them twice.
- Preserve rerun behavior: daily loads replace affected transaction IDs;
  monthly publication replaces only one `cluster_id + report_month` snapshot.
- Never edit an applied migration. Add the next numbered migration.
- Do not sum `Balance` or both sides of an internal transfer.
- Keep `signed_amount` ledger-scoped as
  `ledger_debit_total - ledger_credit_total`; do not derive it from company
  Purchase Account fields.
- Do not treat source `Fee` as universally additive to debit or credit.

## Monthly fee contract

- Digipos eligibility requires scenario `Digipos B2B Transfer Fee` and
  transaction-grain `source_fee = 200`.
- Digipos payable is Rp200 per eligible active original transaction.
- A completed linked reversal before the cutoff makes its original inactive.
- `company_credit` is reconciliation evidence only; it does not determine the
  Digipos payable amount.
- PPOB payable is the active `source_fee` for
  `General to Purchase B2B Transfer Agent Telco`.
- Missing active PPOB fees must produce null net/payable values,
  `INCOMPLETE_MISSING_FEE`, and data-quality counts, not zero.
- Unresolved reversals remain a separate unusual report and are not assigned to
  Digipos or PPOB from their amount.
- `linkaja_monthly_fee_summary_v` is the canonical published summary surface.
- Late raw data updates the live views, not the frozen monthly snapshot. Use the
  refresh-candidate query, then explicitly preview and republish the affected
  cluster-month.

## Validation

```bash
python3 -m compileall -q linkaja_pipeline.py linkaja_fee_pipeline tests
python3 -m unittest discover -s tests
git diff --check
docker build -f Dockerfile.linkaja -t linkaja-fee-pipeline:3.11 .
```

The PostgreSQL integration tests require a dedicated disposable
`LINKAJA_TEST_DSN`; they truncate LinkAja-owned test tables.
