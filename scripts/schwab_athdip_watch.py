#!/usr/bin/env python3
"""Live athdip watcher via Schwab RTH 4h RSI (+ optional Yahoo compare).

Polls a watchlist (``screen_athdip.csv`` by default, or focus / --tickers),
computes Schwab 30m→RTH 4h RSI, and alerts when RSI **crosses** ≤ threshold
(default 31). First sighting of a symbol only seeds state (no alert) — same
idea as thinkScript ``athdipOk and !athdipOk[1]``. Yahoo compare is opt-in
(``--compare``); default is Schwab-only.

Examples::

  python3 scripts/schwab_athdip_watch.py --once --source screen --no-compare
  python3 scripts/schwab_athdip_watch.py --interval 300 --no-compare
  python3 scripts/schwab_athdip_watch.py --tickers AMP,BAC,AEE --once
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import yfinance as yf  # noqa: E402

from stock_screener.data.schwab import (  # noqa: E402
    SchwabNotConfiguredError,
    get_client,
    price_history_rth_4h,
    quote_last,
)
from stock_screener.features import compute_rsi_series  # noqa: E402

OUTPUT = ROOT / "output"
STATE_PATH = OUTPUT / "athdip_schwab_watch_state.json"
DEFAULT_RSI_MAX = 31.0


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _tickers_from_focus() -> list[str]:
    path = OUTPUT / "today_focus.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        return []
    out: list[str] = []
    for _, row in df.iterrows():
        t = str(row.get("ticker") or "").strip().upper()
        if not t or t == "NAN":
            continue
        tags = str(row.get("tags") or "").lower()
        kind = str(row.get("kind") or "").lower()
        if "athdip" in tags or kind == "athdip":
            out.append(t)
    return list(dict.fromkeys(out))


def _tickers_from_screen() -> list[str]:
    path = OUTPUT / "screen_athdip.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        return []
    return [
        t
        for t in df["ticker"].astype(str).str.strip().str.upper().tolist()
        if t and t != "NAN"
    ]


def resolve_watchlist(tickers_arg: str, *, source: str = "screen") -> list[str]:
    """Build the poll list.

    The watcher does **not** re-score the market. It only polls symbols already
    flagged by the last screener/dashboard run (or an explicit --tickers list).

    source:
      focus  — Today's focus rows tagged athdip only (narrow)
      screen — all ``screen_athdip.csv`` names (default; includes BAC etc.)
      all    — union of focus-athdip + screen_athdip
    """
    if tickers_arg.strip():
        return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()]
    focus = _tickers_from_focus()
    screen = _tickers_from_screen()
    src = (source or "screen").strip().lower()
    if src == "focus":
        return focus or screen
    if src == "all":
        return list(dict.fromkeys([*screen, *focus]))
    # default: full athdip screen (not just focus)
    return screen or focus


def yahoo_rth_4h_rsi(symbol: str, *, period: str = "60d") -> float | None:
    try:
        raw = yf.download(
            symbol,
            period=period,
            interval="4h",
            auto_adjust=True,
            progress=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
    else:
        close = raw["Close"]
    close = close.dropna()
    if len(close) < 16:
        return None
    rsi = compute_rsi_series(close, period=14).dropna()
    if rsi.empty:
        return None
    return float(rsi.iloc[-1])


def schwab_rth_4h_rsi(symbol: str, client) -> tuple[float | None, float | None]:
    """Return (last_rsi, last_close) from Schwab RTH 4h."""
    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(days=25)
    try:
        bars = price_history_rth_4h(symbol, client=client, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"  {symbol}: Schwab history error: {exc}", file=sys.stderr)
        return None, None
    if bars.empty or len(bars) < 16:
        return None, None
    rsi = compute_rsi_series(bars["Close"], period=14).dropna()
    if rsi.empty:
        return None, None
    return float(rsi.iloc[-1]), float(bars["Close"].iloc[-1])


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"tickers": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"tickers": {}}


def save_state(state: dict) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify_desktop(title: str, body: str) -> None:
    if platform.system() != "Darwin":
        return
    short = body.replace("\n", " · ")[:180]
    script = f'display notification {json.dumps(short)} with title {json.dumps(title)}'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def notify_slack(text: str) -> bool:
    url = (
        os.getenv("SCREENER_SLACK_WEBHOOK") or os.getenv("SLACK_WEBHOOK_URL") or ""
    ).strip()
    if not url:
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=15)
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        print(f"Slack webhook failed: {exc}", file=sys.stderr)
        return False


def poll_once(
    symbols: list[str],
    *,
    rsi_max: float,
    compare: bool,
    slack: bool,
    desktop: bool,
    dry_run: bool,
) -> int:
    client = get_client(interactive=False)
    state = load_state()
    prev = state.setdefault("tickers", {})
    # First poll (or first sighting of a symbol): seed baseline only — do not
    # treat "already ≤ rsi_max" as a fresh CROSS↓. Matches alert_new_signals
    # snapshot seeding and thinkScript bubble logic (athdipOk and !athdipOk[1]).
    crossings: list[str] = []
    seeded: list[str] = []
    lines = [
        f"Athdip Schwab watch · {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        f" · RSI≤{rsi_max:.0f}"
    ]

    for sym in symbols:
        s_rsi, s_px = schwab_rth_4h_rsi(sym, client)
        y_rsi = yahoo_rth_4h_rsi(sym) if compare else None
        last = quote_last(sym, client=client)
        px = last if last is not None else s_px

        had_prior = sym in prev and isinstance(prev.get(sym), dict) and "below" in (
            prev.get(sym) or {}
        )
        was_below = bool((prev.get(sym) or {}).get("below")) if had_prior else False
        now_below = s_rsi is not None and s_rsi <= rsi_max
        # Only alert on true transitions after a prior observation exists.
        crossed = had_prior and now_below and not was_below
        if not had_prior:
            seeded.append(sym)

        bit = f"  {sym}"
        if px is not None:
            bit += f" · Schwab ${px:.2f}"
        if s_rsi is not None:
            bit += f" · Schwab 4h RSI {s_rsi:.1f}"
        else:
            bit += " · Schwab 4h RSI n/a"
        if compare:
            if y_rsi is not None:
                delta = (s_rsi - y_rsi) if s_rsi is not None else None
                bit += f" · Yahoo 4h RSI {y_rsi:.1f}"
                if delta is not None:
                    bit += f" · Δ{delta:+.1f}"
            else:
                bit += " · Yahoo 4h RSI n/a"
        if now_below:
            bit += " · BELOW"
        if not had_prior:
            bit += " · SEED"
        elif crossed:
            bit += " · CROSS↓"
            crossings.append(sym)

        lines.append(bit)
        prev[sym] = {
            "schwab_rsi": s_rsi,
            "yahoo_rsi": y_rsi,
            "price": px,
            "below": now_below,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    msg = "\n".join(lines)
    if seeded and not crossings:
        msg += f"\n\nBaseline seeded ({len(seeded)} symbols); no CROSS alerts this run."
    if crossings:
        msg += "\n\nNew crosses ≤{:.0f}: {}".format(rsi_max, ", ".join(crossings))
    print(msg)

    if crossings and not dry_run:
        alert = (
            f"Athdip Schwab · RSI crossed ≤{rsi_max:.0f}\n"
            + "\n".join(
                ln for ln in lines if any(c in ln for c in crossings) or ln.startswith("Athdip")
            )
        )
        if desktop:
            notify_desktop("Athdip · Schwab RSI", ", ".join(crossings))
        if slack:
            ok = notify_slack(f"```{alert}```")
            print("Slack webhook: sent" if ok else "Slack webhook: skipped/failed")

    if not dry_run:
        save_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickers", default="", help="Comma list (else from last screen/focus)")
    p.add_argument(
        "--source",
        choices=("screen", "focus", "all"),
        default="screen",
        help="screen=all athdip CSV (default); focus=Today's focus athdip only; all=union",
    )
    p.add_argument("--rsi-max", type=float, default=DEFAULT_RSI_MAX)
    p.add_argument("--once", action="store_true", help="Single poll then exit")
    p.add_argument("--interval", type=float, default=300.0, help="Seconds between polls")
    p.add_argument(
        "--compare",
        action="store_true",
        help="Also fetch Yahoo 4h RSI for side-by-side Δ (off by default)",
    )
    p.add_argument("--no-compare", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-slack", action="store_true")
    p.add_argument("--no-desktop", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Do not write state or alert")
    args = p.parse_args(argv)

    _load_dotenv()
    symbols = resolve_watchlist(args.tickers, source=args.source)
    if not symbols:
        print(
            "No tickers. Run the daily screen first (writes screen_athdip.csv), "
            "or pass --tickers AMP,BAC",
            file=sys.stderr,
        )
        return 1

    print(f"Watching {len(symbols)} symbols ({args.source}): {', '.join(symbols)}")
    do_compare = bool(args.compare) and not bool(args.no_compare)
    try:
        if args.once:
            return poll_once(
                symbols,
                rsi_max=args.rsi_max,
                compare=do_compare,
                slack=not args.no_slack,
                desktop=not args.no_desktop,
                dry_run=args.dry_run,
            )
        while True:
            poll_once(
                symbols,
                rsi_max=args.rsi_max,
                compare=do_compare,
                slack=not args.no_slack,
                desktop=not args.no_desktop,
                dry_run=args.dry_run,
            )
            time.sleep(max(30.0, args.interval))
    except SchwabNotConfiguredError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
