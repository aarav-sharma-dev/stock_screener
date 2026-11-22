#!/usr/bin/env python3
"""DM a short read of new #vli-research-notes posts.

The VM cron sources ~/.config/first-alert-triage/env so SLACK_BOT_TOKEN and
CURSOR_API_KEY are set. The Slack bot must already be in
#vli-research-notes. A run with no new posts, or posts that do not change
the crowding read, does not send a DM.

State file: ~/.local/state/vli-research-notes/last_ts
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHANNEL_ID = os.environ.get("VLI_NOTES_CHANNEL_ID", "C0B0ELTG1GT")
DM_USER_ID = os.environ.get("FIRST_ALERT_DM_USER_ID", "U0BJ0T9PBEK")
AGENT_BIN = os.environ.get("AGENT_BIN", str(Path.home() / ".local" / "bin" / "agent"))
WORKSPACE = Path(
    os.environ.get("SCREENER_ROOT", str(Path.home() / "dev" / "stock_screener"))
)
STATE_DIR = Path.home() / ".local" / "state" / "vli-research-notes"
LAST_TS_PATH = STATE_DIR / "last_ts"
AGENT_TIMEOUT = int(os.environ.get("VLI_NOTES_AGENT_TIMEOUT", "180"))
MAX_CHARS = 12000


def log(msg: str) -> None:
    print(msg, flush=True)


def slack_token() -> str:
    token = (
        os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN") or ""
    ).strip()
    if not token:
        raise SystemExit("SLACK_BOT_TOKEN is not set")
    return token


def slack_api(method: str, token: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(
        {k: v for k, v in payload.items() if v is not None}
    ).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Slack HTTP error {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Slack network error: {exc}") from exc
    if not body.get("ok"):
        error = body.get("error", body)
        if error == "not_in_channel":
            raise SystemExit(
                "Slack bot is not in #vli-research-notes. Invite @skynet, then rerun."
            )
        raise SystemExit(f"Slack API {method} failed: {error}")
    return body


def load_last_ts() -> str:
    if not LAST_TS_PATH.exists():
        return "0"
    return LAST_TS_PATH.read_text(encoding="utf-8").strip() or "0"


def save_last_ts(ts: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_TS_PATH.write_text(ts + "\n", encoding="utf-8")


def fetch_new(token: str, oldest: str) -> list[dict]:
    messages: list[dict] = []
    cursor = None
    while True:
        payload = {"channel": CHANNEL_ID, "oldest": oldest, "limit": "100"}
        if cursor:
            payload["cursor"] = cursor
        body = slack_api("conversations.history", token, payload)
        for msg in body.get("messages") or []:
            subtype = msg.get("subtype") or ""
            if subtype in {"channel_join", "channel_leave", "bot_add", "bot_remove"}:
                continue
            ts = str(msg.get("ts") or "")
            if not ts or ts <= oldest:
                continue
            text = (msg.get("text") or "").strip()
            files = msg.get("files") or []
            if not text and not files:
                continue
            messages.append(msg)
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    messages.sort(key=lambda m: float(m["ts"]))
    return messages


def permalink(token: str, ts: str) -> str:
    try:
        body = slack_api(
            "chat.getPermalink",
            token,
            {"channel": CHANNEL_ID, "message_ts": ts},
        )
        return str(body.get("permalink") or "")
    except SystemExit:
        return ""


def transcript(token: str, messages: list[dict]) -> str:
    chunks: list[str] = []
    for msg in messages:
        ts = str(msg["ts"])
        text = (msg.get("text") or "").strip()
        if not text:
            n = len(msg.get("files") or [])
            text = f"[{n} chart or file attached, no caption]"
        if len(text) > 1500:
            text = text[:1500] + "…"
        link = permalink(token, ts)
        chunks.append(f"ts={ts}\n{text}\n{link}".strip())
    blob = "\n\n---\n\n".join(chunks)
    if len(blob) > MAX_CHARS:
        blob = blob[:MAX_CHARS] + "\n…"
    return blob


def run_agent(prompt: str) -> str:
    if not Path(AGENT_BIN).is_file():
        raise SystemExit(f"agent binary not found: {AGENT_BIN}")
    cmd = [
        AGENT_BIN,
        "-p",
        "--mode",
        "ask",
        "--trust",
        "--workspace",
        str(WORKSPACE),
        "--output-format",
        "text",
        prompt,
    ]
    log(f"running agent ({AGENT_BIN})")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=AGENT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("agent timed out; watermark not advanced")
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if "Authentication required" in err or "Authentication required" in out:
        raise SystemExit("agent auth failure; watermark not advanced")
    if proc.returncode != 0 and not out:
        raise SystemExit(f"agent failed rc={proc.returncode}; watermark not advanced")
    if err:
        log(f"agent stderr: {err[:300]}")
    return out


def post_dm(token: str, text: str) -> None:
    opened = slack_api("conversations.open", token, {"users": DM_USER_ID})
    channel = (opened.get("channel") or {}).get("id")
    if not channel:
        raise SystemExit("conversations.open returned no channel")
    slack_api(
        "chat.postMessage",
        token,
        {"channel": channel, "text": text[:3500]},
    )


PROMPT = """You review new posts from the Slack channel #vli-research-notes for a stock screener. Live signals are athdip dips plus multi-mode focus. Broker notes are context, not buy rules and not score weights.

Posts:
{posts}

Reply with exactly NONE if nothing here should change how today's signals are read.
Otherwise reply in under 120 words:
- what changed in positioning (prime book, software vs semis, hyperscalers, hardware, memory)
- which point could matter for a name that is already in a dip
Do not invent tickers that are not in the posts. No preamble.
"""


def main() -> int:
    dry = "--dry-run" in sys.argv
    token = slack_token()
    oldest = load_last_ts()
    messages = fetch_new(token, oldest)
    log(f"new posts: {len(messages)} oldest>{oldest} dry_run={dry}")
    if not messages:
        return 0
    newest = str(messages[-1]["ts"])
    body = transcript(token, messages)
    if dry:
        print(body)
        log(f"dry-run; watermark stays {oldest}")
        return 0
    review = run_agent(PROMPT.format(posts=body)).strip()
    if not review or review.upper().startswith("NONE"):
        save_last_ts(newest)
        log("nothing material; no DM")
        return 0
    post_dm(token, "Research notes\n" + review)
    save_last_ts(newest)
    log("DM sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
