"""Terminal FinPay alerts, separate from financial writes and their retries."""

import json
import re

import requests


def _identifier(value):
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:, -]{1,200}", value):
        raise ValueError("invalid notification identifier")
    return value


def notification_messages(state, *, cluster_id, start_date, end_date,
                          execution_id, flow_id, write_gsheet, summary=None):
    """Format count-only alerts; SUCCESS is not inferred from task outputs."""
    state = _identifier(state)
    details = (
        f"Flow: {_identifier(flow_id)}\nExecution: {_identifier(execution_id)}\n"
        f"Cluster: {_identifier(cluster_id)}\n"
        f"Dates: {_identifier(start_date)} to {_identifier(end_date)}"
    )
    if state != "SUCCESS":
        return [f"FinPay cell workflow {state}.\n{details}\nInspect the execution logs before retrying."]
    if not isinstance(summary, dict):
        raise ValueError("successful notification requires a checked cohort summary")
    counts = {}
    for key in ("cells", "rows", "unusual_rows", "unusual_cells"):
        value = summary.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("invalid notification count")
        counts[key] = value
    messages = [
        f"FinPay cell workflow succeeded.\n{details}\n"
        f"Cells processed: {counts['cells']}\nSource rows: {counts['rows']}\n"
        + ("Database committed; four-tab Sheets publication verified." if write_gsheet else
           "Database committed; Sheets publication disabled for this run.")
    ]
    if counts["unusual_rows"]:
        messages.append(
            f"FinPay unusual transactions detected.\n{details}\n"
            f"Unusual rows: {counts['unusual_rows']}\n"
            f"Affected cell-days: {counts['unusual_cells']}\n"
            + ("Review the Unusual worksheet. " if write_gsheet else
               "Sheets publication disabled; review the checked cohort before publishing. ")
            + "Counts cover only the requested dates."
        )
    return messages


def send_notifications(messages, token, chat_id, *, post=None):
    """One attempt per message: an ambiguous timeout must not cause blind duplicates."""
    post = requests.post if post is None else post
    if not token or not chat_id:
        raise RuntimeError("Telegram notification credentials are missing")
    sent = 0
    failed = 0
    for text in messages:
        if not isinstance(text, str) or not 0 < len(text) <= 4096:
            raise ValueError("Telegram notification exceeds message limits")
        try:
            response = post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text}, timeout=(10, 30),
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            if (not isinstance(body, dict) or body.get("ok") is not True
                    or not isinstance(body.get("result"), dict)
                    or type(body["result"].get("message_id")) is not int):
                raise ValueError("Telegram rejected notification")
        except Exception:
            # Never propagate Requests errors: their text contains the token-bearing URL.
            failed += 1
        else:
            sent += 1
    if failed:
        raise RuntimeError(f"Telegram delivery unconfirmed for {failed} message(s); no automatic retry")
    return {"sent": sent}


def notify_from_environment(environment):
    summary = json.loads(environment.get("SUMMARY_JSON", "{}"))
    messages = notification_messages(
        environment["EXECUTION_STATE"], cluster_id=environment["CLUSTER_ID"],
        start_date=environment["START_DATE"], end_date=environment["END_DATE"],
        execution_id=environment["EXECUTION_ID"], flow_id=environment["FLOW_ID"],
        write_gsheet=environment["WRITE_GSHEET"].lower() == "true", summary=summary,
    )
    if not messages:
        return {"sent": 0}
    return send_notifications(
        messages, environment.get("TELEGRAM_BOT_TOKEN"), environment.get("TELEGRAM_CHAT_ID"),
    )
