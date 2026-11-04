"""Bounded, secret-safe progress logging for FinPay cell acquisition."""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import date

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_TEXT = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_CLUSTER_CODE = re.compile(r"[0-9]{6}\Z")
_ACCOUNT_CODE = re.compile(r"[0-9]{6}_B\Z")
_CELL_CODE = re.compile(r"[0-9]{6}\Z")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TEXT_FIELDS = {
    "cluster_id", "report_date", "start_date", "end_date", "account",
    "cell_code", "role",
}
_COUNT_FIELDS = {"attempt", "completed", "total", "rows", "bytes", "count"}
_ALLOWED_FIELDS = _TEXT_FIELDS | _COUNT_FIELDS
_OUTPUT_LOCK = threading.Lock()


def _write_kestra_log(record: dict) -> None:
    message = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    envelope = {"logs": [{"level": "INFO", "message": message}]}
    line = "::" + json.dumps(envelope, separators=(",", ":"), allow_nan=False) + "::\n"
    with _OUTPUT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _validate_fields(fields: dict) -> dict:
    unknown = fields.keys() - _ALLOWED_FIELDS
    if unknown:
        raise ValueError("unsupported progress field")

    safe = {}
    for name, value in fields.items():
        if name in _TEXT_FIELDS:
            normalized = _normalize_text_field(name, value)
            if normalized is not None:
                safe[name] = normalized
        elif name == "total" and value is None:
            safe[name] = None
        else:
            if type(value) is not int or value < 0 or (name == "attempt" and value < 1):
                raise ValueError("invalid progress count field")
            safe[name] = value

    total = safe.get("total")
    completed = safe.get("completed")
    if total is not None and completed is not None and completed > total:
        raise ValueError("progress completed count exceeds total")
    return safe


def _normalize_text_field(name: str, value) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    if name == "cluster_id":
        selector = value.strip()
        if selector == "ALL":
            return selector
        groups = [group.strip() for group in selector.split(",")]
        if groups and all(_CLUSTER_CODE.fullmatch(group) for group in groups):
            return ",".join(groups)
        return None
    if name in {"report_date", "start_date", "end_date"}:
        if not _ISO_DATE.fullmatch(value):
            return None
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value
    if name == "account":
        return value if _ACCOUNT_CODE.fullmatch(value) else None
    if name == "cell_code":
        return value if _CELL_CODE.fullmatch(value) else None
    if name == "role":
        return value if _IDENTIFIER.fullmatch(value) else None
    return value if _SAFE_TEXT.fullmatch(value) else None


class Progress:
    """Emit allowlisted progress records and a periodic heartbeat.

    The injected logger receives a sanitized record dictionary. By default the
    record is wrapped in Kestra's script-task ``::...::`` stdout envelope.
    """

    def __init__(
        self,
        stage: str,
        *,
        total: int | None = None,
        logger: Callable[[dict], None] | None = None,
        clock: Callable[[], float] | None = None,
        heartbeat_interval: float | None = 30,
        **fields,
    ) -> None:
        if not isinstance(stage, str) or not _IDENTIFIER.fullmatch(stage):
            raise ValueError("invalid progress stage")
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("invalid progress total")
        if logger is not None and not callable(logger):
            raise ValueError("progress logger must be callable")
        if clock is not None and not callable(clock):
            raise ValueError("progress clock must be callable")
        if heartbeat_interval is not None and (
            isinstance(heartbeat_interval, bool)
            or not isinstance(heartbeat_interval, (int, float))
            or not math.isfinite(heartbeat_interval)
            or heartbeat_interval <= 0
        ):
            raise ValueError("heartbeat interval must be positive or None")

        initial = _validate_fields(fields)
        if total is not None:
            initial["total"] = total
        if "total" in initial and "completed" not in initial:
            initial["completed"] = 0
        _validate_fields(initial)

        self.stage = stage
        self._fields = initial
        self._logger = logger if logger is not None else _write_kestra_log
        self._clock = clock if clock is not None else time.monotonic
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._phase = "stage_start"
        self._phase_started_at: float | None = None
        self._eta_started_at: float | None = None
        self._eta_completed_base = 0
        self._entered = False
        self._closed = False
        self._logging_failed = False

    def __enter__(self) -> Progress:
        with self._lock:
            if self._entered:
                raise RuntimeError("progress context cannot be re-entered")
            self._started_at = self._clock()
            self._eta_started_at = self._started_at
            self._eta_completed_base = self._fields.get("completed", 0)
            self._entered = True
            self._emit_locked("stage_start")
            if self._heartbeat_interval is not None and not self._logging_failed:
                self._thread = threading.Thread(
                    target=self._heartbeat_loop,
                    name=f"progress-{self.stage}",
                    daemon=True,
                )
                self._thread.start()
        return self

    def update(self, event_name: str, **fields) -> None:
        if not isinstance(event_name, str) or not _IDENTIFIER.fullmatch(event_name):
            raise ValueError("invalid progress event")
        safe = _validate_fields(fields)
        with self._lock:
            if not self._entered or self._closed:
                raise RuntimeError("progress context is not active")
            previous = self._fields
            updated = {**previous, **safe}
            role_changed = "role" in safe and safe["role"] != previous.get("role")
            total_changed = "total" in safe and safe["total"] != previous.get("total")
            completed = updated.get("completed", 0)
            completed_regressed = completed < previous.get("completed", 0)
            if role_changed and "completed" not in safe:
                updated["completed"] = 0
            elif total_changed and "completed" not in safe:
                total = updated.get("total")
                if total is not None and previous.get("completed", 0) > total:
                    updated["completed"] = 0
            if updated.get("total") is not None and "completed" not in updated:
                updated["completed"] = 0
            _validate_fields(updated)
            if role_changed or total_changed or completed_regressed:
                self._eta_started_at = self._clock()
                self._eta_completed_base = updated.get("completed", 0)
            self._fields = updated
            self._emit_locked(event_name)

    def _emit_locked(self, event_name: str) -> None:
        if self._logging_failed:
            return
        now = self._clock()
        assert self._started_at is not None
        elapsed = max(0.0, now - self._started_at)
        if event_name != "heartbeat":
            self._phase = event_name
            self._phase_started_at = now
        record = {
            "stage": self.stage,
            "event": event_name,
            "phase": self._phase,
            **self._fields,
            "elapsed_seconds": round(elapsed, 3),
            "phase_elapsed_seconds": round(max(0.0, now - self._phase_started_at), 3),
        }
        completed = self._fields.get("completed", 0)
        total = self._fields.get("total")
        eta_elapsed = max(0.0, now - self._eta_started_at) if self._eta_started_at is not None else 0.0
        eta_completed = completed - self._eta_completed_base
        if eta_elapsed > 0 and eta_completed > 0 and total is not None and completed < total:
            record["estimated_remaining_seconds"] = round(
                eta_elapsed * (total - completed) / eta_completed, 3,
            )
        try:
            self._logger(record)
        except Exception:
            self._logging_failed = True
            self._stop.set()

    def _heartbeat_loop(self) -> None:
        assert self._heartbeat_interval is not None
        while not self._stop.wait(self._heartbeat_interval):
            try:
                with self._lock:
                    if self._closed:
                        return
                    self._emit_locked("heartbeat")
            except Exception:
                self._stop.set()
                return

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            self._closed = True
            try:
                if exc_type is None:
                    total = self._fields.get("total")
                    if total is not None:
                        self._fields["completed"] = total
                    self._emit_locked("stage_complete")
                else:
                    self._emit_locked("stage_failure")
            except Exception:
                if exc_type is None:
                    raise
        return False
