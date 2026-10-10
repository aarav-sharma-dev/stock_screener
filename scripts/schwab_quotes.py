#!/usr/bin/env python3
"""Fetch Schwab quotes and/or daily / RTH-4h history for symbols.

Examples::

  python3 scripts/schwab_quotes.py AAPL MSFT
  .venv/bin/python scripts/schwab_quotes.py --history-4h NVDA --days 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from stock_screener.data.schwab import (  # noqa: E402
    SchwabNotConfiguredError,
    get_client,
    price_history_daily,
    price_history_rth_4h,
    quotes,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbols", nargs="+", help="Tickers")
    p.add_argument("--history-daily", action="store_true")
    p.add_argument("--history-4h", action="store_true", help="RTH 4h from 30m bars")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        client = get_client(interactive=False)
    except SchwabNotConfiguredError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    syms = [s.upper() for s in args.symbols]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    if args.history_daily or args.history_4h:
        for sym in syms:
            if args.history_daily:
                df = price_history_daily(sym, start=start, end=end, client=client)
                print(f"\n=== {sym} daily ({len(df)} bars) ===")
                print(df.tail(10).to_string() if not df.empty else "(empty)")
            if args.history_4h:
                df4 = price_history_rth_4h(sym, start=start, end=end, client=client)
                print(f"\n=== {sym} RTH 4h ({len(df4)} bars) ===")
                print(df4.tail(10).to_string() if not df4.empty else "(empty)")
        return 0

    q = quotes(syms, client=client)
    if args.json:
        print(json.dumps(q, indent=2, default=str))
        return 0
    rows = []
    for sym in syms:
        data = q.get(sym) or {}
        qq = data.get("quote") or data
        rows.append(
            {
                "symbol": sym,
                "last": qq.get("lastPrice") or qq.get("mark"),
                "bid": qq.get("bidPrice"),
                "ask": qq.get("askPrice"),
                "net": qq.get("netChange"),
                "pct": qq.get("netPercentChangeInDouble")
                or qq.get("netPercentChange"),
                "vol": qq.get("totalVolume"),
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
