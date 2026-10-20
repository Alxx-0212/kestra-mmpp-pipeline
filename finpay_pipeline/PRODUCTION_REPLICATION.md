# FinPay Production Replication

Agent-readable build, CI/CD, commissioning, and recovery procedure. Read
[AGENTS.md](AGENTS.md), [README.md](README.md), and repository
[security](../docs/security.md), [architecture](../docs/architecture.md), and
[validation](../docs/validation.md) rules before acting.

This procedure does not authorize deployment, infrastructure creation, sharing
changes, resets, credential rotation, commits, or registry publication. Obtain
current approval for the actual targets and operations. No hosted CI workflow
is installed by this document; the gates below define what CI/CD must execute.

## 1. Freeze And Configure

1. Use an approved, reviewed source revision. Stop on a dirty checkout: a commit
   SHA does not identify uncommitted code. Do not commit without authorization.
2. Inventory the active writers, schedules, DB identities, workbook ID, worksheet
   IDs, and backups. A workbook title alone is not a unique identity.
3. Configure code-owned cell destination identities in `cell_format/settings.py`
   before building: new workbook ID, different legacy workbook ID, writer email,
   Finance editor emails, and four role/tab IDs per enabled cluster. The title
   scheme is `cluster_alias`; missing identities remain fail-closed. Credentials
   remain secrets, not source.
4. Resolve aliases from `CLUSTER_ALIASES` and verify exact titles `<alias>`,
   `<alias> Invoice`, `<alias> Unusual`, and `<alias> Reversal`. Current aliases
   are `412110_B=PKY`, `422090_B=MRT`, `422100_B=TDR`, `422010_B=BGI`,
   `422180_B=MRW`, `422220_B=TNT`, `422140_B=KOTAMOB`, and `422040_B=BOLMONG`.
    Keep worksheet IDs stable during a code-to-alias rename; changing IDs, workbook,
    account ownership, or editors is not a title migration. Before provisioning,
    read workbook metadata and take the approved backup. For each missing role only,
    create an empty tab with the configured ID and exact alias title (`addSheet`),
    then re-read all IDs/titles before enabling writes. Never delete or clear an
    existing tab as an initialization shortcut. First publication requires empty,
    unmanaged targets; existing managed tabs require their matching DB publication
    history. Never silently adopt them into a fresh DB or borrow synthetic fixture IDs.
5. Agree whether this release includes cell, legacy, or monthly reporting.
   A cell canary does not verify legacy/monthly live behavior or other domains.

### October 1 And SALDO

The new cell history starts on **2026-10-01**, in `Asia/Makassar`. The first
displayed running SALDO starts at **0**, then carries forward through rows/days;
it is not reset per execution. Do not insert an opening transaction, adjustment,
or separate balance table. Existing configuration provenance records zero/date
for hashing; raw DigiPOS balances remain immutable source evidence.

Persist October 1 before later dates. Earlier history and missing days fail
safely rather than becoming zero activity. Individual later-day preprocessing
remains possible offline without database access.

## 2. Build First

Use an approved agent with Bash, tar, Docker Engine, and Python 3.11. On a fresh
agent, fetch the approved Kestra `v1.3.26` validator/runtime and PostgreSQL `18.4`
images before the `--pull never` checks; verify and record their digests too.

Check `.dockerignore` before every build. This allowlisted context additionally
excludes credentials, data, private test evidence, Git metadata, and agent state.
Build all images on the same Docker daemon so the reporting build resolves the
new local base. Use approved registry names and unique release tags.

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
: "${REGISTRY:?Set the approved registry namespace}"
RELEASE_ID="$(git rev-parse --verify HEAD)"
BASE_IMAGE="$REGISTRY/finpay-pipeline:$RELEASE_ID"
REPORT_IMAGE="$REGISTRY/finpay-cell-reporting:$RELEASE_ID"
CELL_SCRAPER_IMAGE="$REGISTRY/finpay-digipos-cell-scraper:$RELEASE_ID"
LEGACY_SCRAPER_IMAGE="$REGISTRY/finpay-digipos-scraper:$RELEASE_ID"

finpay_context() {
  tar --exclude='__pycache__' -cf - .dockerignore \
    finpay_pipeline/Dockerfile finpay_pipeline/Dockerfile.cell-reporting \
    finpay_pipeline/Dockerfile.cell-scraper \
    finpay_pipeline/Dockerfile.digipos-scraper \
    finpay_pipeline/requirements.txt \
    finpay_pipeline/requirements-digipos-scraper.txt \
    finpay_pipeline/*.py finpay_pipeline/legacy/*.py \
    finpay_pipeline/cell_format/*.py finpay_pipeline/migrations/*.sql \
    finpay_pipeline/queries/*.sql
}
finpay_context | docker build -f finpay_pipeline/Dockerfile -t "$BASE_IMAGE" -
finpay_context | docker build -f finpay_pipeline/Dockerfile.cell-reporting \
  --build-arg FINPAY_BASE_IMAGE="$BASE_IMAGE" -t "$REPORT_IMAGE" -
finpay_context | docker build -f finpay_pipeline/Dockerfile.cell-scraper \
  -t "$CELL_SCRAPER_IMAGE" -
finpay_context | docker build -f finpay_pipeline/Dockerfile.digipos-scraper \
  -t "$LEGACY_SCRAPER_IMAGE" -
```

Chromium belongs in the Playwright images, not the host. Do not overwrite active
image tags for a canary. Some dependencies/base tags are not fully locked:
record resolved base digests and package versions. Reproducibility here means
promoting the **same tested image digests**, not byte-identical fresh rebuilds.
Dependency-lock/base-digest changes need a separate reviewed change.

## 3. CI Gates And Artifacts

Run these gates before registry publication. Keep results in bounded-retention
CI artifacts, not appended to operating READMEs.

```bash
set -euo pipefail
docker run --rm --pull never --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace \
  "$BASE_IMAGE" python -m unittest discover -s finpay_pipeline/tests
PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest -v \
  tests.test_repository_setup_contracts
docker run --rm --pull never --network none \
  -v "$PWD/finpay_pipeline/workflows:/flows:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 flow validate /flows --local --config /validation.yml
docker run --rm --pull never --network none "$REPORT_IMAGE" python -c \
  "from finpay_pipeline.cell_format.pipeline import preprocess_cell_day, persist_cell_day, publish_cell_sheets; from finpay_pipeline.cell_format.settings import REPORTING_START_DATE; assert REPORTING_START_DATE == '2026-10-01'"
docker run --rm --pull never --network none "$CELL_SCRAPER_IMAGE" python -c \
  "from finpay_pipeline.cell_format.acquisition import acquire_cell_day; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True, args=['--no-sandbox']); assert b.version; b.close(); p.stop()"
docker run --rm --pull never --network none "$LEGACY_SCRAPER_IMAGE" python -c \
  "from finpay_pipeline.legacy.acquisition import acquire_legacy_day"
git diff --check
```

A separate mandatory repository-baseline job must also run
`tests.test_codex_lanes`, root `unittest discover -s tests`, and
`docker compose config --no-env-resolution --quiet`, per the validation guide.
Record failures honestly; do not create unrelated fixtures or silently waive
them because FinPay passes. An unresolved baseline requires owner review before
production promotion. Resolved Compose validation with real secret files belongs
on the protected deployment host, never in public CI logs.

The unit suite mounts source; packaged import/browser smokes intentionally do
not. Also verify packaged module hashes against release source, compile embedded
Python, check subflow references, and run configured lint tools when available.
A missing dependency suite/tool is not a pass. Offline native validation can
warn about unregistered subflows; validate against the actual server as well.

After CI succeeds, publish only to the approved registry using protected secret
handling. Record release SHA, architecture, Dockerfile/dependency/workflow and
migration checksums, resolved package versions, registry digests, and scan/signing
results. Never print secrets or place passwords in command arguments. Registry
publication and production promotion require explicit approval.

## 4. Provision The Runtime

1. Use the approved infrastructure definition. Give coexisting stacks unique
   Compose project names, networks, volumes, and host ports. A second Unix
   account alone does not isolate Docker resources.
2. Keep application data separate from Kestra metadata. Verify
   `current_database()`, `current_user`, and schema against the approved target
   before any migration/write. Destructive tests must never use `finpay`,
   `finpay_txn`, `linkaja`, or `kestra`.
3. Before changing existing data, stop competing writers and take external,
   checksum-verified DB and Sheet backups with tested restore instructions.
4. Under approved bootstrap credentials, explicitly apply
   `migrations/003_finpay_cell_reporting_v1.sql` for cells. Legacy daily has its
   separately documented migration. Do not run monthly/other-domain migrations
   for a cell release. Do not turn bootstrap credentials into task credentials.
5. Verify runtime-role table/schema privileges; do not use a superuser. Use
   secret-file/service-based DB authentication, not passwords in CLI arguments.
6. Supply Kestra secrets `DIGIPOS_PASSWORD`, `FINPAY_DB_HOST`, `FINPAY_DB_PORT`,
   `FINPAY_DB_NAME`, `FINPAY_DB_USER`, `FINPAY_DB_PASSWORD`, and `GCP_SA_KEY`.
   The environment provider uses base64 `SECRET_*` values; this is encoding,
   not encryption. Load only required keys from protected files/CI storage.
   Never put `.env_encoded` in an image or log.
7. Keep Kestra authenticated and internal/loopback or behind the approved TLS
   proxy. Restrict source-import/execution rights and artifact storage: source
   ZIPs and prepared JSON contain private financial evidence.
8. Mount `KESTRA_TASK_WORKDIR` at the same absolute host/container path and set
   the temporary directory to its `tmp/` child. Enable Docker-runner volumes.
   Reporting tasks need the approved finance network, DB access, and Google
   HTTPS; acquisition retains host-network DigiPOS HTTPS egress. Docker socket
   access belongs only to the trusted orchestrator.

Root Compose owns shared infrastructure. Its broad flow-deployment job includes
LinkAja; do not run it as a supposedly FinPay-only action without authorization.
For legacy Sheet publication, additionally provision
`FINPAY_SPREADSHEET_WRITER_EMAILS`, `FINPAY_SPREADSHEET_LOCALE`,
`FINPAY_SPREADSHEET_TIMEZONE`, `FINPAY_PROTECTION_EDITOR_EMAILS`, and
`FINPAY_MANDIRI_EDITOR_EMAILS`; enabled legacy alerts also need
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. These are not cell-path requirements.

## 5. Register The Release

Render reviewed flow YAML with the tested registry image digests. Preserve public
IDs, inputs, task IDs, artifact names, guards, sequential writes, and awaited
subflow failure propagation. Validate rendered files, not just source templates.

The renderer replaces only static `containerImage` fields and refuses missing,
mutable, or unknown image mappings. It emits a new artifact directory and a
checksum manifest, never overwriting source or an existing artifact:

```bash
set -euo pipefail
: "${REPORT_DIGEST:?Set the published reporting repo@sha256 reference}"
: "${CELL_SCRAPER_DIGEST:?Set the published scraper repo@sha256 reference}"
: "${RENDERED_DIR:?Set a new artifact directory under an existing parent}"
python3.11 finpay_pipeline/tests/render_finpay_release.py \
  --pipeline cell --output-directory "$RENDERED_DIR" \
  --report-image "$REPORT_DIGEST" --cell-scraper-image "$CELL_SCRAPER_DIGEST"
docker run --rm --pull never --network none \
  -v "$RENDERED_DIR/flows:/flows:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 flow validate /flows --local --config /validation.yml
```

For `--pipeline legacy`, supply `--base-image` and `--legacy-scraper-image`
instead. `--pipeline both` requires all four. The literal source-to-image mapping
is `finpay-pipeline:3.11` to base, `finpay-cell-reporting:2.0` to reporting,
`finpay-digipos-cell-scraper:2.0` to cell scraper, and
`finpay-digipos-scraper:1.0` to legacy scraper. Only the three primary cell flows or
three primary legacy flows are rendered; compatibility/monthly flow deployment
requires its own explicitly reviewed release artifact.
Register files from the artifact's `flows/` subdirectory; the checksum manifest
sits beside that directory and is not itself a workflow.

Register `finpay_cell_day_v1.yml` before `finpay_cell_range_v1.yml`, namespace
`finance.finpay`, through an authenticated client loading credentials in-process.
Register `finpay_cell_bulk_v1.yml` as the standalone bulk alternative for longer
ranges. It uses the same inputs plus bounded `scrape_workers`; the parallel
acquisition stage still retains per-cell/day browser/login overhead. Use an
execution-specific private cache mount for validated downloads, never a shared
cross-run cache that could silently reuse old source data. Bulk persistence and
publication remain serial, with one four-tab publication per selected cluster.
Read back definitions/image references. If releasing legacy too, register daily
v5 before legacy day, then legacy range. Keep compatibility IDs for their callers.
Do not enable monthly publication or schedules as an incidental deployment step.

Start with this single-cluster range, whose date endpoints are inclusive:

```text
start_date: 2026-10-01
end_date:   2026-10-01
cluster_id: 412110
dry_run: true
write_gsheet: false
```

`412110` selects `412110_B`, cells `412111` and `412112`. Acquisition still
contacts DigiPOS during a dry run; only DB/Google writes are skipped. Use
synthetic or captured artifacts for a genuinely offline check.

For a full alias-named range use `finpay_cell_bulk_v1`, not the per-day range
wrapper:

```text
start_date: 2026-10-01
end_date:   2026-10-19
cluster_id: ALL
scrape_workers: 2
dry_run: false
write_gsheet: true
```

This was exercised against all eight configured clusters (152 cluster/date
cohorts, 342 cell-days). Acquisition took 2,309.4 seconds with two account
workers, about 3.95 cluster-days/minute or 8.88 cell-days/minute. This is one
observed run, not a single-worker comparison or a universal portal throughput
guarantee. Dates within each account remain sequential; DB persistence and Sheet
publication are serial, one publication per cluster. Retain the sanitized run
receipt with CI/evidence, not transaction rows or credentials.
The same native flow's preprocessing stage took 7m53s and its atomic database
stage took 2m30s. The first Sheet attempt stopped at a readback API error after
approximately one minute; it did not rescrape or repeat database persistence.

## 6. Canary And Acceptance

Obtain approval for the account/date, exact four tabs, DB target, migration, and
any test-only reset. A reset requires a verified private backup and exact scope;
it is not normal deployment. Begin with October 1. Inspect a dry acquisition,
then execute the approved non-dry canary with `write_gsheet=true`. Reuse checked
artifacts when retrying a safe stage rather than scraping the whole range again.

Require all of the following:

- Native execution and relevant tasks reach terminal `SUCCESS`.
- `cell_sources_v2.zip` matches exactly the approved account/date and complete
  cell roster; checksums, source validation, and size bounds pass.
- `cell_prepared_v1.json` passes identity/rules/calculation checks. Persistence
  commits that checked calculation rather than silently recomputing the input.
- DB source counts, selected generation/calculation/configuration hashes, and
  full-history handoff agree. No synthetic opening transaction exists; raw source
  balances remain intact.
- Four-tab values, evaluated formulas, formatting, protections, and markers read
  back correctly, and the DB publication epoch is `COMPLETE`. A green tab or A1
  note alone does not prove database completion.
- Summary starts at zero. Offline multi-day tests prove carry-forward; a one-day
  live canary must not be presented as live multi-day coverage.
- Unrelated tab fingerprints and workbook sharing remain unchanged.

### Protection Policy

Summary is strictly protected except each date's `CASH:cash:MANDIRI` DEBET inputs
in D/H/L for up to three cells. Those inputs are restricted to configured Finance
editors and the writer. Invoice, Unusual, and Reversal have no Finance holes.
Temporary whole-tab guards lock inputs while publication is in progress.

The Finance list is global across clusters. The writer and spreadsheet owners
remain privileged exceptions; protection cannot remove owner authority. Range
permissions do not grant workbook sharing. Legacy Summary differs: without
`FINPAY_MANDIRI_EDITOR_EMAILS`, its D-column holes allow workbook editors.

## 7. Promote And Recover

Promote the same validated image digests and flow artifact; do not rebuild
between canary and production. Process remaining dates chronologically in small,
consecutive ranges, stopping on failure. Sequential iteration limits active work,
not total Kestra task-run count.

Before cutover, stop old writers/schedules. Per-flow queueing does not serialize
different flow IDs or Kestra installations. Retain old images/flow artifacts and
verified DB/Sheet backups through the rollback window. Enable schedules only
with separate approval.

For `IN_FLIGHT`, `UNKNOWN`, lost locks, ambiguous Google writes, failed final DB
confirmation, or stale handoffs: stop and retain evidence. Never clear status
rows, erase immutable history, reset protections, or blindly retry. Require an
approved attributable reconciliation. Image rollback does not undo DB/Sheet data.

Clean up only the temporary resources created by an E2E run after evidence
capture. Retain approved backups/sanitized receipts; remove temporary credentials.
Never delete shared volumes or apply test cleanup as production rollback.

## Local Bulk Canary Result

The authorized local run used the copied workbook `Salinan dari Monitoring
Finpay & LinkAja`, a fresh isolated PostgreSQL 18.4 database/application role,
Kestra `v1.3.26`, and immutable locally rebuilt images. It acquired the inclusive
range `2026-10-01` through `2026-10-19` for `cluster_id=ALL` (eight DigiPOS
accounts, 18 cells, 152 account/date cohorts and 342 cell-days). Acquisition used
two concurrent account workers, sequential dates per account, three attempts
for failed cohorts, and checksum-verified per-run cache entries. It completed in
2,309.4 seconds, about 3.95 cluster-days/minute or 8.88 cell-days/minute. This is
one two-worker observation, not a measured single-worker comparison or a general
portal throughput guarantee; each cell/day still incurs the existing portal
login/export overhead.

The database readback verified 342 source manifests, 342 selected cell-days,
342 calculations and ledger row counts matching each manifest. No synthetic
opening transaction was added; raw DigiPOS balance evidence is retained. The
Sheets running SALDO starts at 0 on October 1 and carries forward. The published
tabs use the exact aliases in Section 2 and preserve worksheet IDs. A post-run
audit found the original blank `Sheet72` absent; it was restored at its original
ID/index from the private checksum-verified backup. Its grid hash and workbook
sharing match the pre-run snapshot, and the workbook now has the 32 cell tabs plus
that unrelated sheet. The isolated database and published data remain available
for inspection; this test is not a production deployment.

The first publication task encountered a Google API readback `APIError` after
its writes. The DB record remained `PENDING`; it was not blindly retried. A
read-only reconciliation confirmed all four `412110` tabs matched values,
formulas, styles, protections, marker, epoch and fingerprint, with the October 1
SALDO seed at zero. Under the DB target lock, the verified epoch was recorded as
complete without another Google write. The requested alias migration then
renamed the same 32 worksheet IDs in place. Cluster `412110` retained its first
history epoch and published an alias-metadata epoch; the remaining clusters
published once. A subsequent Sheets API DNS/TLS outage stopped later clusters
before they had a publication record; only those unpublished clusters were
retried on host networking. No completed cluster was replayed.

The final native publication flows completed successfully for all eight cluster
aliases. DB publication epochs and all per-date status rows read back `COMPLETE`.
A name-only alias migration preserves worksheet IDs; `412110_B` retains epoch 1
and records epoch 2 for its alias title, while the other seven clusters use epoch
1. This explains 171 completed epoch/date records but 152 unique cluster/date
pairs in the database. Each native publication returned `COMPLETE` only after
its per-cluster values, formulas, styles, marker, lock and protection readback.
The running total after publication is 152 checked cluster/date sources, 342
cell-days/calculations, and matching immutable raw-ledger row counts.

An independent all-tab verification then passed: IDs/titles, values, evaluated
formulas, styles, COMPLETE markers/fingerprints, and protection policies were
checked for all 32 tabs; unrelated grid contents and workbook permissions match
the backup. It found zero formula errors/mismatches and verified the October 1
running SALDO is 0 in every cluster. Summary keeps only the date/cell MANDIRI
DEBET inputs editable by the configured global Finance list and writer; the
service account and spreadsheet owners remain privileged exceptions. The
sanitized final receipt records 10,584 formula-result checks and zero Google
mutations during verification at
`finpay_pipeline/data/cell_bulk20261020v1/sanitized-final-result.json`.

Google OAuth/Sheets TLS was intermittently unavailable during the run, so the
initial independent full-tab audit was postponed until connectivity returned.
No source rows or credentials are in the receipts. The ignored
`finpay_pipeline/data/cell_bulk20261020v1/` directory contains private source
artifacts and backups; never commit or copy it into a release.
