import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

SKILL_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "digipos-topup-monitoring"
    / "scripts"
    / "download_topup.py"
)

# Three-day requests keep normal all-cluster runs short; a failed request is
# retried as one-day requests because some older Banggai ranges still time out.
EXTRACT_CHUNK_DAYS = 3
FALLBACK_CHUNK_DAYS = 1
DEFAULT_USERS = (
    "411311_A",
    "421306_A",
    "421307_A",
    "421315_A",
    "421318_A",
    "421320_A",
)


def _as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _chunks(start, end, chunk_days=EXTRACT_CHUNK_DAYS):
    cursor = _as_date(start)
    end = _as_date(end)
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _cluster_id(user):
    user = str(user)
    return user[:-2] if user.endswith("_A") else user


def _user_start(start, user):
    if not isinstance(start, dict):
        return _as_date(start)
    cluster_id = _cluster_id(user)
    value = start.get(user, start.get(cluster_id))
    if value is None:
        raise ValueError(f"missing checkpoint start for {user}")
    return _as_date(value)


def _csv_files(directory):
    return sorted(path for path in Path(directory).rglob("*.csv") if path.is_file())


def _csv_row_count(path):
    with path.open(encoding="utf-8", newline="") as source:
        return max(sum(1 for _ in csv.reader(source)) - 1, 0)


def _final_path(output_dir, cluster_id, start, end):
    return (
        Path(output_dir)
        / cluster_id
        / f"finpay-topup-{cluster_id}({start:%d-%m-%Y}to{end:%d-%m-%Y}).csv"
    )


def _merge_csv_files(files, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    header = None
    row_count = 0
    with destination.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        for path in files:
            with path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.reader(source)
                file_header = next(reader, None)
                if file_header is None:
                    continue
                if header is None:
                    header = file_header
                    writer.writerow(header)
                elif file_header != header:
                    raise ValueError(f"inconsistent CSV header in {path.name}")
                for row in reader:
                    writer.writerow(row)
                    row_count += 1
    if header is None:
        raise ValueError("download produced no CSV rows")
    return row_count


def _run_chunk(start, end, user, chunk_dir, password):
    cmd = [
        sys.executable,
        str(SKILL_SCRIPT),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--users",
        user,
        "--output-dir",
        str(chunk_dir),
        "--max-attempts",
        "1",
    ]
    child_env = None
    if password:
        child_env = os.environ.copy()
        child_env["DIGIPOS_PASSWORD"] = password
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=child_env)
    return proc, cmd


def _download_chunk(user, start, end, chunk_dir, password, max_attempts, commands):
    existing = _csv_files(chunk_dir)
    if existing:
        return existing, None
    chunk_dir.mkdir(parents=True, exist_ok=True)
    last_error = "download failed"
    for attempt in range(1, max_attempts + 1):
        proc, cmd = _run_chunk(start, end, user, chunk_dir, password)
        commands.append(" ".join(cmd))
        if proc.returncode == 0:
            produced = _csv_files(chunk_dir)
            if produced:
                return produced, None
            last_error = "download succeeded without a CSV artifact"
        else:
            last_error = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or f"downloader exit code {proc.returncode}"
            )[-500:]
            for partial in _csv_files(chunk_dir):
                partial.unlink()
        print(
            f"event=topup_extract_chunk_failed user={user} "
            f"start={start} end={end} attempt={attempt} "
            f"error_type={type(last_error).__name__ if last_error else 'unknown'}",
            file=sys.stderr,
        )
    return [], last_error


def run_extract(
    start,
    end,
    users=None,
    output_dir="finpay-topup-inbox",
    password=None,
    max_attempts=2,
):
    """Download independent date chunks and preserve successful artifacts."""
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(f"DigiPOS skill script not found: {SKILL_SCRIPT}")

    users = list(DEFAULT_USERS if users is None else users)
    end_date = _as_date(end)
    root = Path(output_dir)
    summaries = []
    failures = []
    commands = []
    for user in users:
        cluster_id = _cluster_id(user)
        user_start = _user_start(start, user)
        final = _final_path(root, cluster_id, user_start, end_date)
        if final.exists() and final.stat().st_size > 0:
            summaries.append({
                "user": user,
                "status": "reused",
                "file": str(final),
                "rows": _csv_row_count(final),
            })
            continue
        if user_start > end_date:
            failures.append({"user": user, "error": "checkpoint start is after end date"})
            continue

        chunk_files = []
        user_failed = False
        for chunk_start, chunk_end in _chunks(user_start, end_date):
            chunk_dir = root / "chunks" / cluster_id / f"{chunk_start.isoformat()}_{chunk_end.isoformat()}"
            produced, last_error = _download_chunk(
                user, chunk_start, chunk_end, chunk_dir, password, max_attempts, commands
            )
            if last_error is None:
                chunk_files.extend(produced)
                continue
            fallback_start = fallback_end = None
            if chunk_start != chunk_end:
                fallback_files = []
                fallback_error = None
                for fallback_start, fallback_end in _chunks(
                    chunk_start, chunk_end, FALLBACK_CHUNK_DAYS
                ):
                    fallback_dir = (
                        root
                        / "fallback_chunks"
                        / cluster_id
                        / f"{fallback_start.isoformat()}_{fallback_end.isoformat()}"
                    )
                    produced, fallback_error = _download_chunk(
                        user,
                        fallback_start,
                        fallback_end,
                        fallback_dir,
                        password,
                        max_attempts,
                        commands,
                    )
                    if fallback_error is not None:
                        break
                    fallback_files.extend(produced)
                if fallback_error is None:
                    chunk_files.extend(fallback_files)
                    continue
                last_error = fallback_error
            if last_error is not None:
                failures.append({
                    "user": user,
                    "start": (fallback_start or chunk_start).isoformat(),
                    "end": (fallback_end or chunk_end).isoformat(),
                    "error": last_error[-500:],
                })
                user_failed = True
                break

        if user_failed:
            continue
        try:
            row_count = _merge_csv_files(chunk_files, final)
            shutil.rmtree(root / "chunks" / cluster_id, ignore_errors=True)
        except Exception as exc:
            failures.append({"user": user, "error": str(exc)[-500:]})
            continue
        summaries.append({
            "user": user,
            "status": "downloaded",
            "file": str(final),
            "rows": row_count,
        })

    result = {
        "users": summaries,
        "failures": failures,
        "chunks": {
            "days": EXTRACT_CHUNK_DAYS,
            "retry_failed_only": True,
        },
    }
    return {
        "returncode": 1 if failures else 0,
        "stdout": json.dumps(result, ensure_ascii=False, sort_keys=True),
        "stderr": json.dumps(failures, ensure_ascii=False, sort_keys=True),
        "command": "per-cluster 3-day chunk downloads with 1-day fallback",
        "commands": commands,
    }
