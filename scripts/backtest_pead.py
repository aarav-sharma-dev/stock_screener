#!/usr/bin/env python3
"""PEAD backtest: event study + fixed-hold portfolio sim on S&P 500 names.

Event: reported earnings (yfinance), reaction day R, entry at close[R+1].
Signals (same definitions as the production ``pead`` mode):
  surprise_pct ≥ --min-surprise  and  EAR (close[R-1]→close[R+1] minus SPY) ≥ --min-ear

Outputs
  output/pead_events.csv        every event with surprise, EAR, forward excess returns
  output/pead_event_study.csv   bucket × horizon: n, mean/median excess, hit rate, t-stat
  output/pead_portfolio.csv     hold × cost: portfolio return vs SPY, DD, trades

Caveats: Yahoo consensus ≠ I/B/E/S; survivorship (today's S&P 500 list); 2024→
is one regime. Pre-registered thresholds (5% / 3%), not tuned here.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

from stock_screener.data.pead import (  # noqa: E402
    earnings_reaction,
    fetch_earnings_events_map,
    reaction_session,
)
from stock_screener.data.universe import resolve_universe  # noqa: E402

import backtest_ytd as bt  # noqa: E402

OUTPUT = ROOT / "output"
HORIZONS = (5, 10, 20, 40, 60)
COSTS = (0.0, 0.10)


def _naive(s: pd.Series) -> pd.Series:
    s = s.dropna().copy()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = pd.DatetimeIndex(idx).normalize()
    return s


def build_events(
    prices: pd.DataFrame,
    universe: list[str],
    events_map,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    spy = _naive(bt._col(prices, "Close", "SPY"))
    rows: list[dict] = []
    for t in universe:
        close = _naive(bt._col(prices, "Close", t))
        open_ = _naive(bt._col(prices, "Open", t))
        if close.empty:
            continue
        for ev in events_map.get(t) or []:
            r = reaction_session(ev, close.index)
            if r is None or r < start or r > end:
                continue
            st = earnings_reaction(close, spy, r, open_)
            if st is None:
                continue
            pos = close.index.get_loc(st.entry_date)
            row = {
                "ticker": t,
                "report_ts": ev.event_ts.strftime("%Y-%m-%d %H:%M"),
                "after_close": ev.after_close,
                "reaction_date": r.date(),
                "entry_date": st.entry_date.date(),
                "entry_price": round(st.entry_price, 2),
                "eps_estimate": ev.eps_estimate,
                "eps_actual": ev.eps_actual,
                "surprise_pct": ev.surprise_pct,
                "ear_pct": round(st.ear_pct, 2),
                "raw_reaction_pct": round(st.raw_ret_pct, 2),
                "gap_pct": round(st.gap_pct, 2) if st.gap_pct is not None else None,
            }
            for h in HORIZONS:
                if pos + h < len(close):
                    d2 = close.index[pos + h]
                    rs = float(close.iloc[pos + h] / close.iloc[pos] - 1.0) * 100.0
                    sp = (
                        float(spy.loc[d2] / spy.loc[st.entry_date] - 1.0) * 100.0
                        if d2 in spy.index and st.entry_date in spy.index
                        else np.nan
                    )
                    row[f"ret_{h}"] = round(rs, 2)
                    row[f"xs_{h}"] = round(rs - sp, 2) if not np.isnan(sp) else np.nan
                else:
                    row[f"ret_{h}"] = np.nan
                    row[f"xs_{h}"] = np.nan
            rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["reaction_date", "ticker"]).reset_index(drop=True)
    return df


def event_study(ev: pd.DataFrame, min_sur: float, min_ear: float) -> pd.DataFrame:
    sur = ev["surprise_pct"].fillna(-999)
    buckets = {
        "all": pd.Series(True, index=ev.index),
        f"surprise>={min_sur:g}": sur >= min_sur,
        f"ear>={min_ear:g}": ev["ear_pct"] >= min_ear,
        f"PROD: surprise>={min_sur:g} & ear>={min_ear:g}": (sur >= min_sur)
        & (ev["ear_pct"] >= min_ear),
        "STRONG: surprise>=15 & ear>=5": (sur >= 15.0) & (ev["ear_pct"] >= 5.0),
        f"surprise>={min_sur:g} & ear<0 (reaction fade)": (sur >= min_sur) & (ev["ear_pct"] < 0),
        f"MIRROR: surprise<=-{min_sur:g} & ear<=-{min_ear:g}": (sur <= -min_sur)
        & (ev["ear_pct"] <= -min_ear),
    }
    rows = []
    for name, m in buckets.items():
        sub = ev[m]
        for h in HORIZONS:
            x = sub[f"xs_{h}"].dropna()
            if len(x) == 0:
                continue
            t = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else np.nan
            rows.append(
                {
                    "bucket": name,
                    "horizon": h,
                    "n": int(len(x)),
                    "mean_xs_pct": round(float(x.mean()), 2),
                    "median_xs_pct": round(float(x.median()), 2),
                    "hit_pct": round(float((x > 0).mean() * 100.0), 1),
                    "t_stat": round(t, 2) if not np.isnan(t) else None,
                    "mean_raw_pct": round(float(sub[f"ret_{h}"].dropna().mean()), 2),
                }
            )
    return pd.DataFrame(rows)


def portfolio_sim(
    prices: pd.DataFrame,
    picks: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    hold: int,
    cost_pct: float,
    max_positions: int = 20,
) -> dict:
    """Enter at entry_date close, exit after `hold` sessions, 1/N of MTM equity per slot."""
    c = cost_pct / 100.0
    spy = _naive(bt._col(prices, "Close", "SPY"))
    days = list(spy.index[(spy.index >= start) & (spy.index <= end)])
    idx = {d: i for i, d in enumerate(days)}
    closes: dict[str, pd.Series] = {}

    def cl(t: str) -> pd.Series:
        if t not in closes:
            closes[t] = _naive(bt._col(prices, "Close", t))
        return closes[t]

    by_day = {pd.Timestamp(d): g for d, g in picks.groupby("entry_date")}
    cash, positions = 1.0, []  # (ticker, entry_i, shares, entry_px)
    curve, trades = [], []
    for d in days:
        i = idx[d]
        keep = []
        for t, ei, sh, epx in positions:
            s = cl(t)
            if i - ei >= hold and d in s.index:
                px = float(s.loc[d])
                cash += sh * px * (1 - c)
                trades.append(px * (1 - c) / (epx * (1 + c)) - 1)
            else:
                keep.append((t, ei, sh, epx))
        positions = keep
        todays = by_day.get(d)
        if todays is not None:
            mtm = cash + sum(
                sh * float(cl(t).loc[d]) for t, _, sh, _ in positions if d in cl(t).index
            )
            held = {t for t, *_ in positions}
            for _, row in todays.sort_values("ear_pct", ascending=False).iterrows():
                t = row["ticker"]
                if t in held or len(positions) >= max_positions:
                    continue
                s = cl(t)
                if d not in s.index:
                    continue
                spend = min(mtm / max_positions, cash)
                if spend <= 1e-9:
                    break
                px = float(s.loc[d])
                positions.append((t, i, spend * (1 - c) / px, px))
                cash -= spend
                held.add(t)
        mtm = cash + sum(
            sh * float(cl(t).loc[d]) for t, _, sh, _ in positions if d in cl(t).index
        )
        curve.append(mtm)
    last = days[-1]
    for t, ei, sh, epx in positions:
        s = cl(t)
        px = float(s.loc[:last].iloc[-1])
        cash += sh * px * (1 - c)
        trades.append(px * (1 - c) / (epx * (1 + c)) - 1)
    cs = pd.Series(curve)
    tr = np.array(trades) if trades else np.array([0.0])
    spy_ret = float(spy.loc[days[-1]] / spy.loc[days[0]] - 1.0) * 100.0
    invested = float(np.mean([1.0 if v != cash else 0.0 for v in curve]))  # rough
    return {
        "hold": hold,
        "cost_per_side_pct": cost_pct,
        "return_pct": round((cash - 1.0) * 100.0, 2),
        "spy_pct": round(spy_ret, 2),
        "excess_pct": round((cash - 1.0) * 100.0 - spy_ret, 2),
        "max_dd_pct": round(float(((cs / cs.cummax()) - 1.0).min() * 100.0), 2),
        "n_trades": len(trades),
        "win_pct": round(float((tr > 0).mean() * 100.0), 1),
        "avg_trade_pct": round(float(tr.mean() * 100.0), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--min-surprise", type=float, default=5.0)
    ap.add_argument("--min-ear", type=float, default=3.0)
    ap.add_argument("--earnings-limit", type=int, default=16)
    args = ap.parse_args()

    today = pd.Timestamp(datetime.now(timezone.utc).date())
    start = pd.Timestamp(args.start)
    universe = resolve_universe(limit=args.limit, universe="sp500")
    print(f"PEAD backtest · {start.date()} → {today.date()} · sp500 limit={args.limit}")
    prices = bt.download_panel(
        universe,
        (start - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
        (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    spy = _naive(bt._col(prices, "Close", "SPY"))
    end = spy.index[-3]  # need R+1 and at least one forward bar

    events_map = fetch_earnings_events_map(universe, limit=args.earnings_limit)
    n_ev = sum(len(v) for v in events_map.values())
    print(f"Fetched {n_ev} reported earnings rows for {len(events_map)} tickers")

    ev = build_events(prices, universe, events_map, start, end)
    if ev.empty:
        print("No events in window")
        return 1
    ev.to_csv(OUTPUT / "pead_events.csv", index=False)
    print(f"{len(ev)} events in window · surprise known on {ev['surprise_pct'].notna().mean()*100:.0f}%")
    print(f"  median surprise={ev['surprise_pct'].median():.1f}%  median EAR={ev['ear_pct'].median():.2f}%")

    es = event_study(ev, args.min_surprise, args.min_ear)
    es.to_csv(OUTPUT / "pead_event_study.csv", index=False)
    print("\n=== Event study: excess return vs SPY from close[R+1] ===")
    print(es.to_string(index=False))

    ytd = ev[pd.to_datetime(ev["reaction_date"]) >= pd.Timestamp(f"{today.year}-01-01")]
    if not ytd.empty:
        es_ytd = event_study(ytd, args.min_surprise, args.min_ear)
        print(f"\n=== Event study, YTD {today.year} only ({len(ytd)} events) ===")
        print(
            es_ytd[es_ytd["bucket"].str.startswith(("PROD", "all"))].to_string(index=False)
        )

    sur = ev["surprise_pct"].fillna(-999)
    prod = ev[(sur >= args.min_surprise) & (ev["ear_pct"] >= args.min_ear)].copy()
    prod["entry_date"] = pd.to_datetime(prod["entry_date"])
    rows = []
    for hold in (20, 40, 60):
        for cost in COSTS:
            rows.append(portfolio_sim(prices, prod, start, spy.index[-1], hold=hold, cost_pct=cost))
    pf = pd.DataFrame(rows)
    pf.to_csv(OUTPUT / "pead_portfolio.csv", index=False)
    print("\n=== Portfolio sim (PROD bucket, max 20 positions, 1/20 sizing, enter close[R+1]) ===")
    print(pf.to_string(index=False))
    print("\nWrote output/pead_events.csv, pead_event_study.csv, pead_portfolio.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
