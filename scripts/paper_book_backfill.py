#!/usr/bin/env python3
"""Replay the athdip paper book over past sessions.

Same entry and exit rules as ``paper_book.py``: a name enters at the daily
close of the session its 4h RSI low printed, and two books exit on hold-10 or
daily RSI(14) > 50, with a 20-session stop, 10 slots, 10 bps per side.

Signals are rebuilt point-in-time from Yahoo daily bars and Yahoo 4h bars with
the production athdip rule (the same code the backtests use). Schwab 4h
history only reaches back about 10 days, so it cannot do this. Expect small
differences from the live Schwab screen: adjusted closes and 4h bar edges.

  python3 scripts/paper_book_backfill.py --start 2026-11-02 --dry-run
  python3 scripts/paper_book_backfill.py --start 2026-11-02          # writes state

Writing replaces ``output/paper_book.json`` after saving the old file next to
it. The live 19:00 CT run then continues from the replayed book.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

import pandas as pd  # noqa: E402

import paper_book as pb  # noqa: E402

OUTPUT = ROOT / "output"


def build_universe(limit: int) -> list[str]:
    """Same names the 06:00 / 19:00 screen scans: S&P slice plus athdip extras."""
    from stock_screener.config import ScreenerConfig
    from stock_screener.data.universe import resolve_universe

    names = resolve_universe(limit=limit, universe="sp500")
    extras: list[str] = []
    try:
        from stock_screener.data.universe import ATHDIP_THEME_EXTRAS

        extras.extend(ATHDIP_THEME_EXTRAS)
    except ImportError:
        print("note: ATHDIP_THEME_EXTRAS not in this checkout; S&P slice only")
    extras.extend(getattr(ScreenerConfig(), "athdip_extra_tickers", []) or [])
    return list(dict.fromkeys([*names, *extras]))


def signals_by_day(sig: pd.DataFrame) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if sig.empty:
        return out
    for day, group in sig.groupby(sig["date"].dt.strftime("%Y-%m-%d")):
        rows = [
            {"ticker": str(r.ticker).upper(), "score": float(r.score)}
            for r in group.itertuples()
        ]
        rows.sort(key=lambda r: (-r["score"], r["ticker"]))
        out[day] = rows
    return out


def closes_by_ticker(prices: pd.DataFrame, tickers: list[str]) -> dict[str, dict[str, float]]:
    import backtest_ytd as bt

    out: dict[str, dict[str, float]] = {}
    for ticker in tickers:
        series = bt._col(prices, "Close", ticker).dropna()
        out[ticker] = {
            pd.Timestamp(ts).strftime("%Y-%m-%d"): float(px)
            for ts, px in series.items()
            if px > 0
        }
    return out


def replay(
    sessions: list[str],
    calendar: list[str],
    by_day: dict[str, list[dict]],
    bars: dict[str, dict[str, float]],
    rsi_by_ticker: dict[str, dict[str, float]],
) -> tuple[dict, list[str]]:
    state = pb.new_state()
    notes: list[str] = []
    for day in sessions:
        notes.extend(
            f"{day} {line}"
            for line in pb.advance(
                state,
                session=day,
                ranked=by_day.get(day, []),
                allow_entries=True,
                calendar=calendar,
                bars=bars,
                rsi_by_ticker=rsi_by_ticker,
            )
        )
    return state, notes


def summarize(state: dict, bars: dict[str, dict[str, float]], slots: int) -> list[str]:
    lines: list[str] = []
    for name in pb.BOOKS:
        book = state["books"][name]
        closed = [t["net_ret"] for t in book["closed"]]
        marks = []
        for pos in book["open"]:
            days = bars.get(pos["ticker"], {})
            if days:
                marks.append(pb.net_return(pos["entry_px"], days[max(days)]))
        wins = sum(1 for v in closed if v > 0)
        avg_closed = sum(closed) / len(closed) * 100 if closed else 0.0
        avg_open = sum(marks) / len(marks) * 100 if marks else 0.0
        # Each lot is one slot of starting equity; no compounding, no cash yield.
        on_capital = (sum(closed) + sum(marks)) / slots * 100
        lines.append(
            f"{name}: {len(closed)} closed (win {wins}/{len(closed)}, avg net {avg_closed:.2f}%), "
            f"{len(book['open'])} open (avg mark {avg_open:.2f}%), "
            f"≈{on_capital:.2f}% on starting equity"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-11-02", help="first session to enter")
    parser.add_argument("--end", default=None, help="last session (default: latest Yahoo close)")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true", help="print only; keep current state")
    args = parser.parse_args(argv)

    import backtest_ytd as bt
    from experiment_athdip_robustness import collect_base_signals
    from stock_screener.config import ScreenerConfig
    from stock_screener.data.prices import download_intraday

    start = pd.Timestamp(args.start)
    today = datetime.now(timezone.utc).date()
    universe = build_universe(args.limit)
    print(f"Universe {len(universe)} names · replay {args.start} → {args.end or 'latest'}")

    warmup = (start - pd.Timedelta(days=1825)).strftime("%Y-%m-%d")
    end_dl = (pd.Timestamp(today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    prices = bt.download_panel(universe, warmup, end_dl)
    vix = bt._col(prices, "Close", "^VIX")
    spy = bt._col(prices, "Close", "SPY").dropna()
    calendar = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in spy.index]
    last_day = args.end or calendar[-1]
    sessions = [d for d in calendar if args.start <= d <= last_day]
    if not sessions:
        print("No sessions in range.")
        return 1

    cfg = ScreenerConfig(
        athdip_require_uptrend=False,
        athdip_require_sma50=False,
        require_above_sma_slow=False,
    )
    print("Downloading 4h bars (Yahoo)…")
    bars_4h = download_intraday(universe, interval="4h", period="730d", chunk_size=20)
    print("Replaying athdip triggers day by day…")
    sig = collect_base_signals(prices, vix, universe, cfg, bars_4h, start)
    sig = sig[sig["date"].dt.strftime("%Y-%m-%d") <= last_day] if not sig.empty else sig
    by_day = signals_by_day(sig)

    traded = sorted({r["ticker"] for rows in by_day.values() for r in rows})
    bars = closes_by_ticker(prices, traded)
    rsi_by_ticker = {t: pb.rsi_table(days) for t, days in bars.items()}

    state, notes = replay(sessions, calendar, by_day, bars, rsi_by_ticker)
    state["backfill"] = {
        "start": args.start,
        "through": last_day,
        "source": "Yahoo daily and 4h bars, point-in-time replay",
    }

    print("\nTriggers per session (score order):")
    for day in sessions:
        names = ", ".join(r["ticker"] for r in by_day.get(day, [])) or "-"
        print(f"  {day}: {names}")
    print("\nLog:")
    for line in notes:
        print("  " + line)
    print()
    for line in summarize(state, bars, int(state["max_positions"])):
        print(line)
    spy_ret = (spy.loc[: pd.Timestamp(last_day)].iloc[-1] / spy.loc[pd.Timestamp(args.start) :].iloc[0] - 1) * 100
    print(f"SPY {args.start} open-session close → {last_day}: {spy_ret:.2f}% (close to close)")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if pb.STATE_PATH.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = OUTPUT / f"paper_book.before-backfill-{stamp}.json"
        shutil.copy2(pb.STATE_PATH, backup)
        print(f"\nSaved previous state → {backup}")
    state["last_entry_session"] = sessions[-1]
    pb.save_state(state)
    pb.write_trades(pb.flatten_trades(state, bars))
    print(f"Wrote {pb.STATE_PATH} and {pb.TRADES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
