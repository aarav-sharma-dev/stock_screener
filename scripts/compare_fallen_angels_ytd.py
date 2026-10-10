#!/usr/bin/env python3
"""YTD: faithful FallenAngels_Scanner.ts vs our athdip (+ loose README proxy).

Faithful rules match Eliahur7/ThinkOrSwim scanners/FallenAngels_Scanner.ts
defaults (daily Stock Hacker):

  • 8–28% below 52w high
  • close ≥ 0.985×SMA200 and SMA200 rising vs SMA200[20]
  • RSI(14) in [25, 44] and RSI > RSI[2]
  • MACD histogram (12/26/9 Diff) turning up
  • buyer support candle (lower wick ≥30% of range OR green + higher close)
  • low holding 5-bar support floor
  • price ≥ $15, 50d avg volume ≥ 1M

Same portfolio engine as peers compare: S&P500 limit=80, top_n=10 equal-weight,
daily hold to next session.
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
from stock_screener.features import compute_rsi_series, pct_from_high  # noqa: E402

import backtest_ytd as bt  # noqa: E402

OUTPUT = ROOT / "output"
LIMIT = 80
TOP_N = 10


@dataclass
class Result:
    key: str
    label: str
    ytd_return_pct: float
    spy_return_pct: float
    excess_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    avg_holdings: float
    n_signals: int
    n_signal_days: int


def _asof(series: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    return series.loc[:as_of].dropna()


def _macd_hist(close: pd.Series) -> pd.Series:
    """Thinkorswim MACD().Diff — MACD line minus signal (EMA 12/26/9)."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


def fallen_angels_scanner_score(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    *,
    rsi_min: float = 25.0,
    rsi_max: float = 44.0,
    min_pullback_pct: float = 8.0,
    max_pullback_pct: float = 28.0,
    min_price: float = 15.0,
    min_avg_volume: float = 1_000_000.0,
) -> float | None:
    """Return ranking score if FallenAngelSignal is true, else None."""
    idx = close.index.intersection(high.index).intersection(low.index)
    idx = idx.intersection(open_.index).intersection(volume.index)
    if len(idx) < 260:
        return None
    o = open_.loc[idx]
    h = high.loc[idx]
    l = low.loc[idx]
    c = close.loc[idx]
    v = volume.loc[idx]

    price = float(c.iloc[-1])
    if price < min_price:
        return None
    avg_vol50 = float(v.tail(50).mean())
    if avg_vol50 < min_avg_volume:
        return None

    high52 = float(h.tail(252).max())
    if high52 <= 0:
        return None
    pullback_pct = (high52 - price) / high52 * 100.0
    if not (min_pullback_pct <= pullback_pct <= max_pullback_pct):
        return None

    sma200 = c.rolling(200).mean()
    if len(sma200.dropna()) < 21:
        return None
    sma_now = float(sma200.iloc[-1])
    sma_20 = float(sma200.iloc[-21])  # SMA200[20] in thinkScript
    sma200_rising = sma_now >= sma_20 - (0.005 * price)
    above_sma200 = price >= (sma_now * 0.985) and sma200_rising
    if not above_sma200:
        return None

    rsi = compute_rsi_series(c, period=14).dropna()
    if len(rsi) < 3:
        return None
    rsi_val = float(rsi.iloc[-1])
    rsi_2 = float(rsi.iloc[-3])  # RSI[2]
    if not (rsi_min <= rsi_val <= rsi_max and rsi_val > rsi_2):
        return None

    hist = _macd_hist(c).dropna()
    if len(hist) < 2:
        return None
    if not (float(hist.iloc[-1]) > float(hist.iloc[-2])):
        return None

    hi = float(h.iloc[-1])
    lo = float(l.iloc[-1])
    op = float(o.iloc[-1])
    candle_range = hi - lo
    if candle_range <= 0:
        return None
    lower_wick = min(op, price) - lo
    buyer_support = (lower_wick >= candle_range * 0.30) or (
        price >= op and price >= float(c.iloc[-2])
    )
    if not buyer_support:
        return None

    # Lowest(low[2], 5) = lowest of lows from bars [2]..[6]
    if len(l) < 7:
        return None
    lowest5 = float(l.iloc[-7:-2].min())
    if lo < lowest5 * 0.985:
        return None

    # Rank: deeper pullback + lower RSI in band (still recovering)
    return pullback_pct + (rsi_max - rsi_val)


def pick_fallen_scanner(
    prices: pd.DataFrame, as_of: pd.Timestamp, universe: list[str], top_n: int
) -> list[str]:
    picks: list[tuple[str, float]] = []
    for ticker in universe:
        score = fallen_angels_scanner_score(
            _asof(bt._col(prices, "Open", ticker), as_of),
            _asof(bt._col(prices, "High", ticker), as_of),
            _asof(bt._col(prices, "Low", ticker), as_of),
            _asof(bt._col(prices, "Close", ticker), as_of),
            _asof(bt._col(prices, "Volume", ticker), as_of),
        )
        if score is not None:
            picks.append((ticker, score))
    picks.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in picks[:top_n]]


def pick_fallen_loose(
    prices: pd.DataFrame, as_of: pd.Timestamp, universe: list[str], top_n: int
) -> list[str]:
    """README-style proxy used in earlier peer compare (not the full .ts)."""
    picks: list[tuple[str, float]] = []
    for ticker in universe:
        close = _asof(bt._col(prices, "Close", ticker), as_of)
        high = _asof(bt._col(prices, "High", ticker), as_of)
        if len(close) < 220 or len(high) < 252:
            continue
        price = float(close.iloc[-1])
        sma200 = float(close.tail(200).mean())
        rsi = compute_rsi_series(close, period=14).dropna()
        if rsi.empty or price <= sma200:
            continue
        rsi14 = float(rsi.iloc[-1])
        pct52 = pct_from_high(price, float(high.tail(252).max()))
        if not (-25.0 <= pct52 <= -8.0):
            continue
        if not (25.0 <= rsi14 <= 40.0):
            continue
        picks.append((ticker, (-pct52) + (40.0 - rsi14)))
    picks.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in picks[:top_n]]


def run_daily(
    prices: pd.DataFrame,
    universe: list[str],
    *,
    key: str,
    label: str,
    year_start: pd.Timestamp,
    top_n: int,
    picker,
) -> Result:
    spy = bt._col(prices, "Close", "SPY").dropna()
    trading_days = list(spy.index[spy.index >= year_start])
    equity = 1.0
    curve: list[tuple[pd.Timestamp, float]] = []
    period_returns: list[float] = []
    holding_counts: list[int] = []
    n_signals = 0
    n_signal_days = 0

    for i, reb_date in enumerate(trading_days):
        holdings = picker(prices, reb_date, universe, top_n)
        holding_counts.append(len(holdings))
        n_signals += len(holdings)
        if holdings:
            n_signal_days += 1
        if i + 1 >= len(trading_days):
            break
        next_date = trading_days[i + 1]
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

    curve_s = pd.Series({d: v for d, v in curve}).sort_index()
    spy_ytd = (
        float(spy.loc[year_start:].iloc[-1] / spy.loc[year_start:].iloc[0] - 1.0)
        * 100.0
    )
    strat_ytd = (equity - 1.0) * 100.0
    dd = 0.0
    if not curve_s.empty:
        peak = curve_s.cummax()
        dd = float(((curve_s / peak) - 1.0).min() * 100.0)
    wins = [r for r in period_returns if r > 0]
    win_rate = (len(wins) / len(period_returns) * 100.0) if period_returns else 0.0
    return Result(
        key=key,
        label=label,
        ytd_return_pct=strat_ytd,
        spy_return_pct=spy_ytd,
        excess_pct=strat_ytd - spy_ytd,
        win_rate_pct=win_rate,
        max_drawdown_pct=dd,
        avg_holdings=float(np.mean(holding_counts)) if holding_counts else 0.0,
        n_signals=n_signals,
        n_signal_days=n_signal_days,
    )


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

    print("\n=== athdip_prod ===")
    ath = bt.run_mode_backtest(
        prices, vix, universe, "athdip", cfg, year_start, TOP_N, bars_4h=bars_4h
    )
    rows.append(
        {
            "variant": "athdip_prod",
            "label": "Ours: ATH + 4h RSI≤31",
            "return_pct": round(ath.ytd_return_pct, 2),
            "spy_pct": round(ath.spy_return_pct, 2),
            "excess_pct": round(ath.excess_pct, 2),
            "win_pct": round(ath.win_rate_pct, 1),
            "max_dd_pct": round(ath.max_drawdown_pct, 2),
            "avg_holdings": round(ath.avg_holdings, 2),
            "n_signals": len(ath.signal_rows or []),
            "n_signal_days": "",
            "note": "production athdip",
        }
    )
    print(
        f"  return={ath.ytd_return_pct:+.2f}% excess={ath.excess_pct:+.2f}% "
        f"maxDD={ath.max_drawdown_pct:.2f}% signals={len(ath.signal_rows or [])}"
    )

    for key, label, picker, note in [
        (
            "fallen_angels_scanner",
            "FallenAngels_Scanner.ts (faithful)",
            pick_fallen_scanner,
            "full ToS defaults",
        ),
        (
            "fallen_angels_loose",
            "Fallen Angels README proxy (loose)",
            pick_fallen_loose,
            "8–25% / RSI25–40 / SMA200 only",
        ),
    ]:
        print(f"\n=== {label} ===")
        res = run_daily(
            prices,
            universe,
            key=key,
            label=label,
            year_start=year_start,
            top_n=TOP_N,
            picker=picker,
        )
        print(
            f"  return={res.ytd_return_pct:+.2f}% excess={res.excess_pct:+.2f}% "
            f"win={res.win_rate_pct:.1f}% maxDD={res.max_drawdown_pct:.2f}% "
            f"avg#={res.avg_holdings:.2f} signals={res.n_signals} "
            f"days_with_sig={res.n_signal_days}"
        )
        rows.append(
            {
                "variant": res.key,
                "label": res.label,
                "return_pct": round(res.ytd_return_pct, 2),
                "spy_pct": round(res.spy_return_pct, 2),
                "excess_pct": round(res.excess_pct, 2),
                "win_pct": round(res.win_rate_pct, 1),
                "max_dd_pct": round(res.max_drawdown_pct, 2),
                "avg_holdings": round(res.avg_holdings, 2),
                "n_signals": res.n_signals,
                "n_signal_days": res.n_signal_days,
                "note": note,
            }
        )

    summary = pd.DataFrame(rows).sort_values("excess_pct", ascending=False)
    path = OUTPUT / "fallen_angels_compare_ytd.csv"
    summary.to_csv(path, index=False)
    print("\n=== COMPARE ===")
    print(summary.to_string(index=False))
    print(f"\nWrote {path}")

    ours = next(r for r in rows if r["variant"] == "athdip_prod")
    fa = next(r for r in rows if r["variant"] == "fallen_angels_scanner")
    print(
        f"\nDelta (athdip − FallenAngels_Scanner.ts): "
        f"return {ours['return_pct'] - fa['return_pct']:+.2f}pp, "
        f"excess {ours['excess_pct'] - fa['excess_pct']:+.2f}pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
