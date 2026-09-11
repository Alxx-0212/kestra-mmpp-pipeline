from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8096
DEFAULT_REFRESH_COOLDOWN_SECONDS = 600


def _parse_non_negative_int(raw, env_name):
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{env_name} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{env_name} must be a non-negative integer, got {raw!r}")
    return value


def _parse_bool(raw, env_name):
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} must be a boolean, got {raw!r}")


def _parse_id_list(raw):
    """Parse a comma-separated integer whitelist; empty/unset yields nothing."""
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


def _read_secret_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    webhook_secret: str
    public_base_url: str = ""
    allowed_chat_ids: frozenset = field(default=frozenset())
    allowed_user_ids: frozenset = field(default=frozenset())
    db_kwargs: dict = field(default_factory=dict)
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    kestra_base_url: str = ""
    kestra_namespace: str = "finance.finpay"
    kestra_flow_id: str = "finpay_topup_pipeline_v1"
    kestra_public_url: str = ""
    kestra_basic_user: str = ""
    kestra_basic_password: str = ""
    refresh_cooldown_seconds: int = DEFAULT_REFRESH_COOLDOWN_SECONDS
    selective_refresh_enabled: bool = False

    @property
    def auth_configured(self):
        """Fail-closed auth requires BOTH whitelists to be non-empty."""
        return bool(self.allowed_chat_ids) and bool(self.allowed_user_ids)

    @property
    def kestra_configured(self):
        """All five Kestra submission settings must be present for /refresh."""
        return bool(
            self.kestra_base_url
            and self.kestra_namespace
            and self.kestra_flow_id
            and self.kestra_basic_user
            and self.kestra_basic_password
        )


def load_settings(env=os.environ):
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    secret = env.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not token and env.get("TELEGRAM_BOT_TOKEN_FILE"):
        token = _read_secret_file(env["TELEGRAM_BOT_TOKEN_FILE"])
    if not secret and env.get("TELEGRAM_WEBHOOK_SECRET_FILE"):
        secret = _read_secret_file(env["TELEGRAM_WEBHOOK_SECRET_FILE"])

    password = env.get("FINPAY_DB_PASSWORD", "")
    if not password and env.get("FINPAY_DB_PASSWORD_FILE"):
        password = _read_secret_file(env["FINPAY_DB_PASSWORD_FILE"])
    db_kwargs = {
        "host": env.get("FINPAY_DB_HOST", "localhost"),
        "port": int(env.get("FINPAY_DB_PORT", "5432")),
        "dbname": env.get("FINPAY_DB_NAME", "finpay"),
        "user": env.get("FINPAY_DB_USER", "finpay"),
        "password": password,
        "connect_timeout": int(env.get("FINPAY_DB_CONNECT_TIMEOUT", "10")),
    }

    return Settings(
        bot_token=token,
        webhook_secret=secret,
        public_base_url=env.get("TELEGRAM_BOT_PUBLIC_BASE_URL", "").rstrip("/"),
        allowed_chat_ids=_parse_id_list(env.get("TELEGRAM_ALLOWED_CHAT_IDS")),
        allowed_user_ids=_parse_id_list(env.get("TELEGRAM_ALLOWED_USER_IDS")),
        db_kwargs=db_kwargs,
        listen_host=env.get("TELEGRAM_BOT_HOST", DEFAULT_LISTEN_HOST),
        listen_port=int(env.get("TELEGRAM_BOT_PORT", str(DEFAULT_LISTEN_PORT))),
        kestra_base_url=env.get("KESTRA_BASE_URL", "").rstrip("/"),
        kestra_namespace=env.get("KESTRA_NAMESPACE", "finance.finpay"),
        kestra_flow_id=env.get("KESTRA_FLOW_ID", "finpay_topup_pipeline_v1"),
        kestra_public_url=env.get("KESTRA_PUBLIC_URL", "").rstrip("/"),
        kestra_basic_user=env.get("KESTRA_BASIC_AUTH_USERNAME", ""),
        kestra_basic_password=env.get("KESTRA_BASIC_AUTH_PASSWORD", ""),
        refresh_cooldown_seconds=_parse_non_negative_int(
            env.get("TELEGRAM_REFRESH_COOLDOWN_SECONDS", "600"),
            "TELEGRAM_REFRESH_COOLDOWN_SECONDS",
        ),
        selective_refresh_enabled=_parse_bool(
            env.get("TELEGRAM_SELECTIVE_REFRESH_ENABLED", "false"),
            "TELEGRAM_SELECTIVE_REFRESH_ENABLED",
        ),
    )
