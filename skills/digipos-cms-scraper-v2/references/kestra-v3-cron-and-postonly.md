# Kestra v3 Cron + POST-only Backfill Notes

## Current daily cron target

The active Hermes cron job is:

- name: `finpay monitoring`
- job_id: `ceb4b7721152`
- schedule: `0 5 * * *`
- profile: `alex`
- workdir: `/home/mmpp`
- delivery: origin chat

It should POST only to:

```text
http://localhost:8080/api/v1/executions/finance.finpay/finpay_daily_pipeline_v3
```

Daily command shape:

```bash
Y=$(date -d yesterday +%F)
python3 /home/mmpp/.hermes/profiles/alex/skills/digipos-cms-scraper/scripts/finpay_backfill.py \
  --start "$Y" \
  --end "$Y" \
  --kestra-url http://localhost:8080/api/v1/executions/finance.finpay/finpay_daily_pipeline_v3 \
  --poll-seconds 10 \
  --timeout-seconds 1800 \
  --post-attempts 3 \
  --post-retry-sleep 30
```

Expected daily success:

```text
downloads_ok=6
posts_ok=6
skipped_no_data=0
failed=0
```

## Current expected workflow usage

- `finpay_daily_pipeline_v3`: active target for daily cron and current backfills.
- `finpay_daily_pipeline`: legacy; not used by current cron.
- `finpay_daily_pipeline_v2`: old test workflow; not used by current cron.

## Checking status

Hermes cron:

```text
cronjob(action="list")
```

OS cron/timers checks used during incident analysis:

```bash
crontab -l
sudo grep -RInE 'finpay|kestra|digipos|trigger_kestra|finpay_backfill|post_finpay' /etc/cron* /var/spool/cron /var/spool/cron/crontabs 2>/dev/null
systemctl list-timers --all | grep -Ei 'finpay|kestra|digipos|hermes'
ps -ef | grep -Ei 'finpay|kestra|digipos|post_finpay|trigger_kestra|finpay_backfill' | grep -v grep
```

Kestra flow schedules/triggers can be checked by fetching flow JSON and inspecting `triggers`; current finpay flows had `triggers: []` during analysis.

## 11:00 rerun diagnosis pattern

When user suspects an unexpected rerun:

1. List Hermes cron jobs first.
2. Check OS cron and systemd timers.
3. Check Kestra flow `triggers`.
4. Query Kestra execution history with WITA conversion and filenames from `parse_and_resolve` outputs.
5. Compare times with manual backfill/test commands from conversation.

In this session, 11:00 all-cluster v3 runs were caused by manual POST-only backfill tests (`/tmp/post_finpay_v3_existing.py`), not an active cron. Normal shell history may not show Hermes terminal commands; use process/session outputs or Kestra execution timestamps.

## POST-only backfill when CSVs are already validated

If files already came from the current scraper and no re-download is needed, validate local coverage first:

- expected clusters: `411311`, `421306`, `421307`, `421315`, `421318`, `421320`
- expected range: `2026-06-01` through yesterday for historical backfill, or one date for daily
- filename pattern: `finpay-{cluster}({DD-MM-YYYY}to{DD-MM-YYYY}).csv`
- reject missing file, header-only CSV, or date mismatch

Then POST in fixed cluster order and date ascending, waiting each Kestra execution before next POST.

## v3 failure mode found and fixed upstream

`finpay_daily_pipeline_v3` revision 2/3 failed on unusual transaction notification because Kestra secret was missing:

```text
SecretNotFoundException: Cannot find secret for key 'TELEGRAM_BOT_TOKEN'
task: notify_unusual_telegram
parent: branch_after_unusual_flag
```

Revision 4 later passed full POST-only backfill:

```text
posts_ok=102
failed=0
range: 2026-06-01 -> 2026-06-17
```

If this failure recurs, fix workflow secret handling or make notification failure non-fatal before retrying backfill.
