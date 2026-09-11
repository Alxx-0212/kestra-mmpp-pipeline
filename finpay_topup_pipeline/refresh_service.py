from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo


DEFAULT_USERS = ["411311_A", "421306_A", "421307_A", "421315_A", "421318_A", "421320_A"]
CLUSTER_USERS = {user[:-2]: user for user in DEFAULT_USERS}
REFRESH_MODES = frozenset({"FULL", "AUTO_CLUSTER", "AUTO_ALL"})
DEFAULT_TIMEZONE = "Asia/Makassar"


def default_window(today=None):
    today = today or local_today()
    return today - timedelta(days=1), today


def local_today(timezone_name=DEFAULT_TIMEZONE):
    return datetime.now(ZoneInfo(timezone_name)).date()


def normalize_window(start, end, today=None):
    """Return a refresh window that always ends at today's local date."""
    today = today or local_today()
    normalized_start = date.fromisoformat(start).isoformat() if start else (today - timedelta(days=1)).isoformat()
    normalized_end = today.isoformat()
    if date.fromisoformat(normalized_start) > today:
        raise ValueError("start date cannot be after today's date")
    return normalized_start, normalized_end


def _multipart_form(fields):
    boundary = "finpay-topup-boundary"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def kestra_execution_url(base_url, namespace, flow_id):
    base = base_url.rstrip("/")
    ns = urllib.parse.quote(namespace, safe="")
    fid = urllib.parse.quote(flow_id, safe="")
    return f"{base}/api/v1/main/executions/{ns}/{fid}"


def submit_kestra_refresh(
    *,
    base_url,
    namespace,
    flow_id,
    start,
    end,
    users,
    dry_run=False,
    requested_by="finance",
    trigger_source="refresh_service",
    verify_bucket_topup=True,
    refresh_mode="FULL",
    checkpoint_dates=None,
    api_token=None,
    basic_user=None,
    basic_password=None,
    timeout=30,
):
    users = [str(user).strip() for user in users]
    fields = {
        "start": start,
        "end": end,
        "users": " ".join(users),
        "dry_run": "true" if dry_run else "false",
        "requested_by": requested_by,
        "trigger_source": trigger_source,
        "verify_bucket_topup": "true" if verify_bucket_topup else "false",
    }
    if refresh_mode is not None:
        refresh_mode = str(refresh_mode).strip().upper()
        if refresh_mode not in REFRESH_MODES:
            raise ValueError(f"unsupported refresh mode: {refresh_mode!r}")
        if refresh_mode == "AUTO_CLUSTER" and (
            len(users) != 1 or users[0] not in CLUSTER_USERS.values()
        ):
            raise ValueError("AUTO_CLUSTER requires one configured DigiPOS user")
        if refresh_mode == "AUTO_ALL" and (
            len(users) != len(DEFAULT_USERS) or set(users) != set(DEFAULT_USERS)
        ):
            raise ValueError("AUTO_ALL requires all configured DigiPOS users")
        fields["refresh_mode"] = refresh_mode
        if checkpoint_dates is not None:
            fields["checkpoint_dates"] = json.dumps(
                {
                    str(cluster_id): (
                        value.isoformat()
                        if hasattr(value, "isoformat")
                        else str(value)
                    )
                    for cluster_id, value in checkpoint_dates.items()
                },
                sort_keys=True,
                separators=(",", ":"),
            )
    body, content_type = _multipart_form(fields)
    req = urllib.request.Request(
        kestra_execution_url(base_url, namespace, flow_id),
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    if api_token:
        req.add_header("Authorization", f"Bearer {api_token}")
    elif basic_user or basic_password:
        token = base64.b64encode(f"{basic_user or ''}:{basic_password or ''}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"status_code": resp.status, "body": raw, "json": _loads_json(raw)}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"status_code": exc.code, "body": raw, "json": _loads_json(raw)}
    except urllib.error.URLError as exc:
        return _unreachable_result(getattr(exc, "reason", None) or exc)
    except OSError as exc:
        # socket.timeout / connection resets raised outside URLError.
        return _unreachable_result(exc)


def _sanitized_reason(exc):
    """Bounded single-line detail; never contains request headers or credentials."""
    text = " ".join(str(exc).split())
    text = re.sub(r"(?i)(basic\s+|bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)(authorization|password|passwd|token|secret|credential)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/\s]*:[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+:[^\s,;]+", "[redacted]", text)
    return text[:200] or "unknown transport error"


def _unreachable_result(reason):
    return {
        "status_code": 0,
        "body": f"Kestra unreachable: {_sanitized_reason(reason)}",
        "json": None,
    }


def _loads_json(raw):
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def execution_id_from_response(result):
    payload = result.get("json") if isinstance(result, dict) else None
    if isinstance(payload, dict):
        for key in ("id", "executionId", "execution_id"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def kestra_execution_web_url(base_url, namespace, flow_id, execution_id):
    base = base_url.rstrip("/")
    if not execution_id:
        return ""
    return (
        f"{base}/ui/executions/{urllib.parse.quote(namespace, safe='')}/"
        f"{urllib.parse.quote(flow_id, safe='')}/"
        f"{urllib.parse.quote(execution_id, safe='')}"
    )


def _html_page(title, body, *, refresh_seconds=None):
    refresh = (
        f"<meta http-equiv='refresh' content='{int(refresh_seconds)}'>"
        if refresh_seconds
        else ""
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{refresh}"
        f"<title>{title}</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;margin:32px;max-width:760px;color:#1f2933}"
        "label{display:block;margin:14px 0 6px;font-weight:600}"
        "input,button{font:inherit;padding:8px 10px}"
        "input[type=date],input[type=text]{width:260px}"
        "button{margin-top:18px;cursor:pointer}"
        ".muted{color:#52606d}.ok{color:#0f7b0f}.bad{color:#a61b1b}.warn{color:#9a5b00}"
        ".card{border:1px solid #d9e2ec;border-radius:10px;padding:16px;margin:16px 0;background:#fff}"
        ".primary{display:inline-block;background:#1f5eff;color:#fff;text-decoration:none;padding:9px 12px;border-radius:7px;margin-right:8px}"
        ".secondary{display:inline-block;color:#1f5eff;text-decoration:none;padding:9px 0}"
        "pre{white-space:pre-wrap;background:#f5f7fa;padding:12px;border-radius:6px}"
        "</style></head><body>"
        f"{body}</body></html>"
    ).encode("utf-8")


class RefreshHandler(BaseHTTPRequestHandler):
    server_version = "FinPayTopupRefresh/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_text("ok\n")
            return
        if not self._authorized():
            self._auth_required()
            return
        if parsed.path not in ("/", "/refresh"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        start, end = default_window()
        users = " ".join(DEFAULT_USERS)
        body = (
            "<h1>FinPay Top Up refresh</h1>"
            "<p class='muted'>Triggers Kestra for yesterday through today by default. "
            "All six clusters are included. The dashboard stays readable while the refresh runs.</p>"
            "<div class='card'>"
            "<strong>Chosen trigger surface:</strong> this protected internal page. "
            "It keeps Kestra credentials server-side and avoids Telegram as a required finance workflow."
            "</div>"
            "<form method='post' action='/refresh'>"
            f"<label>Start date</label><input type='date' name='start' value='{start.isoformat()}' required>"
            f"<label>End date</label><input type='date' name='end' value='{end.isoformat()}' required>"
            f"<label>Users</label><input type='text' name='users' value='{users}' readonly>"
            "<label>Requested by</label><input type='text' name='requested_by' value='finance'>"
            "<label><input type='checkbox' name='dry_run' value='true'> Dry run</label>"
            "<button type='submit'>Refresh dashboard data</button>"
            "</form>"
        )
        self._send_html(_html_page("FinPay Top Up refresh", body))

    def do_POST(self):
        if not self._authorized():
            self._auth_required()
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/refresh":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length") or "0")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        start = _one(form, "start")
        end = _one(form, "end")
        users = DEFAULT_USERS
        requested_by = _one(form, "requested_by") or "finance"
        dry_run = _one(form, "dry_run") == "true"
        if not start:
            self.send_error(HTTPStatus.BAD_REQUEST, "start is required")
            return
        try:
            start, end = normalize_window(start, end)
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        result = submit_kestra_refresh(
            base_url=self.server.kestra_base_url,
            namespace=self.server.kestra_namespace,
            flow_id=self.server.kestra_flow_id,
            start=start,
            end=end,
            users=users,
            dry_run=dry_run,
            requested_by=requested_by,
            api_token=self.server.kestra_api_token,
            basic_user=self.server.kestra_basic_user,
            basic_password=self.server.kestra_basic_password,
            timeout=self.server.kestra_timeout,
        )
        css = "ok" if 200 <= int(result["status_code"]) < 300 else "bad"
        dashboard = self.server.superset_dashboard_url
        execution_id = execution_id_from_response(result)
        execution_link = kestra_execution_web_url(
            self.server.kestra_public_url or self.server.kestra_base_url,
            self.server.kestra_namespace,
            self.server.kestra_flow_id,
            execution_id,
        )
        dashboard_link = (
            f"<a class='primary' href='{dashboard}'>Open Superset dashboard</a>"
            if dashboard
            else ""
        )
        execution_anchor = (
            f"<a class='secondary' href='{execution_link}'>Open Kestra execution</a>"
            if execution_link
            else ""
        )
        guidance = (
            "<p class='muted'>Superset reads refresh status from PostgreSQL. "
            "If the dashboard still shows a working status, refresh the dashboard after the Kestra run reaches a terminal state.</p>"
            if 200 <= int(result["status_code"]) < 300
            else "<p class='bad'>Kestra rejected the refresh request. Check the response below.</p>"
        )
        body = (
            "<h1>Refresh submitted</h1>"
            f"<p class='{css}'>Kestra HTTP status: {result['status_code']}</p>"
            f"<div class='card'>{guidance}{dashboard_link}{execution_anchor}</div>"
            f"<pre>{_escape(result['body'][:4000])}</pre>"
        )
        self._send_html(_html_page("Refresh submitted", body, refresh_seconds=30 if 200 <= int(result["status_code"]) < 300 else None))

    def _send_text(self, text, status=HTTPStatus.OK):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, data, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        expected_user = self.server.basic_user
        expected_password = self.server.basic_password
        if not expected_user and not expected_password:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        username, _, password = decoded.partition(":")
        return username == expected_user and password == expected_password

    def _auth_required(self):
        body = b"authentication required\n"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="FinPay Top Up Refresh"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _one(form, key):
    values = form.get(key)
    return values[0] if values else ""


def _escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class RefreshServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class=RefreshHandler):
        super().__init__(server_address, handler_class)
        self.kestra_base_url = os.environ.get("KESTRA_BASE_URL", "http://localhost:8080")
        self.kestra_public_url = os.environ.get("KESTRA_PUBLIC_URL", "")
        self.kestra_namespace = os.environ.get("KESTRA_NAMESPACE", "finance.finpay")
        self.kestra_flow_id = os.environ.get("KESTRA_FLOW_ID", "finpay_topup_pipeline_v1")
        self.kestra_api_token = os.environ.get("KESTRA_API_TOKEN")
        self.kestra_basic_user = os.environ.get("KESTRA_BASIC_AUTH_USERNAME")
        self.kestra_basic_password = os.environ.get("KESTRA_BASIC_AUTH_PASSWORD")
        self.kestra_timeout = int(os.environ.get("KESTRA_TIMEOUT_SECONDS", "30"))
        self.superset_dashboard_url = os.environ.get("SUPERSET_DASHBOARD_URL", "")
        self.basic_user = os.environ.get("FINPAY_REFRESH_BASIC_USER")
        self.basic_password = os.environ.get("FINPAY_REFRESH_BASIC_PASSWORD")


def main():
    raise SystemExit("Standalone refresh service removed; use the Kestra or Telegram trigger.")


if __name__ == "__main__":
    main()
