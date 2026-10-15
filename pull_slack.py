"""Slack activity -> (practitioner, timestamp, source, action).  DISABLED unless SLACK_ENABLED=true.

Two sources:
  1. Enterprise Grid Audit Logs  (GET https://api.slack.com/audit/v1/logs, scope auditlogs:read, SLACK_AUDIT_TOKEN)
  2. conversations.history / conversations.replies for channels listed in settings.yaml (SLACK_BOT_TOKEN)

Privacy: message TEXT, names and emails are never stored. Only the Slack user id
(mapped to a pseudonymous practitioner id via config/people.yaml), the time, and an action label.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, time as dtime, timezone

import pandas as pd
import requests

from .common import (env, env_flag, get_conn, load_people, load_settings,
                     replace_rows)

AUDIT_URL = "https://api.slack.com/audit/v1/logs"
SLACK_API = "https://slack.com/api"
REDACT_KEYS = {"text", "email", "name", "real_name", "display_name", "blocks", "attachments", "files"}
COLUMNS = ["practitioner_id", "ts", "source", "action"]


def _epoch(d: date, end_of_day=False) -> int:
    t = dtime.max if end_of_day else dtime.min
    return int(datetime.combine(d, t).replace(tzinfo=timezone.utc).timestamp())


def redact(obj):
    """Recursively blank out fields that could hold personal content (for --peek)."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def normalize_audit_entries(entries: list[dict], user_map: dict) -> pd.DataFrame:
    rows = []
    for e in entries:
        uid = ((e.get("actor") or {}).get("user") or {}).get("id")
        if not uid or e.get("date_create") is None:
            continue
        rows.append({
            "practitioner_id": user_map.get(uid, f"slack:{uid}"),
            "ts": datetime.fromtimestamp(int(e["date_create"]), tz=timezone.utc).isoformat(),
            "source": "slack_audit", "action": e.get("action", "unknown"),
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def normalize_messages(messages: list[dict], user_map: dict) -> pd.DataFrame:
    rows = []
    for m in messages:
        uid = m.get("user")
        if not uid or not m.get("ts") or m.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
            continue
        rows.append({
            "practitioner_id": user_map.get(uid, f"slack:{uid}"),
            "ts": datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).isoformat(),
            "source": "slack_message", "action": "reply" if m.get("thread_ts") and m["thread_ts"] != m["ts"] else "message",
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def _get(session, url, params, headers, retries=3):
    for attempt in range(retries + 1):
        r = session.get(url, params=params, headers=headers, timeout=60)
        if r.status_code == 429 and attempt < retries:
            time.sleep(int(r.headers.get("Retry-After", 2 ** (attempt + 1))))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Slack returned HTTP {r.status_code} for {url}")
        return r.json()


def fetch_audit(session, token, start: date, end: date, actions: list[str]) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    out, cursor = [], None
    while True:
        params = {"oldest": _epoch(start), "latest": _epoch(end, True), "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        if len(actions) == 1:
            params["action"] = actions[0]
        data = _get(session, AUDIT_URL, params, headers)
        out += data.get("entries", [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if len(actions) > 1:
        out = [e for e in out if e.get("action") in actions]
    return out


def fetch_channel(session, token, channel: str, start: date, end: date) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    msgs, cursor = [], None
    while True:
        params = {"channel": channel, "oldest": _epoch(start), "latest": _epoch(end, True), "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get(session, f"{SLACK_API}/conversations.history", params, headers)
        if not data.get("ok"):
            raise RuntimeError(f"Slack error on conversations.history: {data.get('error')}")
        for m in data.get("messages", []):
            msgs.append(m)
            if m.get("reply_count"):
                rcursor = None
                while True:
                    rp = {"channel": channel, "ts": m["ts"], "limit": 200}
                    if rcursor:
                        rp["cursor"] = rcursor
                    rd = _get(session, f"{SLACK_API}/conversations.replies", rp, headers)
                    msgs += [x for x in rd.get("messages", []) if x.get("ts") != m["ts"]]
                    rcursor = (rd.get("response_metadata") or {}).get("next_cursor")
                    if not rcursor:
                        break
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return msgs


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull Slack activity (only if SLACK_ENABLED=true).")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--peek", action="store_true",
                    help="print a few raw entries on screen instead of saving (message text, names, emails are redacted)")
    ap.add_argument("--peek-count", type=int, default=5)
    a = ap.parse_args(argv)
    if not env_flag("SLACK_ENABLED"):
        raise SystemExit("Slack is disabled. Set SLACK_ENABLED=true in .env to use it.")
    settings = load_settings()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    user_map = load_people().get("slack_user_to_practitioner", {}) or {}
    session = requests.Session()
    parts = []
    audit_token, bot_token = env("SLACK_AUDIT_TOKEN"), env("SLACK_BOT_TOKEN")
    if audit_token:
        entries = fetch_audit(session, audit_token, start, end, settings["slack"].get("audit_actions", []))
        if a.peek:
            print(f"--- {len(entries)} audit entries; first {a.peek_count} (redacted) ---")
            for e in entries[:a.peek_count]:
                print(json.dumps(redact(e), indent=1)[:1500])
        parts.append(normalize_audit_entries(entries, user_map))
    if bot_token:
        for ch in settings["slack"].get("channels", []):
            msgs = fetch_channel(session, bot_token, ch, start, end)
            if a.peek:
                print(f"--- channel {ch}: {len(msgs)} messages; first {a.peek_count} (redacted) ---")
                for m in msgs[:a.peek_count]:
                    print(json.dumps(redact(m))[:600])
            parts.append(normalize_messages(msgs, user_map))
    if not parts:
        raise SystemExit("No Slack tokens configured (SLACK_AUDIT_TOKEN / SLACK_BOT_TOKEN).")
    if a.peek:
        return
    df = pd.concat(parts, ignore_index=True).astype("string")
    key_val = f"{start}_{end}"
    df["_pull_day"] = key_val
    replace_rows(get_conn(settings=settings), "raw_slack_events", df, "_pull_day", key_val)
    print(f"Stored {len(df)} Slack events.")


if __name__ == "__main__":
    main()
