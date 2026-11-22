#!/usr/bin/env python3
"""Publish the athdip paper book to a Slack canvas.

Runs after each paper-book update (19:00 CT). Uses the Slack bot token the
first-alert triage already loads from ``~/.config/first-alert-triage/env``. The
token is read inside this process and never printed.

First run creates a canvas and saves its id to ``output/paper_book_canvas.json``.
Later runs replace the body. A failure is logged and never changes the book.

  python3 scripts/paper_book_canvas.py --print    # show the markdown only
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_book as pb  # noqa: E402

OUTPUT = ROOT / "output"
CANVAS_PATH = OUTPUT / "paper_book_canvas.json"
ENV_PATH = Path.home() / ".config" / "first-alert-triage" / "env"
SLACK_API = "https://slack.com/api"
CHICAGO = ZoneInfo("America/Chicago")
TITLE = "Athdip paper book (replay from 2026-11-02)"
BOOK_LABELS = {
    "hold10": "Hold 10",
    "rsi50": "RSI 50",
    "hold10_low20": "Hold 10 + 20-day low",
    "rsi50_low20": "RSI 50 + 20-day low",
}
BOOK_SUMMARY_LABELS = {
    "hold10": "Hold 10 sessions",
    "rsi50": "RSI(14) above 50",
    "hold10_low20": "Hold 10, 20-day low only",
    "rsi50_low20": "RSI(14) above 50, 20-day low only",
}


def _pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in rows)
    return "\n".join(lines)


def last_prices(trades_path: Path = pb.TRADES_PATH) -> dict[tuple[str, str], float]:
    """(book, ticker) -> last close for open lots, from the ledger the book writes."""
    if not trades_path.is_file():
        return {}
    out: dict[tuple[str, str], float] = {}
    with trades_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") == "open" and row.get("last_px"):
                out[(row["book"], row["ticker"])] = float(row["last_px"])
    return out


def build_markdown(state: dict, marks: dict[tuple[str, str], float], session: str) -> str:
    slots = int(state.get("max_positions", pb.MAX_POSITIONS))
    now = datetime.now(CHICAGO).strftime("%Y-%m-%d %H:%M CT")

    summary = []
    for name in pb.BOOKS:
        label = BOOK_SUMMARY_LABELS[name]
        book = state["books"].get(name, {"open": [], "closed": []})
        closed = [t["net_ret"] for t in book["closed"]]
        wins = sum(1 for v in closed if v > 0)
        avg_closed = sum(closed) / len(closed) * 100 if closed else 0.0
        open_marks = [
            pb.net_return(p["entry_px"], marks[(name, p["ticker"])])
            for p in book["open"]
            if (name, p["ticker"]) in marks
        ]
        avg_mark = sum(open_marks) / len(open_marks) * 100 if open_marks else 0.0
        summary.append(
            [
                label,
                str(len(closed)),
                f"{wins} / {len(closed)}" if closed else "0 / 0",
                f"{avg_closed:+.2f}%",
                str(len(book["open"])),
                f"{avg_mark:+.2f}%",
            ]
        )

    parts = [
        "# Status",
        f"Through the {session} close. Updated {now}. Starts from a Yahoo replay; the "
        "19:00 CT screen keeps it current. Not live fills. "
        f"{slots} slots, 10 bps per side, no orders.",
        "",
        "# Summary",
        _table(
            ["Book", "Closed", "Win", "Avg net (closed)", "Open", "Avg mark (open, net)"],
            summary,
        ),
        "",
        "# Rules",
        "- **Entry:** athdip name whose 4h RSI low printed that session, at its daily close.",
        "- **Hold 10:** sell the close 10 sessions later (20-session stop).",
        "- **RSI 50:** sell the first later close with daily RSI(14) > 50 (20-session stop).",
        "- **20-day low books (parallel):** same exits, but only when the trigger-day close is "
        "the lowest close of the last 20 sessions. Other triggers are skipped. This is an "
        "in-sample finding from the entry-timing test, so it is a forward test.",
        "",
    ]

    for name in pb.BOOKS:
        book = state["books"].get(name, {"open": [], "closed": []})
        label = BOOK_LABELS[name]
        parts.append(f"# {label}: open lots")
        if book["open"]:
            rows = []
            for p in book["open"]:
                last = marks.get((name, p["ticker"]))
                rows.append(
                    [
                        p["ticker"],
                        p["entry_date"],
                        f"{p['entry_px']:.2f}",
                        f"{last:.2f}" if last else "n/a",
                        _pct(pb.net_return(p["entry_px"], last)) if last else "n/a",
                    ]
                )
            parts.append(_table(["Ticker", "Entry", "Entry px", "Last px", "Net mark"], rows))
        else:
            parts.append("None.")
        parts.append("")
        parts.append(f"# {label}: closed")
        if book["closed"]:
            rows = [
                [t["ticker"], t["entry_date"], t["exit_date"], _pct(t["net_ret"])]
                for t in reversed(book["closed"])
            ]
            parts.append(_table(["Ticker", "Entry", "Exit", "Net"], rows))
        else:
            parts.append("None yet.")
        parts.append("")

    parts += [
        "# Caveats",
        "- The replay used Yahoo bars; later sessions use Schwab, so a trigger can shift a day.",
        "- One selloff window with VIX in the mid-30s. Energy names are heavily correlated, so the open book is effectively one bet.",
        "- Full slots skip triggers; skipped names are not in the book.",
        "- Sample too small to choose between the two exits.",
        "- Small sample, one volatile stretch. The 20-day low books were picked from the same data, so they are optimistic.",
        "- Context only, not a buy or sell instruction.",
    ]
    return "\n".join(parts) + "\n"


def _token() -> str | None:
    """Slack bot token from the env file. Never printed."""
    value = os.environ.get("SLACK_BOT_TOKEN")
    if value:
        return value
    if not ENV_PATH.is_file():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line.startswith("SLACK_BOT_TOKEN="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _call(method: str, token: str, payload: dict) -> dict:
    import requests

    try:
        resp = requests.post(
            f"{SLACK_API}/{method}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps(payload),
            timeout=30,
        )
    except requests.RequestException as exc:  # network blip: report, do not raise
        return {"ok": False, "error": f"request_failed:{exc.__class__.__name__}"}
    try:
        return resp.json()
    except ValueError:
        return {"ok": False, "error": f"http_{resp.status_code}"}


def publish(markdown: str, *, path: Path = CANVAS_PATH) -> tuple[bool, str]:
    token = _token()
    if not token:
        return False, "no Slack bot token in the triage env file"

    saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    canvas_id = saved.get("canvas_id")
    content = {"type": "markdown", "markdown": markdown}
    if canvas_id:
        data = _call(
            "canvases.edit",
            token,
            {
                "canvas_id": canvas_id,
                "changes": [{"operation": "replace", "document_content": content}],
            },
        )
        if data.get("ok"):
            return True, f"canvas updated: {saved.get('url') or canvas_id}"
        return False, f"canvases.edit failed: {data.get('error')}"

    data = _call("canvases.create", token, {"title": TITLE, "document_content": content})
    if not data.get("ok"):
        return False, f"canvases.create failed: {data.get('error')}"
    new_id = data.get("canvas_id")
    url = data.get("canvas_url") or ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"canvas_id": new_id, "url": url}, indent=2) + "\n", encoding="utf-8")
    return True, f"canvas created: {url or new_id}"


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    state = pb.load_state()
    session = state.get("last_entry_session") or "n/a"
    markdown = build_markdown(state, last_prices(), session)
    if "--print" in args:
        print(markdown)
        return 0
    ok, msg = publish(markdown)
    print(f"Paper book canvas: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
