"""Count-only progress hooks; business logic never depends on Kestra clients."""

from contextvars import ContextVar
from functools import wraps
from inspect import signature

from .cell_progress import Progress


_current = ContextVar("cell_progress", default=None)


def report(event, **fields):
    progress = _current.get()
    if progress is not None:
        progress.update(event, **fields)


def observed(stage):
    """Stream lifecycle/heartbeat messages without changing function contracts."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def run(*args, **kwargs):
            supplied = parameters.bind(*args, **kwargs).arguments
            context = {key: supplied[key] for key in (
                "cluster_id", "report_date", "start_date", "end_date",
            ) if key in supplied and supplied[key] is not None}
            with Progress(stage, **context) as progress:
                token = _current.set(progress)
                try:
                    return function(*args, **kwargs)
                finally:
                    _current.reset(token)

        return run
    return decorate


def cohort_counts(batch):
    """Summarize the checked requested cohort, never the cumulative Sheet history."""
    return {
        "cells": len(batch),
        "rows": sum(item["calculation"]["raw_row_count"] for item in batch),
        "unusual_rows": sum(len(item["calculation"]["unusual"]) for item in batch),
        "unusual_cells": sum(bool(item["calculation"]["unusual"]) for item in batch),
    }
