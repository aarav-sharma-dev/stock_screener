#!/usr/bin/env python3
"""
YTD backtest: support vs catalyst vs enhanced value sketch.

Value mode is EXPERIMENTAL and backtest-only — does not replace live CLI modes.
Yahoo fundamentals are current snapshots (look-ahead on multiples); disclosed below.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from stock_screener.config import ScreenerConfig
from stock_screener.data.earnings import EarningsAssessment
from stock_screener.data.fundamentals import FundamentalsSnapshot, fetch_fundamentals
from stock_screener.data.market_cycle import MarketCycle, assess_market_cycle
from stock_screener.data.news import NewsAssessment
from stock_screener.data.reddit import RedditAssessment
from stock_screener.data.sentiment import MarketSentiment, RegimeParams, _regime_for
from stock_screener.data.trump_lexicon import all_tracked_tickers
from stock_screener.data.universe import resolve_universe
from stock_screener.data.prices import download_intraday
from stock_screener.features import (
    all_time_high,
    compute_technicals,
    min_rsi_lookback_detail,
    pct_from_high,
    recent_multi_year_ath_setup,
)
from stock_screener.scoring import (
    athdip_composite,
    build_catalyst_row,
    build_support_row,
)


NEUTRAL_NEWS = NewsAssessment(50.0, False, 0, "", "neutral (backtest)")
NEUTRAL_EARN = EarningsAssessment(None, 50.0, "neutral", 50.0, "neutral")
NEUTRAL_REDDIT = RedditAssessment(50.0, 0, "neutral")


# ---------------------------------------------------------------------------
# VALUE MODE SKETCH v2 (backtest-only)
#
# Hard gates:
#   - ROE ≥ 15%
#   - Cheap: valuation_score ≥ 55 OR (PE≤20 and PB<3) OR PEG ≤ 1.5
#   - Prefer FCF yield > 0 (hard require if yield available and ≤ 0 → fail)
#   - Avoid trap: D/E > 200 and current ratio < 1
#   - No chase: RSI(14) < 75
#
# Ranking (sums ≈ 1.0):
#   35% valuation multiples | 15% PEG | 15% FCF yield
#   15% profitability/margins | 10% ROE/capital | 10% balance
# Soft: +2 if RSI ≤ 50
#
# Market-cycle overlay (CAPE + Buffett Indicator):
#   expensive → higher min score, fewer names
#   cheap → lower bar, full top_n
# ---------------------------------------------------------------------------


@dataclass
class ValueScore:
    ticker: str
    score: float
    valuation: float
    peg: float | None
    fcf_yield: float | None
    roe: float | None
    pass_filters: bool
    reason: str


def _peg_component(peg: float | None) -> float:
    if peg is None or peg <= 0:
        return 50.0
    if peg < 1.0:
        return 90.0
    if peg < 1.5:
        return 75.0
    if peg < 2.0:
        return 55.0
    if peg < 3.0:
        return 40.0
    return 25.0


def _fcfy_component(fcfy: float | None) -> float:
    if fcfy is None:
        return 50.0
    if fcfy >= 0.06:
        return 90.0
    if fcfy >= 0.04:
        return 75.0
    if fcfy >= 0.02:
        return 60.0
    if fcfy > 0:
        return 45.0
    return 20.0


def score_value_mode(
    snap: FundamentalsSnapshot,
    rsi14: float | None = None,
    *,
    min_score: float = 55.0,
) -> ValueScore:
    """Dedicated value score with PEG, FCF yield, ROE≥15% quality floor."""
    val = snap.valuation_score
    peg = snap.peg
    fcfy = snap.fcf_yield
    roe = snap.roe

    if roe is None or roe < 0.15:
        return ValueScore(
            snap.ticker,
            float("nan"),
            val,
            peg,
            fcfy,
            roe,
            False,
            f"ROE<{0.15:.0%} ({roe if roe is not None else 'n/a'})",
        )

    pe = snap.trailing_pe
    fpe = snap.forward_pe
    pb = snap.price_to_book
    cheap_gate = val >= 55.0
    if not cheap_gate:
        pe_ok = any(x is not None and 0 < x <= 20 for x in (pe, fpe))
        pb_ok = pb is not None and 0 < pb < 3.0
        peg_ok = peg is not None and 0 < peg <= 1.5
        cheap_gate = (pe_ok and pb_ok) or peg_ok

    if not cheap_gate:
        return ValueScore(
            snap.ticker, float("nan"), val, peg, fcfy, roe, False, "not cheap enough"
        )

    if fcfy is not None and fcfy <= 0:
        return ValueScore(
            snap.ticker, float("nan"), val, peg, fcfy, roe, False, "FCF yield ≤ 0"
        )

    de = snap.debt_to_equity
    cr = snap.current_ratio
    if de is not None and de > 200 and (cr is None or cr < 1.0):
        return ValueScore(
            snap.ticker, float("nan"), val, peg, fcfy, roe, False, "value-trap BS"
        )

    if rsi14 is not None and rsi14 >= 75.0:
        return ValueScore(
            snap.ticker, float("nan"), val, peg, fcfy, roe, False, "RSI chase"
        )

    score = (
        0.35 * val
        + 0.15 * _peg_component(peg)
        + 0.15 * _fcfy_component(fcfy)
        + 0.15 * snap.profitability_score
        + 0.10 * snap.capital_score
        + 0.10 * snap.balance_score
    )
    if rsi14 is not None and rsi14 <= 50.0:
        score = min(100.0, score + 2.0)

    if score < min_score:
        return ValueScore(
            snap.ticker,
            float("nan"),
            val,
            peg,
            fcfy,
            roe,
            False,
            f"below cycle min {min_score:.0f}",
        )

    return ValueScore(
        snap.ticker,
        round(score, 2),
        val,
        peg,
        fcfy,
        roe,
        True,
        f"val={val:.0f}; peg={peg if peg is not None else 'n/a'}; "
        f"fcfy={f'{fcfy:.1%}' if fcfy is not None else 'n/a'}; roe={roe:.0%}",
    )


@dataclass
class BacktestResult:
    mode: str
    ytd_return_pct: float
    spy_return_pct: float
    excess_pct: float
    rebalance_count: int
    avg_holdings: float
    win_rate_pct: float
    max_drawdown_pct: float
    signal_rows: list[dict] | None = None
    cost_per_side_pct: float = 0.0
    total_cost_pct: float = 0.0  # cumulative % of equity paid in costs
    gated_days: int = 0  # athdip days skipped by regime gates


def market_at(vix: float | None, cfg: ScreenerConfig) -> MarketSentiment:
    if vix is None:
        label, score, reason = "neutral", 50.0, "VIX unavailable"
    elif vix <= cfg.vix_risk_on:
        label = "risk-on"
        score = 80.0 + max(0.0, (cfg.vix_risk_on - vix) * 2.0)
        reason = f"VIX {vix:.1f} ≤ {cfg.vix_risk_on:.0f}"
    elif vix >= cfg.vix_risk_off:
        label = "risk-off"
        score = max(5.0, 40.0 - (vix - cfg.vix_risk_off) * 2.0)
        reason = f"VIX {vix:.1f} ≥ {cfg.vix_risk_off:.0f}"
    else:
        span = cfg.vix_risk_off - cfg.vix_risk_on
        t = (vix - cfg.vix_risk_on) / span
        score = 80.0 - t * 40.0
        label = "neutral"
        reason = f"VIX {vix:.1f} in mid range"
    regime: RegimeParams = _regime_for(label, cfg)
    return MarketSentiment(
        vix=vix,
        score=min(100.0, max(0.0, score)),
        label=label,
        reason=reason,
        regime=regime,
    )


def download_panel(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    return yf.download(
        tickers=tickers + ["SPY", "^VIX"],
        start=start,
        end=end,
        group_by="column",
        auto_adjust=True,
        threads=True,
        progress=False,
    )


def _col(frame: pd.DataFrame, field: str, ticker: str) -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    if (field, ticker) in frame.columns:
        return frame[(field, ticker)].dropna()
    if (ticker, field) in frame.columns:
        return frame[(ticker, field)].dropna()
    return pd.Series(dtype=float)


def _rsi_4h_asof(
    close_4h: pd.Series,
    as_of: pd.Timestamp,
    *,
    lookback_days: int = 5,
) -> float | None:
    """Min point-in-time 4h RSI over ``lookback_days`` ending on ``as_of``."""
    detail = min_rsi_lookback_detail(
        close_4h,
        as_of=as_of,
        lookback_days=lookback_days,
        period=14,
    )
    return None if detail is None else detail[0]


def _rsi_4h_trigger(
    close_4h: pd.Series,
    as_of: pd.Timestamp,
    *,
    lookback_days: int = 5,
) -> tuple[float, object] | None:
    """``(min_rsi, trigger_date)`` in the lookback window ending on ``as_of``."""
    return min_rsi_lookback_detail(
        close_4h,
        as_of=as_of,
        lookback_days=lookback_days,
        period=14,
    )

def fetch_fund_map(tickers: list[str]) -> dict[str, FundamentalsSnapshot]:
    out: dict[str, FundamentalsSnapshot] = {}

    def _one(t: str) -> tuple[str, FundamentalsSnapshot | None]:
        return t, fetch_fundamentals(t)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futs):
            t, snap = fut.result()
            if snap is not None and snap.pass_filters:
                out[t] = snap
    return out


def pick_tickers(
    prices: pd.DataFrame,
    as_of: pd.Timestamp,
    universe: list[str],
    mode: str,
    cfg: ScreenerConfig,
    market: MarketSentiment,
    top_n: int,
    fund_map: dict[str, FundamentalsSnapshot] | None = None,
    cycle: MarketCycle | None = None,
    bars_4h: pd.DataFrame | None = None,
) -> list[str]:
    picks: list[tuple[str, float]] = []
    min_score = cycle.min_value_score if cycle else 55.0
    for ticker in universe:
        close = _col(prices, "Close", ticker).loc[:as_of]
        high = _col(prices, "High", ticker).loc[:as_of]
        low = _col(prices, "Low", ticker).loc[:as_of]
        vol = _col(prices, "Volume", ticker).loc[:as_of]
        tech = compute_technicals(
            close,
            high,
            low,
            vol,
            donchian_window=cfg.donchian_window,
            sma_fast=cfg.sma_fast,
            sma_slow=cfg.sma_slow,
            avg_volume_window=cfg.avg_volume_window,
        )
        if tech is None:
            continue
        if tech.price < cfg.min_price or tech.avg_dollar_volume < cfg.min_avg_dollar_volume:
            continue

        if mode == "value":
            snap = (fund_map or {}).get(ticker)
            if snap is None:
                continue
            vs = score_value_mode(snap, rsi14=tech.rsi14, min_score=min_score)
            if vs.pass_filters and not np.isnan(vs.score):
                picks.append((ticker, vs.score))
            continue

        if mode == "trump":
            # Proxy: lexicon universe + 10d momentum / vol spike (no historical Truth posts)
            if len(close) < 12:
                continue
            if tech.rsi14 is not None and tech.rsi14 >= 80:
                continue
            ret10 = float(close.iloc[-1] / close.iloc[-11] - 1.0) * 100.0
            vol_boost = min(2.0, tech.volume_ratio) if tech.volume_ratio else 1.0
            score = ret10 * vol_boost
            if score <= 0:
                continue
            picks.append((ticker, score))
            continue

        if mode == "athdip":
            # Setup: recent multi-year ATH; fire only on actual 4h RSI trigger day
            ath = all_time_high(high)
            if ath is None:
                continue
            pct_ath = pct_from_high(tech.price, ath)
            if cfg.athdip_require_uptrend:
                if cfg.athdip_require_sma50:
                    if not (tech.above_sma_fast and tech.above_sma_slow):
                        continue
                elif not tech.above_sma_slow:
                    continue
            if cfg.athdip_drop_death_cross and tech.death_cross:
                continue
            if (
                cfg.athdip_require_near_ath_pct
                and pct_ath < cfg.athdip_max_pct_from_ath
            ):
                continue
            days_since: int | None = None
            if cfg.athdip_require_multi_year_break:
                ok_setup, _, ath_setup, days_since = recent_multi_year_ath_setup(
                    high,
                    close,
                    fresh_window=cfg.athdip_fresh_high_days,
                    max_days_since_ath=cfg.athdip_max_days_since_ath,
                )
                if not ok_setup:
                    continue
                if ath_setup is not None:
                    ath = ath_setup
                    pct_ath = pct_from_high(tech.price, ath)
            trig = _rsi_4h_trigger(
                _col(bars_4h, "Close", ticker)
                if bars_4h is not None
                else pd.Series(dtype=float),
                as_of,
                lookback_days=cfg.athdip_rsi_lookback_days,
            )
            if trig is None:
                continue
            rsi_4h, trigger_date = trig
            asof_date = (
                pd.Timestamp(as_of).tz_convert("America/New_York").date()
                if getattr(as_of, "tzinfo", None)
                else pd.Timestamp(as_of).date()
            )
            # Only signal on the calendar day the lookback-min 4h RSI printed
            if trigger_date != asof_date:
                continue
            if rsi_4h > cfg.athdip_rsi_4h_max:
                continue
            ibs: float | None = None
            if len(high) and len(low) and len(close):
                rng = float(high.iloc[-1]) - float(low.iloc[-1])
                if rng > 0:
                    ibs = (float(close.iloc[-1]) - float(low.iloc[-1])) / rng
            if cfg.athdip_ibs_max is not None and (ibs is None or ibs > cfg.athdip_ibs_max):
                continue
            score = athdip_composite(
                tech,
                pct_from_ath=pct_ath,
                rsi_4h=rsi_4h,
                config=cfg,
                days_since_ath=days_since,
            )
            picks.append(
                (
                    ticker,
                    score,
                    {
                        "trigger_date": str(trigger_date),
                        "price": round(tech.price, 2),
                        "ath": round(float(ath), 2),
                        "pct_from_ath": round(pct_ath, 2),
                        "days_since_ath": days_since,
                        "rsi_4h": round(rsi_4h, 2),
                        "ibs": round(ibs, 3) if ibs is not None else None,
                        "score": round(score, 2),
                    },
                )
            )
            continue

        if mode == "support":
            row = build_support_row(
                ticker, tech, market, NEUTRAL_NEWS, NEUTRAL_EARN, NEUTRAL_REDDIT, None, cfg
            )
        else:
            row = build_catalyst_row(
                ticker, tech, market, NEUTRAL_NEWS, NEUTRAL_EARN, NEUTRAL_REDDIT, None, cfg
            )
        if row.pass_filters and not np.isnan(row.composite):
            picks.append((ticker, row.composite))

    picks.sort(key=lambda x: x[1], reverse=True)
    top = picks[:top_n]
    if mode == "athdip":
        # Return (tickers, signal detail rows)
        tickers_out = [p[0] for p in top]
        details = []
        asof_s = str(
            pd.Timestamp(as_of).date()
            if not getattr(as_of, "tzinfo", None)
            else pd.Timestamp(as_of).tz_convert("America/New_York").date()
        )
        for p in top:
            meta = p[2] if len(p) > 2 else {}
            details.append({"date": asof_s, "ticker": p[0], **meta})
        return tickers_out, details
    return [t for t, _ in top], None


def turnover_cost(
    prev: list[str], new: list[str], cost_per_side_pct: float
) -> float:
    """Fraction of equity paid to move an equal-weight book from ``prev`` to ``new``.

    Entries pay ``cost`` on their new weight (1/len(new)); exits pay ``cost`` on
    their old weight (1/len(prev)). Names held across both legs pay nothing
    (equal-weight drift ignored). ``cost_per_side_pct`` is in percent (0.1 = 10 bps).
    """
    if cost_per_side_pct <= 0:
        return 0.0
    c = cost_per_side_pct / 100.0
    prev_set, new_set = set(prev), set(new)
    entries = len(new_set - prev_set)
    exits = len(prev_set - new_set)
    cost = 0.0
    if new_set and entries:
        cost += c * entries / len(new_set)
    if prev_set and exits:
        cost += c * exits / len(prev_set)
    return cost


def athdip_regime_ok(
    prices: pd.DataFrame,
    vix: pd.Series,
    as_of: pd.Timestamp,
    cfg: ScreenerConfig,
) -> tuple[bool, str]:
    """Pre-chosen regime gates for athdip (both off by default).

    * ``athdip_gate_vix_min``: prior-session VIX close must be ≥ this
      (quanterlab 2026 used 20, fixed from 1990–2005 mean; one-day lag because
      VIX settles after the stock close).
    * ``athdip_gate_spx_mom_days``: SPY return over this many sessions must be > 0
      (Alvarez uses 126d).
    """
    if cfg.athdip_gate_vix_min is not None:
        v = vix.loc[:as_of].dropna()
        if len(v) < 2:
            return False, "vix n/a"
        prior = float(v.iloc[-2])
        if prior < cfg.athdip_gate_vix_min:
            return False, f"VIX prior {prior:.1f} < {cfg.athdip_gate_vix_min:.0f}"
    if cfg.athdip_gate_spx_mom_days:
        spy = _col(prices, "Close", "SPY").loc[:as_of].dropna()
        n = int(cfg.athdip_gate_spx_mom_days)
        if len(spy) <= n:
            return False, "spx n/a"
        mom = float(spy.iloc[-1] / spy.iloc[-1 - n] - 1.0)
        if mom <= 0:
            return False, f"SPX {n}d {mom*100:+.1f}% ≤ 0"
    return True, "ok"


def run_mode_backtest(
    prices: pd.DataFrame,
    vix: pd.Series,
    universe: list[str],
    mode: str,
    cfg: ScreenerConfig,
    year_start: pd.Timestamp,
    top_n: int,
    fund_map: dict[str, FundamentalsSnapshot] | None = None,
    cycle: MarketCycle | None = None,
    bars_4h: pd.DataFrame | None = None,
    cost_per_side_pct: float = 0.0,
) -> BacktestResult:
    spy = _col(prices, "Close", "SPY").dropna()
    trading_days = spy.index[spy.index >= year_start]
    weeks = pd.Series(trading_days).groupby(trading_days.to_period("W")).max()

    effective_top = top_n
    if mode == "value" and cycle is not None:
        effective_top = max(3, int(round(top_n * cycle.top_n_scale)))

    equity = 1.0
    curve: list[tuple[pd.Timestamp, float]] = []
    period_returns: list[float] = []
    holding_counts: list[int] = []
    signal_rows: list[dict] = []
    prev_holdings: list[str] = []
    total_cost = 0.0
    gated_days = 0

    # Athdip: daily rebalance on actual 4h RSI trigger days; others stay weekly
    if mode == "athdip":
        rebalance_dates = list(trading_days)
        period_label = "day"
    else:
        rebalance_dates = list(weeks)
        period_label = "week"

    for i, reb_date in enumerate(rebalance_dates):
        vix_val = float(vix.loc[:reb_date].iloc[-1]) if not vix.loc[:reb_date].empty else None
        market = market_at(vix_val, cfg)
        gate_ok = True
        if mode == "athdip":
            gate_ok, _why = athdip_regime_ok(prices, vix, reb_date, cfg)
            if not gate_ok:
                gated_days += 1
        if gate_ok:
            picked = pick_tickers(
                prices,
                reb_date,
                universe,
                mode,
                cfg,
                market,
                effective_top,
                fund_map=fund_map,
                cycle=cycle if mode == "value" else None,
                bars_4h=bars_4h if mode == "athdip" else None,
            )
            holdings, details = picked
        else:
            holdings, details = [], None
        if details:
            signal_rows.extend(details)
        holding_counts.append(len(holdings))
        # Pay to rebalance from the previous book into this one
        cost = turnover_cost(prev_holdings, holdings, cost_per_side_pct)
        if cost:
            equity *= 1.0 - cost
            total_cost += cost
        prev_holdings = list(holdings)
        if i + 1 >= len(rebalance_dates):
            break
        next_date = rebalance_dates[i + 1]
        if not holdings:
            period_returns.append(0.0)
            curve.append((next_date, equity))
            continue
        rets = []
        for t in holdings:
            s = _col(prices, "Close", t).loc[reb_date:next_date]
            if len(s) < 2:
                continue
            rets.append(float(s.iloc[-1] / s.iloc[0] - 1.0))
        port_ret = float(np.mean(rets)) if rets else 0.0
        period_returns.append(port_ret)
        equity *= 1.0 + port_ret
        curve.append((next_date, equity))
    # Close the final book
    final_cost = turnover_cost(prev_holdings, [], cost_per_side_pct)
    if final_cost:
        equity *= 1.0 - final_cost
        total_cost += final_cost

    curve_s = pd.Series({d: v for d, v in curve}).sort_index()
    spy_ytd = float(spy.loc[year_start:].iloc[-1] / spy.loc[year_start:].iloc[0] - 1.0) * 100.0
    strat_ytd = (equity - 1.0) * 100.0
    dd = 0.0
    if not curve_s.empty:
        peak = curve_s.cummax()
        dd = float(((curve_s / peak) - 1.0).min() * 100.0)
    wins = sum(1 for r in period_returns if r > 0)
    win_rate = (wins / len(period_returns) * 100.0) if period_returns else 0.0
    avg_h = float(np.mean(holding_counts)) if holding_counts else 0.0
    _ = period_label  # documented in caller print
    return BacktestResult(
        mode=mode,
        ytd_return_pct=round(strat_ytd, 2),
        spy_return_pct=round(spy_ytd, 2),
        excess_pct=round(strat_ytd - spy_ytd, 2),
        rebalance_count=len(period_returns),
        avg_holdings=round(avg_h, 1),
        win_rate_pct=round(win_rate, 1),
        max_drawdown_pct=round(dd, 2),
        signal_rows=signal_rows or None,
        cost_per_side_pct=cost_per_side_pct,
        total_cost_pct=round(total_cost * 100.0, 2),
        gated_days=gated_days,
    )

def print_sketch(cycle: MarketCycle) -> None:
    print(
        f"""
=== VALUE MODE SKETCH v2 (backtest-only; live CLI unchanged) ===

Stock gates
  • ROE ≥ 15%
  • Cheap: val≥55 OR (PE≤20 & PB<3) OR PEG≤1.5
  • FCF yield > 0 when available
  • Skip weak BS traps; RSI < 75; soft +2 if RSI ≤ 50

Ranking
  • 35% multiples | 15% PEG | 15% FCF yield
  • 15% margins/profit | 10% capital | 10% balance

Market-cycle overlay (current)
  • {cycle.note}
  • min value score={cycle.min_value_score:.0f} · top_n scale={cycle.top_n_scale:.2f}

=== TRUMP MODE SKETCH (backtest-only proxy) ===
  • Universe = Trump lexicon tickers (companies + policy theme proxies)
  • Weekly top-N by 10d return × volume ratio; skip RSI≥80
  • NOT driven by historical Truth/X posts (RSS only covers ~days)
  • Treat as theme-momentum proxy, not live tracker fidelity

=== ATHDIP MODE SKETCH ===
  • Setup: multi-year ATH break (21d fresh high at ATH time), watch ≤63 sessions
  • Trigger: fire on the actual calendar day the lookback-min 4h RSI prints (≤ max)
  • Daily rebalance (not Friday-only); buy at trigger-day close
  • Rank by oversold depth + ATH proximity + ATH freshness

Caveats
  • Yahoo fundamentals CURRENT (look-ahead)
  • CAPE/Buffett are spot readings, not full historical series
  • Profit margins still snapshot (not multi-year trend yet)
  • Athdip 4h history capped by Yahoo (~730d); older weeks may lack bars
"""
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest support / catalyst / value / trump / athdip"
    )
    parser.add_argument("--universe", default="sp500", choices=("sp500", "us"))
    parser.add_argument(
        "--limit",
        type=int,
        default=80,
        help="Cap universe size (0 = all constituents)",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--year", type=int, default=None, help="Calendar year start (Jan 1)")
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help="Trailing N months ending today (e.g. 12 for 1 year)",
    )
    parser.add_argument(
        "--modes",
        default="support,catalyst,value,trump,athdip",
        help="Comma-separated: support,catalyst,value,trump,athdip",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=0.0,
        help="Transaction cost per side in percent (0.1 = 10 bps each way); default 0",
    )
    args = parser.parse_args()

    cycle = assess_market_cycle()
    print_sketch(cycle)

    cfg = ScreenerConfig(
        universe=args.universe,
        max_tickers=None if args.limit == 0 else args.limit,
        require_above_sma_slow=False,
        enable_reddit=False,
        enable_grok=False,
        enable_fundamentals=False,
        enable_politicians=False,
        enable_trump=False,
    )
    base_universe = resolve_universe(
        limit=None if args.limit == 0 else args.limit,
        universe=args.universe,
    )
    trump_universe = all_tracked_tickers()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    # Download union so trump proxies (ETFs etc.) are available even if outside S&P slice
    download_tickers = list(base_universe)
    if "trump" in modes:
        seen = set(download_tickers)
        for t in trump_universe:
            if t not in seen:
                seen.add(t)
                download_tickers.append(t)

    today = datetime.now(timezone.utc).date()
    if args.months is not None:
        period_start = pd.Timestamp(today) - pd.DateOffset(months=args.months)
        period_label = f"trailing {args.months}m ({period_start.date()} → {today})"
    else:
        year = args.year or today.year
        period_start = pd.Timestamp(f"{year}-01-01")
        period_label = f"YTD {year} ({period_start.date()} → {today})"

    warmup_days = 400
    if "athdip" in modes:
        warmup_days = max(warmup_days, 1825)
    warmup_start = (period_start - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"Downloading {len(download_tickers)} tickers + SPY + VIX ({warmup_start} → {end})…")
    prices = download_panel(download_tickers, warmup_start, end)
    vix = _col(prices, "Close", "^VIX")

    bars_4h: pd.DataFrame | None = None
    if "athdip" in modes:
        print(
            f"Downloading 4h bars for athdip ({len(base_universe)} tickers, "
            f"period={cfg.athdip_intraday_period})…"
        )
        bars_4h = download_intraday(
            base_universe,
            interval="4h",
            period=cfg.athdip_intraday_period,
            chunk_size=min(40, cfg.price_chunk_size),
        )
        print(f"4h panel columns: {0 if bars_4h is None or bars_4h.empty else bars_4h.shape[1]}")

    need_funds = "value" in modes
    fund_map: dict[str, FundamentalsSnapshot] = {}
    if need_funds:
        print(f"Fetching Yahoo fundamentals for value sketch ({len(base_universe)} tickers)…")
        fund_map = fetch_fund_map(base_universe)
        print(f"Fundamentals snapshots: {len(fund_map)}")

    print(f"\n=== BACKTEST — {period_label} ===")
    if "athdip" in modes and len(modes) == 1:
        print(
            f"Universe: {args.universe} (limit {args.limit}) · "
            f"daily actual-trigger · top {args.top} EW"
        )
    else:
        print(
            f"Universe: {args.universe} (limit {args.limit}) · weekly · top {args.top} EW"
        )
    if "trump" in modes:
        print(f"Trump proxy universe: {len(trump_universe)} lexicon tickers")
    if "athdip" in modes:
        print(
            "Athdip: signal only on actual 4h RSI trigger day "
            f"(lookback {cfg.athdip_rsi_lookback_days}d, RSI≤{cfg.athdip_rsi_4h_max:.0f}; "
            f"ATH age ≤{cfg.athdip_max_days_since_ath}d)"
        )
    print(f"Modes: {', '.join(modes)}\n")

    results: list[BacktestResult] = []
    for mode in modes:
        uni = trump_universe if mode == "trump" else base_universe
        res = run_mode_backtest(
            prices,
            vix,
            uni,
            mode,
            cfg,
            period_start,
            args.top,
            fund_map=fund_map if mode == "value" else None,
            cycle=cycle if mode == "value" else None,
            bars_4h=bars_4h if mode == "athdip" else None,
            cost_per_side_pct=args.cost,
        )
        results.append(res)
        if mode == "athdip" and res.signal_rows:
            out_path = Path("output/athdip_backtest_signals.csv")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sig_df = pd.DataFrame(res.signal_rows)
            # Prefer trigger_date as the signal date column
            if "trigger_date" in sig_df.columns:
                sig_df = sig_df.rename(columns={"date": "asof_date"})
            sig_df.to_csv(out_path, index=False)
            print(f"Athdip signals ({len(sig_df)} rows) → {out_path}")
            # Show latest month sample
            if "trigger_date" in sig_df.columns:
                latest = sig_df.sort_values("trigger_date")
                print("\nRecent athdip trigger days (last 15 signals):")
                cols = [
                    c
                    for c in (
                        "trigger_date",
                        "ticker",
                        "price",
                        "rsi_4h",
                        "pct_from_ath",
                        "days_since_ath",
                        "score",
                    )
                    if c in latest.columns
                ]
                print(latest.tail(15)[cols].to_string(index=False))

    win_hdr = "Win%" if any(m == "athdip" for m in modes) else "WinWk%"
    header = (
        f"{'Mode':10} {'Return%':>8} {'SPY%':>8} {'Excess':>8} "
        f"{win_hdr:>8} {'MaxDD%':>8} {'Avg#':>6} {'Cost%':>7}"
    )
    print(f"\nCost per side: {args.cost:.3f}%")
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.mode:10} {r.ytd_return_pct:+8.2f} {r.spy_return_pct:+8.2f} "
            f"{r.excess_pct:+8.2f} {r.win_rate_pct:8.1f} {r.max_drawdown_pct:8.2f} "
            f"{r.avg_holdings:6.1f} {r.total_cost_pct:7.2f}"
        )

    if results:
        best = max(results, key=lambda x: x.excess_pct)
        print(f"\nBest excess vs SPY (this run): {best.mode} ({best.excess_pct:+.2f}%)")
    print(
        "Live screener unchanged — value / trump / athdip backtest sketches disclosed above."
    )


if __name__ == "__main__":
    main()
