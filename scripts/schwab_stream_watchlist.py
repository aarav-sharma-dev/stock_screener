#!/usr/bin/env python3
"""Watchlist quotes: prefer schwab-py L1 stream (Py≥3.10); else poll REST.

Examples::

  python3 scripts/schwab_stream_watchlist.py --from-focus --seconds 90
  .venv/bin/python scripts/schwab_stream_watchlist.py --tickers AAPL,MSFT --poll 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

sys.path.insert(0, str(ROOT / "src"))

from stock_screener.data.schwab import (  # noqa: E402
    SchwabNotConfiguredError,
    get_client,
    quotes,
    schwab_env,
)


def _focus_tickers() -> list[str]:
    path = ROOT / "output" / "today_focus.csv"
    if not path.exists():
        return []
    try:
        import pandas as pd

        df = pd.read_csv(path)
        if "ticker" not in df.columns:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for t in df["ticker"].astype(str):
            t = t.strip().upper()
            if t and t != "NAN" and t not in seen:
                seen.add(t)
                out.append(t)
        return out
    except Exception:  # noqa: BLE001
        return []


def _print_quote_snapshot(payload: dict) -> None:
    for sym, data in sorted(payload.items()):
        q = data.get("quote") or data
        last = q.get("lastPrice") or q.get("mark")
        bid = q.get("bidPrice")
        ask = q.get("askPrice")
        print(f"{sym:6} last={last}  bid={bid}  ask={ask}", flush=True)


def poll_watchlist(symbols: list[str], *, seconds: float, interval: float) -> None:
    client = get_client(interactive=False)
    deadline = time.monotonic() + seconds
    print(
        f"Polling REST quotes every {interval:.0f}s for {len(symbols)} symbols "
        f"· {seconds:.0f}s total",
        flush=True,
    )
    while time.monotonic() < deadline:
        _print_quote_snapshot(quotes(symbols, client=client))
        print("---", flush=True)
        time.sleep(max(1.0, interval))


async def stream_schwab_py(symbols: list[str], *, seconds: float) -> None:
    """Optional WebSocket L1 via schwab-py (requires Python ≥3.10 + package)."""
    from schwab.auth import client_from_token_file  # type: ignore
    from schwab.streaming import StreamClient  # type: ignore

    cfg = schwab_env()
    if not cfg["account_id"]:
        raise SchwabNotConfiguredError("Set SCHWAB_ACCOUNT_ID for schwab-py streamer")
    http = client_from_token_file(
        api_key=cfg["api_key"],
        app_secret=cfg["app_secret"],
        token_path=cfg["token_path"],
    )
    acct = cfg["account_id"]
    try:
        acct_id: int | str = int(acct)
    except ValueError:
        acct_id = acct
    stream = StreamClient(http, account_id=acct_id)

    def handler(msg):
        print(json.dumps(msg, default=str)[:500], flush=True)

    await stream.login()
    stream.add_level_one_equity_handler(handler)
    await stream.level_one_equity_subs(symbols)
    print(f"Streaming L1 (schwab-py) · {seconds:.0f}s …", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await stream.handle_message()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickers", default="", help="Comma-separated list")
    p.add_argument("--from-focus", action="store_true")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--poll", type=float, default=5.0, help="REST poll interval (seconds)")
    p.add_argument(
        "--websocket",
        action="store_true",
        help="Force schwab-py WebSocket (Py≥3.10); default polls REST",
    )
    args = p.parse_args(argv)

    symbols: list[str] = []
    if args.tickers.strip():
        symbols.extend(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if args.from_focus or not symbols:
        symbols.extend(t for t in _focus_tickers() if t not in symbols)
    if not symbols:
        print("No symbols. Pass --tickers or build today_focus.csv.", file=sys.stderr)
        return 1

    try:
        if args.websocket:
            asyncio.run(stream_schwab_py(symbols, seconds=args.seconds))
        else:
            poll_watchlist(symbols, seconds=args.seconds, interval=args.poll)
    except SchwabNotConfiguredError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(
            f"schwab-py unavailable ({exc}). Use default REST polling "
            "(omit --websocket) or install schwab-py on Python ≥3.10.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Watchlist error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
