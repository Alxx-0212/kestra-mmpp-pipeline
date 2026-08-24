from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

BASE_URL = "https://digipos-cms.finpay.id"


@dataclass(frozen=True)
class BucketTopupSnapshot:
    username: str
    cluster_id: str
    value: float
    text: str
    snapshot_at: datetime
    url: str


def cluster_id(username: str) -> str:
    return username[:-2] if username.endswith("_A") else username


def _csrf_token(markup: str) -> str | None:
    match = re.search(r'name="_token"[^>]*value="([^"]+)"', markup)
    return match.group(1) if match else None


def _plain_text(markup: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", markup, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_bucket_topup(markup: str) -> tuple[float, str]:
    text = _plain_text(markup)
    pattern = re.compile(
        r"BUCKET\s+TOP\s+UP\s*(?:Rp\.?|IDR)?\s*([-+]?\d[\d.,]*)",
        flags=re.I,
    )
    match = pattern.search(text)
    if not match:
        raise ValueError("BUCKET TOP UP value not found on DigiPOS home page")
    raw_value = match.group(1)
    if "." in raw_value and "," in raw_value:
        decimal_sep = "," if raw_value.rfind(",") > raw_value.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        normalized = raw_value.replace(thousands_sep, "").replace(decimal_sep, ".")
    else:
        normalized = raw_value.replace(".", "").replace(",", "")
    return float(normalized), match.group(0).strip()


def fetch_bucket_topup(username: str, password: str, base_url: str = BASE_URL) -> BucketTopupSnapshot:
    if not password:
        raise ValueError("DigiPOS password is required")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"})
    login_page = session.get(f"{base_url}/login", timeout=30)
    login_page.raise_for_status()
    token = _csrf_token(login_page.text)
    if not token:
        raise RuntimeError("No CSRF _token on DigiPOS login page")

    login = session.post(
        f"{base_url}/login-post",
        data={"_token": token, "username": username, "password": password},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": token,
            "Referer": f"{base_url}/login",
        },
        timeout=30,
    )
    login.raise_for_status()
    try:
        payload = login.json()
    except ValueError as exc:
        raise RuntimeError("DigiPOS login did not return JSON") from exc
    if payload.get("statusCode") not in ("00", "000"):
        raise RuntimeError(f"DigiPOS login failed: {payload.get('statusDesc')}")

    url = f"{base_url}/home"
    home = session.get(url, timeout=30)
    home.raise_for_status()
    value, displayed = parse_bucket_topup(home.text)
    return BucketTopupSnapshot(
        username=username,
        cluster_id=cluster_id(username),
        value=value,
        text=displayed,
        snapshot_at=datetime.now(timezone.utc),
        url=url,
    )
