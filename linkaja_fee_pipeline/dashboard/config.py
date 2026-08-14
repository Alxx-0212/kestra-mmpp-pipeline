"""Validated runtime configuration for the LinkAja Taipy dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass

from linkaja_fee_pipeline.database import linkaja_postgres_dsn_from_env


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean_setting(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class DashboardConfig:
    """Environment-backed configuration with no secret-bearing repr output."""

    dsn: str
    host: str = "0.0.0.0"
    port: int = 5000
    max_date_span_days: int = 93
    default_date_span_days: int = 31
    query_timeout_seconds: int = 30
    debug: bool = False

    def __repr__(self) -> str:
        return (
            "DashboardConfig(dsn='<redacted>', "
            f"host={self.host!r}, port={self.port!r}, "
            f"max_date_span_days={self.max_date_span_days!r}, "
            f"default_date_span_days={self.default_date_span_days!r}, "
            f"query_timeout_seconds={self.query_timeout_seconds!r}, "
            f"debug={self.debug!r})"
        )

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        dsn = os.environ.get("LINKAJA_DASHBOARD_DB_DSN", "").strip()
        if not dsn:
            dsn = linkaja_postgres_dsn_from_env()
        max_span = _integer_setting(
            "LINKAJA_DASHBOARD_MAX_DATE_SPAN_DAYS",
            93,
            1,
            366,
        )
        default_span = _integer_setting(
            "LINKAJA_DASHBOARD_DEFAULT_DATE_SPAN_DAYS",
            31,
            1,
            max_span,
        )
        return cls(
            dsn=dsn,
            host=os.environ.get(
                "LINKAJA_DASHBOARD_HOST",
                "0.0.0.0",
            ).strip() or "0.0.0.0",
            port=_integer_setting(
                "LINKAJA_DASHBOARD_PORT",
                5000,
                1,
                65535,
            ),
            max_date_span_days=max_span,
            default_date_span_days=default_span,
            query_timeout_seconds=_integer_setting(
                "LINKAJA_DASHBOARD_QUERY_TIMEOUT_SECONDS",
                30,
                1,
                300,
            ),
            debug=_boolean_setting("LINKAJA_DASHBOARD_DEBUG"),
        )
