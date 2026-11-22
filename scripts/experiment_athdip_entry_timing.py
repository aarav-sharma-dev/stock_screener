#!/usr/bin/env python3
"""Entry timing for athdip triggers: buy on the trigger, or wait for confirmation?

Signals are the production athdip triggers from ``experiment_athdip_robustness``
(recent multi-year ATH, min 4h RSI ≤ 31 over 5 days, fired on the day that low
printed). Only the entry rule changes. Exits are the same two tested rules as the
paper book: hold 10 sessions, or first daily RSI(14) > 50 after one session, with
a 20-session stop.

Entry variants (fixed before running; not tuned on the sample):

  trigger_close       buy the trigger day's close (paper book / 2026 test)
  next_open           buy the next session's open
  next_close          buy the next session's close
  wait3_close         buy the close three sessions after the trigger, no test
  confirm_prior_high  first close in the next 5 sessions above the prior day's high
  confirm_sma5        first close in the next 5 sessions above its 5-day average
  confirm_rsi_up      first daily RSI(14) in the next 5 sessions above the prior day
  filter_20d_low      trigger close, only if it is the lowest close of 20 sessions

Costs: 0 and 10 bps per side. Excess is net return minus SPY over the same dates.

Trades are scored one at a time (no slot limit), so these are per-trade statistics.
Only trades whose exit happened by the last bar count; open trades are reported.

  python3 scripts/experiment_athdip_entry_timing.py            # trailing 24 months
  python3 scripts/experiment_athdip_entry_timing.py --limit 80
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

import backtest_ytd as bt  # noqa: E402
from experiment_athdip_robustness import collect_base_signals  # noqa: E402
from stock_screener.config import ScreenerConfig  # noqa: E402
from stock_screener.data.prices import download_intraday  # noqa: E402
from stock_screener.data.universe import resolve_universe  # noqa: E402
from stock_screener.features import compute_rsi_series  # noqa: E402

OUTPUT = ROOT / "output"
COSTS = (0.0, 0.001)
EXITS = ("hold10", "rsi50")
ENTRIES = (
    "trigger_close",
    "next_open",
    "next_close",
    "wait3_close",
    "confirm_prior_high",
    "confirm_sma5",
    "confirm_rsi_up",
    "filter_20d_low",
)
TIME_STOP = 20
HOLD = 10
CONFIRM_WINDOW = 5


def _net(entry: float, exit_: float, cost: float) -> float:
    return (exit_ * (1 - cost)) / (entry * (1 + cost)) - 1


def _entry(
    variant: str,
    i: int,
    o: np.ndarray,
    h: np.ndarray,
    c: np.ndarray,
    sma5: np.ndarray,
    rsi: np.ndarray,
) -> tuple[int, float] | None:
    """Entry index and price for a trigger at calendar index ``i``, or None."""
    n = len(c)
    if variant == "trigger_close":
        return (i, c[i]) if not np.isnan(c[i]) else None
    if variant == "filter_20d_low":
        if i < 19 or np.isnan(c[i]):
            return None
        window = c[i - 19 : i + 1]
        return (i, c[i]) if np.nanmin(window) == c[i] else None
    if variant == "next_open":
        j = i + 1
        return (j, o[j]) if j < n and not np.isnan(o[j]) else None
    if variant == "next_close":
        j = i + 1
        return (j, c[j]) if j < n and not np.isnan(c[j]) else None
    if variant == "wait3_close":
        j = i + 3
        return (j, c[j]) if j < n and not np.isnan(c[j]) else None
    for j in range(i + 1, min(n, i + 1 + CONFIRM_WINDOW)):
        if np.isnan(c[j]):
            continue
        if variant == "confirm_prior_high" and c[j] > h[j - 1]:
            return j, c[j]
        if variant == "confirm_sma5" and not np.isnan(sma5[j]) and c[j] > sma5[j]:
            return j, c[j]
        if (
            variant == "confirm_rsi_up"
            and not np.isnan(rsi[j])
            and not np.isnan(rsi[j - 1])
            and rsi[j] > rsi[j - 1]
        ):
            return j, c[j]
    return None


def _exit(
    rule: str, j: int, c: np.ndarray, rsi: np.ndarray
) -> tuple[int, float, int] | None:
    """(exit index, exit price, held) or None if the trade is still open."""
    n = len(c)
    for held in range(1, TIME_STOP + 1):
        k = j + held
        if k >= n:
            return None
        if np.isnan(c[k]):
            continue
        if held == TIME_STOP:
            return k, c[k], held
        if rule == "hold10" and held >= HOLD:
            return k, c[k], held
        if rule == "rsi50" and held >= 1 and not np.isnan(rsi[k]) and rsi[k] > 50:
            return k, c[k], held
    return None


def run(signals: pd.DataFrame, panel: dict, calendar: list[pd.Timestamp], spy: np.ndarray) -> pd.DataFrame:
    rows = []
    for rec in signals.itertuples(index=False):
        t = rec.ticker
        if t not in panel:
            continue
        o, h, c, sma5, rsi = panel[t]
        i = calendar.index(pd.Timestamp(rec.trigger_date))
        for variant in ENTRIES:
            got = _entry(variant, i, o, h, c, sma5, rsi)
            if got is None:
                rows.append(
                    {"entry": variant, "ticker": t, "trigger": rec.trigger_date, "status": "no_entry"}
                )
                continue
            j, px = got
            for rule in EXITS:
                out = _exit(rule, j, c, rsi)
                base = {
                    "entry": variant,
                    "exit": rule,
                    "ticker": t,
                    "trigger": rec.trigger_date,
                    "entry_date": calendar[j].strftime("%Y-%m-%d"),
                    "sessions_after_trigger": j - i,
                    "entry_px": float(px),
                }
                if out is None:
                    rows.append({**base, "status": "open"})
                    continue
                k, xpx, held = out
                spy_ret = spy[k] / spy[j] - 1
                for cost in COSTS:
                    net = _net(px, xpx, cost)
                    rows.append(
                        {
                            **base,
                            "status": "closed",
                            "cost_bps": int(round(cost * 10000)),
                            "exit_date": calendar[k].strftime("%Y-%m-%d"),
                            "held": held,
                            "net": net,
                            "excess": net - spy_ret,
                        }
                    )
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    closed = trades[trades["status"] == "closed"]
    out = []
    for (period, entry, exit_, cost), g in closed.groupby(["period", "entry", "exit", "cost_bps"]):
        signals = trades[(trades["period"] == period) & (trades["entry"] == entry)]
        n_sig = signals["trigger"].nunique() if "trigger" in signals else 0
        fired = signals[(signals["status"] != "no_entry") & (signals["exit"] == exit_)]
        entered = fired.drop_duplicates(subset=["ticker", "trigger"]).shape[0]
        out.append(
            {
                "period": period,
                "entry": entry,
                "exit": exit_,
                "cost_bps": cost,
                "triggers": n_sig,
                "entered": int(entered),
                "closed": len(g),
                "win_pct": round((g["net"] > 0).mean() * 100, 1),
                "avg_net_pct": round(g["net"].mean() * 100, 2),
                "med_net_pct": round(g["net"].median() * 100, 2),
                "avg_excess_pct": round(g["excess"].mean() * 100, 2),
                "avg_held": round(g["held"].mean(), 1),
                "avg_sessions_after_trigger": round(g["sessions_after_trigger"].mean(), 2),
            }
        )
    return pd.DataFrame(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--months", type=int, default=24)
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    start = pd.Timestamp(today) - pd.DateOffset(months=args.months)
    warmup = (start - pd.Timedelta(days=1825)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    universe = resolve_universe(limit=args.limit, universe="sp500")
    print(f"Universe {len(universe)} · trailing {args.months}m from {start.date()} → {today}")
    prices = bt.download_panel(universe, warmup, end)
    vix = bt._col(prices, "Close", "^VIX")
    spy_s = bt._col(prices, "Close", "SPY").dropna()
    calendar = list(spy_s.index)
    spy = spy_s.to_numpy(dtype=float)

    cfg = ScreenerConfig(
        athdip_require_uptrend=False,
        athdip_require_sma50=False,
        require_above_sma_slow=False,
    )
    print("Downloading 4h bars (Yahoo, 730d)…")
    bars_4h = download_intraday(universe, interval="4h", period="730d", chunk_size=20)
    print("Collecting athdip triggers…")
    sig = collect_base_signals(prices, vix, universe, cfg, bars_4h, start)
    if sig.empty:
        print("No triggers.")
        return 1
    sig["trigger_date"] = sig["trigger_date"].astype(str).str[:10]
    sig = sig.drop_duplicates(subset=["ticker", "trigger_date"])
    print(f"{len(sig)} triggers on {sig['trigger_date'].nunique()} days")

    cal_index = pd.DatetimeIndex(calendar)
    panel: dict[str, tuple] = {}
    for t in sig["ticker"].unique():
        o = bt._col(prices, "Open", t).reindex(cal_index).to_numpy(dtype=float)
        h = bt._col(prices, "High", t).reindex(cal_index).to_numpy(dtype=float)
        c = bt._col(prices, "Close", t).reindex(cal_index).to_numpy(dtype=float)
        if np.isnan(c).all():
            continue
        cs = pd.Series(c, index=cal_index)
        sma5 = cs.rolling(5, min_periods=5).mean().to_numpy(dtype=float)
        rsi = compute_rsi_series(cs.dropna(), 14).reindex(cal_index).to_numpy(dtype=float)
        panel[t] = (o, h, c, sma5, rsi)

    print("Replaying entries and exits…")
    trades = run(sig, panel, calendar, spy)
    trades["trigger"] = trades["trigger"].astype(str)

    mid = start + (pd.Timestamp(today) - start) / 2
    trades["period"] = "all"
    trig = pd.to_datetime(trades["trigger"])
    frames = [trades.assign(period="all")]
    frames.append(trades[trig < mid].assign(period="first_half"))
    frames.append(trades[trig >= mid].assign(period="second_half"))
    ytd = pd.Timestamp(f"{today.year}-01-01")
    frames.append(trades[trig >= ytd].assign(period=f"ytd_{today.year}"))
    stacked = pd.concat(frames, ignore_index=True)

    summary = summarize(stacked)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUTPUT / "athdip_entry_timing.csv"
    summary.to_csv(out_csv, index=False)
    trades.to_csv(OUTPUT / "athdip_entry_timing_trades.csv", index=False)

    with pd.option_context("display.width", 200, "display.max_columns", 20):
        for period in summary["period"].unique():
            sub = summary[(summary["period"] == period) & (summary["cost_bps"] == 10)]
            print(f"\n=== {period} · 10 bps/side ===")
            print(sub.drop(columns=["cost_bps"]).to_string(index=False))
    print(f"\nWrote {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
