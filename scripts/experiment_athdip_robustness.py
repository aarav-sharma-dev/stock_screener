#!/usr/bin/env python3
"""Athdip robustness: costs × regime gates × IBS filter × exit rules.

All variants reuse the production athdip trigger (recent multi-year ATH +
4h RSI≤31). Filters/gates were fixed from the literature BEFORE running, not
tuned on this sample:

  gates  — VIX prior close ≥ 20 (quanterlab 2026); SPY 126d return > 0 (Alvarez)
  IBS    — entry-bar Internal Bar Strength ≤ 0.25 (Alvarez)
  exits  — hold 1 (engine default) / 3 / 5 / 10 sessions; daily RSI14 > 50;
           close > SMA5 (Connors-style); each with a 20-session time stop
  costs  — 0 / 0.05 / 0.10 % per side

Two engines:
  A) daily equal-weight book (same as backtest_ytd) — for gates/IBS/costs
  B) position-level sim, fixed 1/N sizing, enter at trigger close — for exits

Outputs: output/athdip_robustness_books.csv, output/athdip_robustness_exits.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

from stock_screener.config import ScreenerConfig  # noqa: E402
from stock_screener.data.universe import resolve_universe  # noqa: E402
from stock_screener.features import compute_rsi_series  # noqa: E402

import backtest_ytd as bt  # noqa: E402
from compare_athdip_peers_ytd import evaluate_books  # noqa: E402

OUTPUT = ROOT / "output"
COSTS = (0.0, 0.05, 0.10)
TOP_N = 10


# ---------------------------------------------------------------- signals

def collect_base_signals(
    prices: pd.DataFrame,
    vix: pd.Series,
    universe: list[str],
    cfg: ScreenerConfig,
    bars_4h: pd.DataFrame,
    start: pd.Timestamp,
) -> pd.DataFrame:
    """Every athdip trigger (not capped at top_n) with score, IBS, gate flags."""
    spy = bt._col(prices, "Close", "SPY").dropna()
    days = list(spy.index[spy.index >= start])
    rows: list[dict] = []
    vix_cfg = replace(cfg, athdip_gate_vix_min=20.0, athdip_gate_spx_mom_days=None)
    spx_cfg = replace(cfg, athdip_gate_vix_min=None, athdip_gate_spx_mom_days=126)
    for d in days:
        vix_val = float(vix.loc[:d].iloc[-1]) if not vix.loc[:d].empty else None
        market = bt.market_at(vix_val, cfg)
        _, details = bt.pick_tickers(
            prices, d, universe, "athdip", cfg, market, 500, bars_4h=bars_4h
        )
        vix_ok, _ = bt.athdip_regime_ok(prices, vix, d, vix_cfg)
        spx_ok, _ = bt.athdip_regime_ok(prices, vix, d, spx_cfg)
        for det in details or []:
            rows.append({**det, "vix_ok": vix_ok, "spx_ok": spx_ok})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def books_from_signals(
    sig: pd.DataFrame, days: list[pd.Timestamp], mask: pd.Series, top_n: int
) -> list[tuple[pd.Timestamp, list[str]]]:
    sub = sig[mask]
    by_day = {
        d: g.sort_values("score", ascending=False)["ticker"].head(top_n).tolist()
        for d, g in sub.groupby("date")
    }
    return [(d, by_day.get(d.normalize(), by_day.get(d, []))) for d in days]


# ---------------------------------------------------------------- engine B

@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_px: float
    shares: float


def position_sim(
    prices: pd.DataFrame,
    sig: pd.DataFrame,
    days: list[pd.Timestamp],
    *,
    exit_rule: str,
    hold_days: int,
    time_stop: int,
    cost_per_side_pct: float,
    max_positions: int = TOP_N,
) -> dict:
    """Fixed 1/N sizing; enter at trigger close; exits evaluated at daily close."""
    c = cost_per_side_pct / 100.0
    cash = 1.0
    positions: list[Position] = []
    equity_curve: list[float] = []
    trade_rets: list[float] = []
    holds: list[int] = []
    close_cache: dict[str, pd.Series] = {}
    rsi_cache: dict[str, pd.Series] = {}

    def close_s(t: str) -> pd.Series:
        if t not in close_cache:
            close_cache[t] = bt._col(prices, "Close", t).dropna()
        return close_cache[t]

    def rsi_s(t: str) -> pd.Series:
        if t not in rsi_cache:
            rsi_cache[t] = compute_rsi_series(close_s(t), 14)
        return rsi_cache[t]

    sig_by_day = {d: g for d, g in sig.groupby("date")}
    day_index = {d: i for i, d in enumerate(days)}

    for d in days:
        # 1) exits at today's close
        still: list[Position] = []
        for p in positions:
            s = close_s(p.ticker)
            if d not in s.index:
                still.append(p)
                continue
            px = float(s.loc[d])
            held = day_index[d] - day_index.get(p.entry_date, day_index[d])
            do_exit = False
            if exit_rule == "hold":
                do_exit = held >= hold_days
            elif exit_rule == "rsi50":
                r = rsi_s(p.ticker)
                do_exit = held >= 1 and d in r.index and float(r.loc[d]) > 50.0
            elif exit_rule == "sma5":
                sma5 = s.loc[:d].tail(5).mean()
                do_exit = held >= 1 and px > float(sma5)
            if held >= time_stop:
                do_exit = True
            if do_exit:
                proceeds = p.shares * px * (1.0 - c)
                cash += proceeds
                trade_rets.append(px * (1.0 - c) / (p.entry_px * (1.0 + c)) - 1.0)
                holds.append(held)
            else:
                still.append(p)
        positions = still

        # 2) entries at today's close (first-entry only while not held)
        todays = sig_by_day.get(d)
        if todays is not None:
            held_set = {p.ticker for p in positions}
            # Mark-to-market equity for sizing
            mtm = cash + sum(
                p.shares * float(close_s(p.ticker).loc[d])
                for p in positions
                if d in close_s(p.ticker).index
            )
            slot = mtm / max_positions
            for _, row in todays.sort_values("score", ascending=False).iterrows():
                t = row["ticker"]
                if t in held_set or len(positions) >= max_positions:
                    continue
                s = close_s(t)
                if d not in s.index:
                    continue
                px = float(s.loc[d])
                spend = min(slot, cash)
                if spend <= 0:
                    break
                shares = spend * (1.0 - c) / px
                cash -= spend
                positions.append(Position(t, d, px, shares))
                held_set.add(t)

        # 3) mark
        mtm = cash + sum(
            p.shares * float(close_s(p.ticker).loc[d])
            for p in positions
            if d in close_s(p.ticker).index
        )
        equity_curve.append(mtm)

    # Liquidate at end
    last = days[-1]
    for p in positions:
        s = close_s(p.ticker)
        px = float(s.loc[:last].iloc[-1])
        cash += p.shares * px * (1.0 - c)
        trade_rets.append(px * (1.0 - c) / (p.entry_px * (1.0 + c)) - 1.0)
        holds.append(day_index[last] - day_index.get(p.entry_date, day_index[last]))
    final_eq = cash
    curve = pd.Series(equity_curve)
    dd = float(((curve / curve.cummax()) - 1.0).min() * 100.0) if len(curve) else 0.0
    tr = np.array(trade_rets) if trade_rets else np.array([0.0])
    return {
        "return_pct": (final_eq - 1.0) * 100.0,
        "max_dd_pct": dd,
        "n_trades": len(trade_rets),
        "win_pct": float((tr > 0).mean() * 100.0),
        "avg_trade_pct": float(tr.mean() * 100.0),
        "med_trade_pct": float(np.median(tr) * 100.0),
        "avg_hold_days": float(np.mean(holds)) if holds else 0.0,
    }


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--months", type=int, default=None, help="Trailing months (else YTD)")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    if args.months:
        start = pd.Timestamp(today) - pd.DateOffset(months=args.months)
        label = f"trailing {args.months}m"
    else:
        start = pd.Timestamp(f"{today.year}-01-01")
        label = f"YTD {today.year}"
    warmup = (start - pd.Timedelta(days=1825)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    universe = resolve_universe(limit=args.limit, universe="sp500")
    print(f"{label} · {start.date()} → {today} · sp500 limit={args.limit} top={TOP_N}")
    prices = bt.download_panel(universe, warmup, end)
    vix = bt._col(prices, "Close", "^VIX")
    bars_4h = bt.download_intraday(universe, interval="4h", period="730d", chunk_size=20)
    cfg = ScreenerConfig(
        athdip_require_uptrend=False,
        athdip_require_sma50=False,
        require_above_sma_slow=False,
    )
    spy = bt._col(prices, "Close", "SPY").dropna()
    days = list(spy.index[spy.index >= start])
    spy_ret = float(spy.loc[start:].iloc[-1] / spy.loc[start:].iloc[0] - 1.0) * 100.0

    print("Collecting base athdip triggers…")
    sig = collect_base_signals(prices, vix, universe, cfg, bars_4h, start)
    if sig.empty:
        print("No athdip signals in window")
        return 1
    sig.to_csv(OUTPUT / "athdip_robustness_signals.csv", index=False)
    n_days_vix = int(sig.drop_duplicates("date")["vix_ok"].sum())
    n_days_spx = int(sig.drop_duplicates("date")["spx_ok"].sum())
    print(
        f"  {len(sig)} triggers on {sig['date'].nunique()} days · "
        f"VIX≥20 prior on {n_days_vix} of those days · SPX126>0 on {n_days_spx}"
    )
    ibs_avail = sig["ibs"].notna().mean() * 100.0
    print(f"  IBS available on {ibs_avail:.0f}% of triggers; "
          f"median IBS={sig['ibs'].median():.2f}")

    # ---- Engine A: books × filters × costs
    all_true = pd.Series(True, index=sig.index)
    filters = {
        "base": all_true,
        "ibs<=0.25": sig["ibs"].fillna(1.0) <= 0.25,
        "vix>=20": sig["vix_ok"],
        "spx126>0": sig["spx_ok"],
        "vix>=20 & spx126>0": sig["vix_ok"] & sig["spx_ok"],
        "ibs<=0.25 & spx126>0": (sig["ibs"].fillna(1.0) <= 0.25) & sig["spx_ok"],
    }
    rows_a: list[dict] = []
    for name, mask in filters.items():
        books = books_from_signals(sig, days, mask, TOP_N)
        for cost in COSTS:
            r = evaluate_books(
                prices, books, key=name, label=name, year_start=start,
                cost_per_side_pct=cost,
            )
            rows_a.append(
                {
                    "variant": name,
                    "cost_per_side_pct": cost,
                    "return_pct": round(r.ytd_return_pct, 2),
                    "spy_pct": round(spy_ret, 2),
                    "excess_pct": round(r.ytd_return_pct - spy_ret, 2),
                    "max_dd_pct": round(r.max_drawdown_pct, 2),
                    "avg_holdings": round(r.avg_holdings, 2),
                    "n_signals": r.n_signals,
                    "kept_pct": round(float(mask.mean() * 100.0), 1),
                }
            )
    df_a = pd.DataFrame(rows_a)
    df_a.to_csv(OUTPUT / "athdip_robustness_books.csv", index=False)
    print("\n=== Engine A: daily EW book (1-day hold) ===")
    for cost in COSTS:
        print(f"\n-- cost {cost:.2f}%/side --")
        print(
            df_a[df_a.cost_per_side_pct == cost][
                ["variant", "return_pct", "excess_pct", "max_dd_pct", "avg_holdings",
                 "n_signals", "kept_pct"]
            ].to_string(index=False)
        )

    # ---- Engine B: exits × costs (base signals)
    exits = [
        ("hold1", "hold", 1),
        ("hold3", "hold", 3),
        ("hold5", "hold", 5),
        ("hold10", "hold", 10),
        ("rsi14>50", "rsi50", 0),
        ("close>sma5", "sma5", 0),
    ]
    rows_b: list[dict] = []
    for name, rule, hd in exits:
        for cost in COSTS:
            r = position_sim(
                prices, sig, days, exit_rule=rule, hold_days=hd, time_stop=20,
                cost_per_side_pct=cost,
            )
            rows_b.append(
                {"exit": name, "cost_per_side_pct": cost, "spy_pct": round(spy_ret, 2),
                 **{k: round(v, 2) for k, v in r.items()}}
            )
    df_b = pd.DataFrame(rows_b)
    df_b["excess_pct"] = (df_b["return_pct"] - df_b["spy_pct"]).round(2)
    df_b.to_csv(OUTPUT / "athdip_robustness_exits.csv", index=False)
    print("\n=== Engine B: position sim (1/10 sizing, enter at trigger close, 20d time stop) ===")
    for cost in COSTS:
        print(f"\n-- cost {cost:.2f}%/side --")
        print(
            df_b[df_b.cost_per_side_pct == cost][
                ["exit", "return_pct", "excess_pct", "max_dd_pct", "n_trades", "win_pct",
                 "avg_trade_pct", "avg_hold_days"]
            ].to_string(index=False)
        )
    print(f"\nSPY {label}: {spy_ret:+.2f}%")
    print("Wrote output/athdip_robustness_books.csv, output/athdip_robustness_exits.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
