#!/usr/bin/env python3
"""Live athdip watcher via Schwab RTH 4h RSI (+ optional Yahoo compare).

Polls fresh-ATH setups (``athdip_setups.csv`` by default), computes Schwab
RTH 4h RSI, and alerts when the minimum RSI over the last 5 calendar days
crosses ≤ threshold (default 31). That is the same trigger as the screener.
A name already below the threshold alerts once, then stays quiet until the
minimum rises back above it. Yahoo compare is opt-in (``--compare``).

Examples::

  python3 scripts/schwab_athdip_watch.py --once --source setups --no-compare
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

from stock_screener.data.prices import download_intraday, series_for  # noqa: E402
from stock_screener.data.schwab import (  # noqa: E402
    SchwabNotConfiguredError,
    get_client,
    quote_last,
)
from stock_screener.features import compute_rsi_series, min_rsi_lookback_detail  # noqa: E402

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


def _tickers_from_setups() -> list[str]:
    path = OUTPUT / "athdip_setups.csv"
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
      setups — fresh multi-year ATH names, before the RSI trigger (default)
      screen — names that already passed athdip
      focus  — Today's focus rows tagged athdip
      all    — union of setups, screen, and focus
    """
    if tickers_arg.strip():
        return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()]
    focus = _tickers_from_focus()
    screen = _tickers_from_screen()
    setups = _tickers_from_setups()
    src = (source or "setups").strip().lower()
    if src == "focus":
        return focus or screen
    if src == "screen":
        return screen or focus
    if src == "all":
        return list(dict.fromkeys([*setups, *screen, *focus]))
    return setups or screen or focus


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


def schwab_lookback_rsi(
    symbols: list[str], *, lookback_days: int = 5
) -> dict[str, tuple[float | None, float | None]]:
    """Map symbol → (min 4h RSI over the lookback, last 4h RSI).

    Uses the screener's Schwab 30m→RTH 4h bars and the same 5-calendar-day
    minimum. The minimum is the trigger; the last print is only for the log.
    """
    out: dict[str, tuple[float | None, float | None]] = {
        sym: (None, None) for sym in symbols
    }
    if not symbols:
        return out
    try:
        bars = download_intraday(
            symbols, interval="4h", period="60d", source="schwab"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Schwab 4h download failed: {exc}", file=sys.stderr)
        return out
    for sym in symbols:
        close = series_for(bars, "Close", sym).dropna()
        if len(close) < 16:
            continue
        rsi = compute_rsi_series(close, period=14).dropna()
        last = float(rsi.iloc[-1]) if not rsi.empty else None
        detail = min_rsi_lookback_detail(
            close, lookback_days=lookback_days, period=14
        )
        out[sym] = (detail[0] if detail else None, last)
    return out


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
    # Alert when the 5-day minimum crosses ≤ threshold, including the first
    # time a setup is seen already below. A repeat poll stays quiet.
    crossings: list[str] = []
    seeded: list[str] = []
    lines = [
        f"Athdip Schwab watch · {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        f" · 5d min RSI≤{rsi_max:.0f}"
    ]
    rsi_map = schwab_lookback_rsi(symbols)

    for sym in symbols:
        rsi_min, rsi_last = rsi_map.get(sym, (None, None))
        y_rsi = yahoo_rth_4h_rsi(sym) if compare else None
        last = quote_last(sym, client=client)
        px = last

        had_prior = sym in prev and isinstance(prev.get(sym), dict) and "below" in (
            prev.get(sym) or {}
        )
        was_below = bool((prev.get(sym) or {}).get("below")) if had_prior else False
        now_below = rsi_min is not None and rsi_min <= rsi_max
        crossed = now_below and not was_below
        if not had_prior and not now_below:
            seeded.append(sym)

        bit = f"  {sym}"
        if px is not None:
            bit += f" · Schwab ${px:.2f}"
        if rsi_min is not None:
            bit += f" · 5d min RSI {rsi_min:.1f}"
        else:
            bit += " · 5d min RSI n/a"
        if rsi_last is not None:
            bit += f" · last {rsi_last:.1f}"
        if compare:
            if y_rsi is not None:
                delta = (rsi_min - y_rsi) if rsi_min is not None else None
                bit += f" · Yahoo 4h RSI {y_rsi:.1f}"
                if delta is not None:
                    bit += f" · Δ{delta:+.1f}"
            else:
                bit += " · Yahoo 4h RSI n/a"
        if now_below:
            bit += " · BELOW"
        if crossed:
            bit += " · CROSS↓"
            crossings.append(sym)
        elif not had_prior:
            bit += " · SEED"

        lines.append(bit)
        prev[sym] = {
            "schwab_rsi": rsi_min,
            "schwab_rsi_last": rsi_last,
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
        choices=("setups", "screen", "focus", "all"),
        default="setups",
        help="setups=fresh ATH names (default); screen=already triggered; focus=Today's focus; all=union",
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
