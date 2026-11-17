# FinPay Production Replication

Agent-readable build, CI/CD, commissioning, and recovery procedure. Read
[AGENTS.md](AGENTS.md), [README.md](README.md), and repository
[security](../docs/security.md), [architecture](../docs/architecture.md), and
[validation](../docs/validation.md) rules before acting.

This procedure does not authorize production deployment, infrastructure creation,
changes to sharing on existing workbooks, production resets or repairs, credential
rotation, commits, or registry publication. Development destructive tests are
separate from production replication and may run only against dedicated disposable
non-production databases and workbooks, using synthetic data and code-level target
guards. Every reset, repair, or cleanup requires fresh, explicit authorization from
the current user for the exact target, scope, operation, and backup plan; any
no-backup exception requires fresh, explicit authorization from the current user
for that consequence. Section 6.1 records historical approval only, not current or
future authorization. Never point test resets or cleanup at production resources.
Cell workflows require an existing workbook and exact existing worksheet IDs/titles;
they do not create destinations or change sharing. Obtain current approval for
production targets and operations.

Before a live Sheet mutation, verify the exact workbook and role tabs and perform
a read-only audit of the selected date blocks against persisted rows. Obtain
current authorization for the exact write scope; historical execution receipts
and reset approvals do not authorize additional writes.
No hosted CI workflow is installed here; the gates below define what CI/CD must
execute.

## 1. Freeze And Configure

1. Use an approved, reviewed source revision. Stop on a dirty checkout: a commit
   SHA does not identify uncommitted code. Do not commit without authorization.
2. Inventory the active writers, schedules, DB identities, workbook ID, worksheet
   IDs, and backups. A workbook title alone is not a unique identity.
3. Configure one existing destination and cluster group for each bulk flow.
   Set `cell_spreadsheet_name` from Kestra secret `FINPAY_CELL_SPREADSHEET_NAME`
   or a private flow copy, and pin `cell_spreadsheet_id` when known. Set the
   distinct `legacy_spreadsheet_id` and `cell_cluster_group`. The group accepts
   one numeric cluster, a comma-separated list, or `ALL`; it may be read from
   `secret('FINPAY_CELL_CLUSTER_GROUP')`. Give each cloned flow a unique ID and
   keep groups disjoint. The daily/range wrappers use the deployment's shared
   target secrets. Do not commit workbook IDs, Finance emails, or credentials.
4. Configure the writer service-account email and Finance editor identities as
   Kestra secrets. The package title scheme is `cluster_alias`; worksheet IDs are
   verified against exact alias titles, with an explicit ID map when pinned.
5. Resolve aliases from `CLUSTER_ALIASES` and verify exact titles `<alias>`,
   `<alias> Invoice`, `<alias> Unusual`, and `<alias> Reversal`. Current aliases
   are `412110_B=PKY`, `422090_B=MRT`, `422100_B=TDR`, `422010_B=BGI`,
   `422180_B=MRW`, `422220_B=TNT`, `422140_B=KOTAMOB`, and `422040_B=BOLMONG`.
    Keep workbook and worksheet IDs stable during a code-to-alias rename; changing
    IDs, workbook, account ownership, or editors is not a title migration. Before
    execution, read workbook metadata and take the approved backup. The read-only
    preflight requires all four existing role tabs with exact IDs and titles before
    persistence. Missing or mismatched targets fail closed. Existing workbook
    sharing is never changed, and no tab is created, renamed, restored, deleted,
    or cleared by the workflow.
6. Agree whether this release includes cell, legacy, or monthly reporting.
   A cell canary does not verify legacy/monthly live behavior or other domains.

### October 1 And SALDO

The new cell history starts on **2026-10-01**, in `Asia/Makassar`. The first
displayed running SALDO starts at **0**, then formulas carry it through rows/days;
it is not reset per execution or stored as a cumulative database value. Do not
insert an opening transaction, adjustment, or separate balance table. Existing
configuration provenance records zero/date for hashing; raw DigiPOS balances
remain immutable source evidence. Combined MANDIRI is formula-derived from the
following date's `CASHOUT APOLLO` KREDIT and shows `PENDING NEXT DAY` when absent.

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
   before any migration/write. Run destructive tests only against dedicated
   disposable non-production databases with code-level target guards. Section 6.1
   documents a past, specifically approved departure from these restrictions and
   does not establish a current exception. Never
   use `finpay`, `finpay_txn`, `linkaja`, or `kestra` for destructive tests,
   and never direct test cleanup at production databases or workbooks.
3. Before changing existing data, stop competing writers and take external,
   checksum-verified DB and Sheet backups with tested restore instructions.
4. Under approved bootstrap credentials, explicitly apply
   `migrations/003_finpay_cell_reporting_v1.sql` for cells. Legacy daily has its
   separately documented migration. Do not run monthly/other-domain migrations
   for a cell release. Do not turn bootstrap credentials into task credentials.
5. Verify runtime-role table/schema privileges; do not use a superuser. Use
   secret-file/service-based DB authentication, not passwords in CLI arguments.
6. Supply Kestra secrets `DIGIPOS_PASSWORD`, `FINPAY_DB_HOST`, `FINPAY_DB_PORT`,
   `FINPAY_DB_NAME`, `FINPAY_DB_USER`, `FINPAY_DB_PASSWORD`, `GCP_SA_KEY`,
   `FINPAY_CELL_SPREADSHEET_NAME`, `FINPAY_LEGACY_SPREADSHEET_ID`,
    `FINPAY_CELL_WRITER_EMAIL`, and `FINPAY_CELL_FINANCE_EMAILS`. An optional
    `FINPAY_CELL_WORKSHEET_IDS` JSON map can pin role IDs; to use that secret, set
    the copied flow variable `cell_worksheet_ids_json` to
    `{{ secret('FINPAY_CELL_WORKSHEET_IDS') }}`. Otherwise the package resolves
    alias worksheet IDs by title. A spreadsheet ID secret is only needed when the
    copied flow references it; name-only resolution requires one exact matching
    Drive title. `FINPAY_CELL_CLUSTER_GROUP` may set the cloned flow's default
    group by referencing it from `cell_cluster_group`.
   The environment provider uses base64 `SECRET_*` values; this is encoding, not
   encryption. Load only required keys from protected files/CI storage. Never
   put `.env_encoded` in an image or log.
7. Grant the cell service account only Sheets and `drive.file` scopes for Sheets
   writes and new-file creation, plus Drive metadata-read scope for unique-name
   lookup. A newly created workbook is owned by that service account and shared
   only with the configured Finance editors. Existing workbook sharing is never
   changed automatically; missing permissions fail closed.
8. Keep Kestra authenticated and internal/loopback or behind the approved TLS
   proxy. Restrict source-import/execution rights and artifact storage: source
   ZIPs and prepared JSON contain private financial evidence.
9. Mount `KESTRA_TASK_WORKDIR` at the same absolute host/container path and set
   the temporary directory to its `tmp/` child. Enable Docker-runner volumes.
   Reporting tasks need the approved finance network, DB access, and Google
   HTTPS; acquisition retains host-network DigiPOS HTTPS egress. Docker socket
   access belongs only to the trusted orchestrator.
10. Before running `finpay_cell_day_v1`, create and restrict the Docker-host
    directory `/tmp/finpay inbox`; its task runner bind-mounts that exact path.
    It receives checksum-verified source workbooks under
    `<cluster>/<date>/<inbox_generation_sha256>/<cell>/` using the legacy
    cluster/date filename. A cohort is promoted as one content-addressed
    directory; changed sources create a new generation. The flow does not purge
    this inbox or treat it as a backup; the generation directory matches the
    acquisition task's `inbox_generation_sha256` output, and host `/tmp` cleanup
    policy applies.

Root Compose owns shared infrastructure. Its broad flow-deployment job includes
LinkAja; do not run it as a supposedly FinPay-only action without authorization.
For legacy Sheet publication, additionally provision
`FINPAY_SPREADSHEET_WRITER_EMAILS`, `FINPAY_SPREADSHEET_LOCALE`,
`FINPAY_SPREADSHEET_TIMEZONE`, `FINPAY_PROTECTION_EDITOR_EMAILS`, and
`FINPAY_MANDIRI_EDITOR_EMAILS`; enabled legacy alerts also need
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Cell alerts use the same secrets only
when `send_telegram=true`; keep this input false during isolated verification.
Check the terminal notification task separately from financial execution success.

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
publication remain serial, with one date-block publication per selected
cluster/date.

For two destination workbooks, clone `finpay_cell_bulk_v1` twice, assign unique
flow IDs, and change the clone variables (or their `secret()` names). Example:

```yaml
variables:
  cell_spreadsheet_name: "{{ secret('FINPAY_CELL_GROUP_A_SPREADSHEET_NAME') }}"
  cell_spreadsheet_id: "" # Optional; pin when the exact file ID is known.
  cell_cluster_group: "412110,422010"
```

The second clone uses its own destination name and cluster list. The execution
input `cluster_id` defaults to the group but can still be overridden per run.
Each destination workbook and all constant alias role tabs must already exist;
missing or mismatched targets fail the read-only preflight before persistence.
Existing workbook sharing is not changed.
Read back definitions/image references. If releasing legacy too, register daily
v5 before legacy day, then legacy range. Keep compatibility IDs for their callers.
Keep `finpay_cell_range_v1`'s `daily_cell_range_0500_wita` trigger disabled when
registering the flow. It targets the previous day at 05:00 `Asia/Makassar` for
`ALL` clusters; activation requires separate approval. Do not enable monthly
publication or schedules as an incidental deployment step.

### Execute A Per-Day Range

`finpay_cell_range_v1` is the sequential range wrapper; it starts one
`finpay_cell_day_v1` child for each cluster/date cohort. It persists every selected
cohort before the separate publication pass writes only those date blocks. Keep
its scheduled trigger disabled and execute it manually from the Kestra UI:

Before running, set the flow variable `cell_reporting_network` to the exact
shared Docker network that can resolve the configured database host. This is a
flow variable, not an execution input; the range forwards it to each day child.
Do not assume the default `bridge` resolves Compose-local service names.

1. Open `finance.finpay.finpay_cell_range_v1` and choose **Execute**. Enter ISO
   dates in `start_date` and `end_date`; both endpoints are inclusive.
2. For a first validation, use one configured cluster and one date with
   `dry_run=true`, `write_gsheet=false`, and `send_telegram=false`:

```text
start_date: 2026-10-01
end_date:   2026-10-01
cluster_id: 412110
dry_run: true
write_gsheet: false
send_telegram: false
```

`412110` selects account alias `412110_B` and DigiPOS cells `412111` and
`412112`.

3. Submit the execution and monitor the parent range plus its child execution.
   Acquisition still contacts DigiPOS during a dry run; DB and Google writes are
   skipped. Use synthetic or captured artifacts for a genuinely offline check.
4. Only after target and data-write approval in the test environment, set
   `dry_run=false` and `write_gsheet=true`. Keep `send_telegram=false` unless
   notifications are intentionally enabled. Start with one date/cluster, then
   expand `cluster_id` to a comma-separated group or `ALL` and widen the date
   range after verifying the first cohort.
5. Confirm the parent and every child reach terminal `SUCCESS`, then verify the
   selected DB current rows and four requested date blocks. A green tab alone is
   not proof of DB persistence; compare it with the committed current-result view.

For live Sheet writes, the range pauses 30 seconds after each successful
cluster/date cohort. Each cell Sheets client spaces HTTPS requests by five seconds.
Failed operational tasks retry up to five times after the initial attempt, with
exponential waits of 30 seconds, 60 seconds, then a 120-second cap. After the
child exhausts its retries, the parent stops rather than restarting the entire
day. Mutation requests themselves are not retried by the Google HTTP adapter.
Rerunning a cohort is idempotent: it reads current persisted data and replaces or
appends only the matching column-A report-date block in the same four worksheets.
Earlier source generations and other dates remain intact; row-count changes resize
only the requested block.

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
publication are serial, one publication per requested date/cluster. Retain the sanitized run
receipt with CI/evidence, not transaction rows or credentials.
The same native flow's preprocessing stage took 7m53s and its atomic database
stage took 2m30s. The first Sheet attempt stopped at a readback API error after
approximately one minute; it did not rescrape or repeat database persistence.

## 6. Canary And Acceptance

Development E2E checks, including destructive reset/cleanup, are separate from
production commissioning and may continue while development is in progress. Keep
them on dedicated disposable non-production database and workbook targets, with
synthetic data and code-level target guards. Obtain fresh, explicit current-user
authorization for each destructive run as stated above; Section 6.1 is historical
context only, not a reusable approval. Production commissioning approval is not a
prerequisite for isolated development checks.

### 6.1 One-Time Development Reset Exception

Historical record only. This section preserves the approval, safeguards, scope,
and no-backup instruction for one development-state disposal. The approval and
instruction applied only to that operation; this record grants no present or
future authorization and establishes no current no-backup instruction or waiver.
The details below describe historical approved conditions, not an execution
procedure. Any new operation requires fresh, explicit current-user authorization
under the rules above, with any no-backup exception expressly included.

At the time, the user explicitly approved this one-time exception, including
removal of the existing `UNKNOWN` operation and publication/source history. It
overrode only the development-test restrictions in Sections 1, 4.2, and 6; it was
not a production canary, release, or reusable reset authorization.

The documented operation was a controlled disposal of one development test state,
not an application recovery path. Normal source-generation/ledger append-only and
publication recovery rules remain unchanged for application behavior and every
other target.
For that disposal, the user also explicitly instructed that no new development
data backups be created. This historical instruction waived the backup/restore
prerequisites in Section 4 step 3, the pre-provisioning backup in Section 1 step 5,
and the linked security guide only for that exact confirmed development state. It
is not a no-backup instruction for any later operation. The discarded state was
not guaranteed recoverable. Existing backups made before that instruction were
not removed by the exception. Production and all other resets remained subject to
their backup and evidence-retention requirements.

- Database scope and guard: The approval covered `finpay`, schema `public`, only
  after connection preflight established that the endpoint was the intended
  development instance and not production. Each destructive SQL transaction was
  required to abort unless `current_database() = 'finpay'` and the selected schema
  was `public`; application preflight did not replace this SQL guard. The approval
  excluded `finpay_txn`, `linkaja`, and `kestra`.
- Workbook scope: The approval required the exact user-approved ID to be supplied
  from the private, mode-restricted execution profile and verified by live
  metadata. Before any Sheet write, the procedure required independent evidence
  from a trusted environment inventory or explicit owner confirmation that the
  workbook was a non-production development/test copy. Without that evidence, or
  if the file was the production workbook, the run was to stop; production writes
  remained subject to ordinary production-canary approval and gates. The
  non-production copy was also required to match `Monitoring Finpay (MMPP & GMM)`;
  a title mismatch blocked the run. The ID was not to be stored in this tracked
  document or public logs.
- Data scope: The approval covered daily cell reporting for `2026-10-01` through
  `2026-10-25` inclusive, for all eight configured clusters (`412110`, `422010`,
  `422040`, `422090`, `422100`, `422140`, `422180`, and `422220`).
- Deletion controls: Before deletion, the procedure required an in-memory allowlist
  of exact source-generation and calculation IDs, cluster/cell/date keys, source
  fingerprints, and selected current rows. For every selected publication epoch,
  it required recording the exact `(cluster_id, target_id, epoch)`, range, status,
  error, confirmed chunks, `snapshot_fingerprint`, and canonical snapshot hash.
  The live key sets and recorded values had to match in the guarded transaction;
  deletion was limited to those exact rows and their FK children. Any extra or
  missing row, changed fingerprint/status, unexpected status, or `IN_FLIGHT` state
  would have blocked the reset. The procedure prohibited persisting this dev data
  as a new receipt or backup and prohibited date-only predicates from widening
  deletion beyond the in-memory allowlist.
- Data removal scope: The approval covered possible removal of only the selected
  cell source generations, ledger, calculations, current selections, dirty dates,
  publication snapshots/date rows, and manual-reset records for the selected
  clusters and inclusive date range. Configuration provenance was to remain unless
  reset preflight proved replacement necessary. A selected publication epoch
  starting before October 1 or ending after October 25 would have blocked the
  reset; partial removal of an out-of-range publication snapshot was excluded.
- Target-operation scope: The target-operation table had no date key. Removal was
  limited to a row for the approved workbook ID with exactly
  `cluster_id=422180`, `status=UNKNOWN`, `error_class=APIError`, and the captured
  `operation_id` and `started_at`, provided the in-memory preflight found exactly
  one such row. This was the only target-operation row in scope. Any mismatch,
  additional row, or `IN_FLIGHT` state would have blocked the reset; its complete
  row fingerprint was to be compared in memory.
- Worksheet and data boundaries: The approval permitted creation of at most 32
  expected alias-named role tabs confirmed missing at preflight. It excluded
  deletion or rewriting of pre-existing worksheets, workbook-sharing changes, and
  modifications to non-cell tables, other schemas, other destinations, or records
  outside the stated clusters/date range.

The historical procedure required all writers and active executions to be
quiescent; database, host/instance, role, and schema checks were to be completed
without printing credentials, and exact before-state counts and target identities
were to be compared in memory. The contemporaneous no-backup instruction
prohibited a new database dump, workbook snapshot, data archive, or backup fixture
for that run. Retention was limited to normal workflow operational records/artifacts
and sanitized counts/status needed to report execution. Pre-existing worksheet
state and sharing were to be compared in memory and remained outside the mutation
scope. Failed or ambiguous publication was to be retained for reconciliation,
not blindly retried or rolled back; restoration of the discarded state required
separate approval.

The historical procedure specified a transactionally guarded reset with exact
target predicates, FK-safe delete order, and asserted before/after counts. It
prohibited blanket `TRUNCATE`, deletion of other targets, and continuation after
unexpected state. The image scope was limited to uniquely tagged local test images
from the reviewed source snapshot; release tags were not to be overwritten and
images were not to be published. The approved procedure called for one run of the
full inclusive range. A failed or ambiguous stage would have stopped the run;
only normal workflow outputs and sanitized execution status were to be retained,
not a new receipt, archive, or backup. Another reset or destructive retry required
fresh approval. The prescribed independent verification covered all 200
cluster/date cohorts and 450 cell-days, database source/current/calculation
consistency, four-tab values and evaluated formulas, publication markers and
fingerprints, protections, and unchanged pre-existing worksheet metadata/content
and workbook sharing. The historical approval did not authorize a scheduler,
production deployment, commit, push, or registry publication.

### 6.2 One-Time 2026-11-15 Development Reset Authorization

Status: approved by the current user; volume reset and schema recreation completed
on 2026-11-15. The first range execution failed in the day-child database preflight
before acquisition, persistence, or Sheet publication. The failed child used Docker's
default bridge and could not resolve the Compose-local `postgres` hostname. No cell
data or Sheet writes occurred. This is a one-time authorization for the current
confirmed non-production stack and workbook; it does not authorize any later reset.

- Scope: run `finpay_cell_range_v1` for `2026-10-01` through `2026-11-14`
  inclusive with `cluster_id=ALL`, `dry_run=false`, `write_gsheet=true`, and
  `send_telegram=false`. This is 45 dates across the eight configured clusters.
  `DISBURSEMENT` remains included whenever present in the source labels.
- Volume reset: after full external backups and successful restore tests, remove
  only `mmpp-finpay-replication-20261021_postgres-data` and
  `mmpp-finpay-replication-20261021_kestra-data`. The user explicitly accepts
  loss of every database and Kestra execution/flow record in those volumes for
  this dev reset. Preserve the `mmpp-finpay-awsdx` and
  `mmpp-finpay-synthetic-20261021` projects and the exited provisioner's
  anonymous volume. Do not use a global volume/image prune.
- Manual values: the user explicitly authorizes discarding prior dev-only MANDIRI
  manual inputs from the reset state. This does not authorize discarding manual
  inputs from any other workbook or environment.
- Workbook: retain the same verified non-production spreadsheet and its sharing.
  Preserve the exact saved 32 worksheet IDs/titles, styles generated by the cell
  publisher, and the unrelated pre-existing tab. Do not publish until the pinned
  worksheet-ID map is configured and the native flow validator passes.
- Backups: the current external private backup includes a full PostgreSQL-cluster
  dump, Kestra storage archive, workbook XLSX/grid/metadata/permissions snapshot,
  worksheet identity map, and the prior manual-input snapshot. The PostgreSQL
  restore test covered all seven databases; the Kestra store restore test matched
  all 143 archived entries. Keep these backups through final acceptance.
- Runtime: Kestra is stopped during reset. Re-provision the project databases,
  apply base migration `003_finpay_cell_reporting_v1.sql` to `finpay`; apply
  `004_finpay_cell_remove_publication_state.sql` only after separate approval and
  a verified backup. Deploy only the range/day flows with verified current images. Set and forward
  `cell_reporting_network` to `mmpp-finpay-replication-20261021_mmpp-finance-network`
  on Docker reporting task runners; DigiPOS acquisition retains its host network.
  Keep the range schedule disabled; do not run the broad flow-deployer that also
  publishes LinkAja flows.
- Failure handling: an execution that fails before acquisition and has verified
  no DB or Sheet writes may be resubmitted after fixing configuration; do not reset
  again. For any ambiguous historical Sheet outcome, perform a read-only comparison
  first and obtain fresh authorization before another live write. Unpinned worksheet
  identities, failed preflight, changed sharing, or extra resources remain hard stops.

For the production canary, obtain approval for the account/date, exact four tabs,
DB target, migration, and planned writes. Limit it to the approved DB/Sheet
publication for that scope; do not run destructive tests, resets, or cleanup, or
clear existing production state. Begin with October 1. Inspect a dry acquisition,
then execute the approved non-dry canary with `write_gsheet=true`. Reuse checked
artifacts when retrying a safe stage rather than scraping the whole range again.

Require all of the following:

- Native execution and relevant tasks reach terminal `SUCCESS`.
- `cell_sources_v2.zip` matches exactly the approved account/date and complete
  cell roster; checksums, source validation, and size bounds pass.
- `cell_prepared_v1.json` passes identity/rules/calculation checks. Persistence
  commits that checked calculation rather than silently recomputing the input.
- DB source counts, selected generation/calculation/configuration hashes, and
  requested date/cell coverage agree. No synthetic opening transaction exists;
  raw source balances remain intact.
- Four-tab requested date blocks and formulas match persisted current data after
  batched readback. A green tab alone does not prove database persistence.
- Summary starts at zero. Offline multi-day tests prove carry-forward; a one-day
  live canary must not be presented as live multi-day coverage.
- Unrelated tab fingerprints and workbook sharing remain unchanged.

### Protection Policy

Cell Summary, Invoice, Unusual, and Reversal are formula/value presentation
outputs. The writer maintains a managed display-only protection per tab and
removes legacy MANDIRI input holes and temporary publisher guards. It leaves
unmanaged protections and workbook sharing unchanged. The configured service
account must already have the required workbook access; workbook owners retain
their Google privileges.
Previous-format Summary/MANDIRI protection behavior remains separate.

## 7. Promote And Recover

Promote the same validated image digests and flow artifact; do not rebuild
between canary and production. Process remaining dates chronologically in small,
consecutive ranges, stopping on failure. Sequential iteration limits active work,
not total Kestra task-run count.

The per-cluster/day flow retains its configured Kestra retry/backoff policy; the
Google HTTP adapter does not retry mutation requests. A retry rereads current DB
facts and idempotently replaces the requested date block. The range's 30-second
cohort pause and sequential order remain in force.

Before cutover, stop old writers/schedules. Per-flow queueing does not serialize
different flow IDs or Kestra installations. Retain old images/flow artifacts and
verified DB/Sheet backups through the rollback window. Enable schedules only
with separate approval.

For an ambiguous prior live Sheet outcome, stop and retain evidence. Compare the
affected date blocks with persisted DB data using read-only access, then obtain
fresh approval before any corrective write. Do not create or restore a missing
worksheet as a recovery shortcut. Image rollback does not undo DB/Sheet data.

### Existing Destination Blockers

Missing or mismatched workbooks/tabs block before persistence when Sheets output
is enabled. Resolve destination structure through a separately approved workbook
administration procedure, then rerun the read-only preflight. The cell workflow
never creates, renames, shares, restores, or adopts a destination. Migration 004
removes the retired database write-state tables only after separate approval and
a verified backup; it is not an automatic workflow action.

Clean up temporary E2E resources after evidence capture. A dev-only volume reset
requires exact user approval, stopped consumers, and a verified backup/restore
plan for every database and Kestra store in the named volumes; no-backup reset
requires explicit approval. Never use `docker volume prune` or apply this rule to
production/shared volumes. Remove temporary credentials.

## Local Bulk Canary Result

Historical evidence only: this run exercised the prior publication-state writer,
not the current stateless date-block writer. It does not resolve the outstanding
read-only audit or authorize a new live Sheet mutation.

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
