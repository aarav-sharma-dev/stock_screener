#!/usr/bin/env python3
"""YTD compare: our athdip vs common peer strategies from the literature.

Peers (same S&P500 limit=80 universe, equal-weight top_n=10, daily hold to next
signal day — same portfolio engine as backtest_ytd athdip):

  athdip_prod     — recent multi-year ATH + 4h RSI≤31 (production)
  ath_daily_rsi30 — same ATH setup, but daily RSI(14)≤30 (no 4h)
  fallen_angels   — 8–25% below 52w high, RSI(14) 25–40, above SMA200
                    (ToS / community “Fallen Angels” style)
  connors_rsi2    — above SMA200, below SMA5, RSI(2)<10 (Connors classic)
  ath_pct_pull    — recent ATH + 5–20% off ATH (SetupAlpha-style pullback,
                    no RSI filter)

Public references are cited in the printed summary / canvas; exact SetupAlpha
rules are proprietary — ath_pct_pull is a transparent proxy only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
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
from stock_screener.features import (  # noqa: E402
    compute_rsi,
    pct_from_high,
    recent_multi_year_ath_setup,
)

import backtest_ytd as bt  # noqa: E402

OUTPUT = ROOT / "output"
LIMIT = 80
TOP_N = 10


@dataclass
class PeerResult:
    key: str
    label: str
    ytd_return_pct: float
    spy_return_pct: float
    excess_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    avg_holdings: float
    n_signals: int


def _asof_slice(series: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    return series.loc[:as_of].dropna()


def pick_peer(
    prices: pd.DataFrame,
    as_of: pd.Timestamp,
    universe: list[str],
    key: str,
    cfg: ScreenerConfig,
    top_n: int,
) -> list[str]:
    picks: list[tuple[str, float]] = []
    for ticker in universe:
        close = _asof_slice(bt._col(prices, "Close", ticker), as_of)
        high = _asof_slice(bt._col(prices, "High", ticker), as_of)
        low = _asof_slice(bt._col(prices, "Low", ticker), as_of)
        vol = _asof_slice(bt._col(prices, "Volume", ticker), as_of)
        if len(close) < 220:
            continue
        price = float(close.iloc[-1])
        sma5 = float(close.tail(5).mean())
        sma200 = float(close.tail(200).mean())
        rsi14 = compute_rsi(close, period=14)
        rsi2 = compute_rsi(close, period=2)
        if np.isnan(rsi14) or np.isnan(rsi2):
            continue

        if key == "connors_rsi2":
            if price <= sma200 or price >= sma5 or rsi2 >= 10.0:
                continue
            # deeper RSI2 = stronger signal
            picks.append((ticker, 10.0 - rsi2))
            continue

        if key == "fallen_angels":
            if len(high) < 252:
                continue
            hi52 = float(high.tail(252).max())
            pct52 = pct_from_high(price, hi52)
            if not (-25.0 <= pct52 <= -8.0):
                continue
            if not (25.0 <= rsi14 <= 40.0):
                continue
            if price <= sma200:
                continue
            # prefer deeper pullback + lower RSI
            picks.append((ticker, (-pct52) + (40.0 - rsi14)))
            continue

        # ATH-based peers share setup
        ok_setup, _, ath_setup, days_since = recent_multi_year_ath_setup(
            high,
            close,
            fresh_window=cfg.athdip_fresh_high_days,
            max_days_since_ath=cfg.athdip_max_days_since_ath,
        )
        if not ok_setup or ath_setup is None:
            continue
        pct_ath = pct_from_high(price, float(ath_setup))

        if key == "ath_daily_rsi30":
            if rsi14 > 30.0:
                continue
            picks.append((ticker, 30.0 - rsi14))
            continue

        if key == "ath_pct_pull":
            if not (-20.0 <= pct_ath <= -5.0):
                continue
            # prefer mid-pullbacks closer to -10% with fresher ATH
            freshness = max(0.0, 63.0 - float(days_since or 63))
            picks.append((ticker, (-pct_ath) + 0.1 * freshness))
            continue

    picks.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in picks[:top_n]]


def collect_peer_books(
    prices: pd.DataFrame,
    universe: list[str],
    key: str,
    cfg: ScreenerConfig,
    year_start: pd.Timestamp,
    top_n: int,
) -> list[tuple[pd.Timestamp, list[str]]]:
    """Daily (date, holdings) sequence for a peer rule — computed once."""
    spy = bt._col(prices, "Close", "SPY").dropna()
    trading_days = list(spy.index[spy.index >= year_start])
    return [(d, pick_peer(prices, d, universe, key, cfg, top_n)) for d in trading_days]


def evaluate_books(
    prices: pd.DataFrame,
    books: list[tuple[pd.Timestamp, list[str]]],
    *,
    key: str,
    label: str,
    year_start: pd.Timestamp,
    cost_per_side_pct: float,
) -> PeerResult:
    """Price a daily equal-weight book sequence with per-side turnover costs."""
    spy = bt._col(prices, "Close", "SPY").dropna()
    equity = 1.0
    curve: list[tuple[pd.Timestamp, float]] = []
    period_returns: list[float] = []
    holding_counts: list[int] = []
    n_signals = 0
    prev: list[str] = []

    for i, (reb_date, holdings) in enumerate(books):
        holding_counts.append(len(holdings))
        n_signals += len(holdings)
        cost = bt.turnover_cost(prev, holdings, cost_per_side_pct)
        if cost:
            equity *= 1.0 - cost
        prev = list(holdings)
        if i + 1 >= len(books):
            break
        next_date = books[i + 1][0]
        if not holdings:
            period_returns.append(0.0)
            curve.append((next_date, equity))
            continue
        rets = []
        for t in holdings:
            s = bt._col(prices, "Close", t).loc[reb_date:next_date]
            if len(s) < 2:
                continue
            rets.append(float(s.iloc[-1] / s.iloc[0] - 1.0))
        port_ret = float(np.mean(rets)) if rets else 0.0
        period_returns.append(port_ret)
        equity *= 1.0 + port_ret
        curve.append((next_date, equity))
    final = bt.turnover_cost(prev, [], cost_per_side_pct)
    if final:
        equity *= 1.0 - final

    curve_s = pd.Series({d: v for d, v in curve}).sort_index()
    spy_ytd = float(spy.loc[year_start:].iloc[-1] / spy.loc[year_start:].iloc[0] - 1.0) * 100.0
    strat_ytd = (equity - 1.0) * 100.0
    dd = 0.0
    if not curve_s.empty:
        peak = curve_s.cummax()
        dd = float(((curve_s / peak) - 1.0).min() * 100.0)
    wins = [r for r in period_returns if r > 0]
    win_rate = (len(wins) / len(period_returns) * 100.0) if period_returns else 0.0
    avg_h = float(np.mean(holding_counts)) if holding_counts else 0.0
    return PeerResult(
        key=key,
        label=label,
        ytd_return_pct=strat_ytd,
        spy_return_pct=spy_ytd,
        excess_pct=strat_ytd - spy_ytd,
        win_rate_pct=win_rate,
        max_drawdown_pct=dd,
        avg_holdings=avg_h,
        n_signals=n_signals,
    )


COSTS = (0.0, 0.05, 0.10)  # percent per side


def main() -> int:
    today = datetime.now(timezone.utc).date()
    year_start = pd.Timestamp(f"{today.year}-01-01")
    warmup_start = (year_start - pd.Timedelta(days=1825)).strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    universe = resolve_universe(limit=LIMIT, universe="sp500")
    print(f"Universe sp500 limit={LIMIT} · YTD {year_start.date()} → {today}")
    print("Downloading daily + 4h…")
    prices = bt.download_panel(universe, warmup_start, end)
    vix = bt._col(prices, "Close", "^VIX")
    bars_4h = bt.download_intraday(
        universe, interval="4h", period="730d", chunk_size=20
    )
    cfg = ScreenerConfig(
        athdip_require_uptrend=False,
        athdip_require_sma50=False,
        require_above_sma_slow=False,
    )

    rows: list[dict] = []

    def _row(variant: str, label: str, family: str, cost: float, r) -> dict:
        return {
            "variant": variant,
            "label": label,
            "family": family,
            "cost_per_side_pct": cost,
            "return_pct": round(r.ytd_return_pct, 2),
            "spy_pct": round(r.spy_return_pct, 2),
            "excess_pct": round(r.excess_pct, 2),
            "win_pct": round(r.win_rate_pct, 1),
            "max_dd_pct": round(r.max_drawdown_pct, 2),
            "avg_holdings": round(r.avg_holdings, 2),
            "n_signals": (
                r.n_signals if hasattr(r, "n_signals") else len(r.signal_rows or [])
            ),
        }

    print("\n=== athdip_prod (4h RSI≤31) ===")
    for cost in COSTS:
        ath = bt.run_mode_backtest(
            prices,
            vix,
            universe,
            "athdip",
            cfg,
            year_start,
            TOP_N,
            bars_4h=bars_4h,
            cost_per_side_pct=cost,
        )
        rows.append(_row("athdip_prod", "Ours: ATH + 4h RSI≤31", "ours", cost, ath))
        print(
            f"  cost={cost:.2f}%  return={ath.ytd_return_pct:+.2f}% "
            f"excess={ath.excess_pct:+.2f}% maxDD={ath.max_drawdown_pct:.2f}% "
            f"signals={len(ath.signal_rows or [])} paid={ath.total_cost_pct:.2f}%"
        )

    peers = [
        ("ath_daily_rsi30", "Peer: ATH + daily RSI≤30", "daily-rsi"),
        ("fallen_angels", "Peer: Fallen Angels (52w/−8–25% + RSI 25–40)", "community"),
        ("connors_rsi2", "Peer: Connors RSI(2)<10 + SMA200", "classic"),
        ("ath_pct_pull", "Peer: ATH −5–20% pullback (no RSI)", "ath-proxy"),
    ]
    for key, label, family in peers:
        print(f"\n=== {label} ===")
        books = collect_peer_books(prices, universe, key, cfg, year_start, TOP_N)
        for cost in COSTS:
            res = evaluate_books(
                prices,
                books,
                key=key,
                label=label,
                year_start=year_start,
                cost_per_side_pct=cost,
            )
            rows.append(_row(key, label, family, cost, res))
            print(
                f"  cost={cost:.2f}%  return={res.ytd_return_pct:+.2f}% "
                f"excess={res.excess_pct:+.2f}% maxDD={res.max_drawdown_pct:.2f}% "
                f"signals={res.n_signals}"
            )

    summary = pd.DataFrame(rows).sort_values(
        ["cost_per_side_pct", "excess_pct"], ascending=[True, False]
    )
    path = OUTPUT / "athdip_peers_compare_ytd.csv"
    summary.to_csv(path, index=False)
    print("\n=== PEER COMPARE (YTD) by cost ===")
    for cost in COSTS:
        print(f"\n-- cost {cost:.2f}% per side --")
        print(
            summary[summary["cost_per_side_pct"] == cost][
                ["label", "return_pct", "excess_pct", "max_dd_pct", "avg_holdings", "n_signals"]
            ].to_string(index=False)
        )
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
