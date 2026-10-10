#!/usr/bin/env python3
"""YTD athdip: production (no SMA gate) vs require above SMA50+SMA200."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

from stock_screener.config import ScreenerConfig  # noqa: E402
from stock_screener.data.universe import resolve_universe  # noqa: E402

import backtest_ytd as bt  # noqa: E402


def main() -> int:
    limit = 80
    top_n = 10
    today = datetime.now(timezone.utc).date()
    year_start = pd.Timestamp(f"{today.year}-01-01")
    warmup_start = (year_start - pd.Timedelta(days=1825)).strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    universe = resolve_universe(limit=limit, universe="sp500")
    print(f"Universe sp500 limit={limit} · YTD {year_start.date()} → {today}")
    print(f"Downloading daily + 4h…")
    prices = bt.download_panel(universe, warmup_start, end)
    vix = bt._col(prices, "Close", "^VIX")
    bars_4h = bt.download_intraday(
        universe, interval="4h", period="730d", chunk_size=20
    )

    variants = [
        (
            "prod_no_sma",
            ScreenerConfig(
                athdip_require_uptrend=False,
                athdip_require_sma50=False,
                require_above_sma_slow=False,
            ),
            "Production default (no SMA gate)",
        ),
        (
            "sma50_200",
            ScreenerConfig(
                athdip_require_uptrend=True,
                athdip_require_sma50=True,
                require_above_sma_slow=False,
            ),
            "Require close > SMA50 & SMA200",
        ),
    ]

    rows = []
    for key, cfg, label in variants:
        print(f"\n=== {label} ===")
        res = bt.run_mode_backtest(
            prices,
            vix,
            universe,
            "athdip",
            cfg,
            year_start,
            top_n,
            bars_4h=bars_4h,
        )
        n_sig = len(res.signal_rows or [])
        print(
            f"  return={res.ytd_return_pct:+.2f}%  excess={res.excess_pct:+.2f}%  "
            f"win={res.win_rate_pct:.1f}%  maxDD={res.max_drawdown_pct:.2f}%  "
            f"avg#={res.avg_holdings:.1f}  signals={n_sig}"
        )
        out = Path("output") / f"athdip_backtest_signals_{key}.csv"
        if res.signal_rows:
            pd.DataFrame(res.signal_rows).to_csv(out, index=False)
            print(f"  wrote {out}")
        rows.append(
            {
                "variant": key,
                "label": label,
                "return_pct": round(res.ytd_return_pct, 2),
                "spy_pct": round(res.spy_return_pct, 2),
                "excess_pct": round(res.excess_pct, 2),
                "win_pct": round(res.win_rate_pct, 1),
                "max_dd_pct": round(res.max_drawdown_pct, 2),
                "avg_holdings": round(res.avg_holdings, 2),
                "n_signals": n_sig,
            }
        )

    summary = pd.DataFrame(rows)
    path = Path("output/athdip_sma_compare_ytd.csv")
    summary.to_csv(path, index=False)
    print("\n=== COMPARE (YTD athdip) ===")
    print(summary.to_string(index=False))
    print(f"\nWrote {path}")

    a, b = rows[0], rows[1]
    print(
        f"\nDelta (prod − SMA-gate): return {a['return_pct'] - b['return_pct']:+.2f}pp, "
        f"excess {a['excess_pct'] - b['excess_pct']:+.2f}pp, "
        f"signals {a['n_signals'] - b['n_signals']:+d}"
    )
    better = (
        "Production (no SMA)"
        if a["excess_pct"] >= b["excess_pct"]
        else "SMA50+200 gate"
    )
    print(f"Better excess vs SPY this YTD: {better}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
