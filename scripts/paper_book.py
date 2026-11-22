#!/usr/bin/env python3
"""Forward paper book for the tested athdip exits.

Two main books share one entry rule and differ only on the exit:

  entry   an athdip name whose 4h RSI low printed on that session
          (``trigger_date`` equals the session), at that session's daily close
  hold10  sell the close 10 trading sessions later
  rsi50   sell the first later close where daily RSI(14) > 50
  both    also sell at the 20-session close

Two parallel books use the same exits with one extra entry gate (``*_low20``):
the trigger-day close must be the lowest close of the last 20 sessions. That
gate came from ``experiment_athdip_entry_timing.py`` (in-sample, 8 entries x 2
exits). Treat it as a forward test, not a proven rule.

The screen lists a name for 5 days after its trigger. Only the trigger day
enters, like the 2026 test; later days on the list do not. A name that prints
a new, lower RSI on a later day triggers again.

Sizing matches the 2026 test: at most 10 names, one slot each. Round-trip
cost is 10 bps per side. This does not place orders.

The 19:00 CT run opens new names. A morning run only updates exits, because
the cash close is already recorded. ``paper_book_backfill.py`` replays past
sessions with the same rules.

State: ``output/paper_book.json``. Flat log: ``output/paper_book.csv``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

OUTPUT = ROOT / "output"
STATE_PATH = OUTPUT / "paper_book.json"
TRADES_PATH = OUTPUT / "paper_book.csv"
SCREEN_PATH = OUTPUT / "screen_athdip.csv"

BOOKS = ("hold10", "rsi50", "hold10_low20", "rsi50_low20")
EXIT_RULE = {
    "hold10": "hold10",
    "rsi50": "rsi50",
    "hold10_low20": "hold10",
    "rsi50_low20": "rsi50",
}
LOW20_BOOKS = frozenset({"hold10_low20", "rsi50_low20"})
LOW_WINDOW = 20
MAX_POSITIONS = 10
TIME_STOP = 20
HOLD_DAYS = 10
COST_PER_SIDE = 0.001  # 10 bps
CHICAGO = ZoneInfo("America/Chicago")
NY = ZoneInfo("America/New_York")


def exit_reason(rule: str, held: int, rsi: float | None, *, time_stop: int = TIME_STOP) -> str | None:
    """Reason to sell at this close, or None to keep the position."""
    if held >= time_stop:
        return "time20"
    if rule == "hold10" and held >= HOLD_DAYS:
        return "hold10"
    if rule == "rsi50" and held >= 1 and rsi is not None and rsi > 50.0:
        return "rsi50"
    return None


def net_return(entry_px: float, exit_px: float, cost: float = COST_PER_SIDE) -> float:
    return (exit_px * (1.0 - cost)) / (entry_px * (1.0 + cost)) - 1.0


def held_sessions(calendar: list[str], entry: str, day: str) -> int | None:
    if entry not in calendar or day not in calendar:
        return None
    return calendar.index(day) - calendar.index(entry)


def new_state() -> dict:
    return {
        "version": 2,
        "max_positions": MAX_POSITIONS,
        "cost_per_side": COST_PER_SIDE,
        "last_entry_session": None,
        "books": {name: {"open": [], "closed": []} for name in BOOKS},
    }


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.is_file():
        return new_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    for name in BOOKS:
        data.setdefault("books", {}).setdefault(name, {"open": [], "closed": []})
    # Version 1 held back names already on the screen. The tested rule does not.
    data.pop("seeded", None)
    data.pop("ineligible", None)
    return data


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def ranked_signals(rows: list[dict], session: str | None = None) -> list[dict]:
    """Rows sorted the way the backtest fills slots: score, then ticker.

    With ``session`` set, keep only names whose trigger day is that session.
    """
    cleaned = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker == "NAN":
            continue
        trigger = str(row.get("trigger_date") or "").strip()[:10]
        if session is not None and trigger != session:
            continue
        try:
            score = float(row.get("composite") or row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        cleaned.append({"ticker": ticker, "score": score, "trigger_date": trigger})
    cleaned.sort(key=lambda r: (-r["score"], r["ticker"]))
    # One row per ticker
    seen: set[str] = set()
    out = []
    for row in cleaned:
        if row["ticker"] in seen:
            continue
        seen.add(row["ticker"])
        out.append(row)
    return out


def load_screen(path: Path = SCREEN_PATH) -> list[dict]:
    """Every row on the athdip screen, trigger day included."""
    import pandas as pd

    if not path.is_file():
        return []
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        return []
    return ranked_signals(df.to_dict(orient="records"))


def chicago_file_day(path: Path) -> str | None:
    if not path.is_file():
        return None
    ts = datetime.fromtimestamp(path.stat().st_mtime, CHICAGO)
    return ts.date().isoformat()


def completed_session(calendar: list[str], now_ct: datetime) -> str | None:
    """Last cash session whose daily close is in. Before 15:05 CT, that is yesterday."""
    if not calendar:
        return None
    today = now_ct.date().isoformat()
    after_close = now_ct.hour > 15 or (now_ct.hour == 15 and now_ct.minute >= 5)
    if after_close:
        eligible = [d for d in calendar if d <= today]
    else:
        eligible = [d for d in calendar if d < today]
    return eligible[-1] if eligible else None


def first_exit(
    rule: str,
    calendar: list[str],
    entry: str,
    up_to: str,
    closes: dict[str, float],
    rsi: dict[str, float],
) -> dict | None:
    """First close after entry, through ``up_to``, where the rule sells."""
    if entry not in calendar or up_to not in calendar:
        return None
    start = calendar.index(entry)
    end = calendar.index(up_to)
    for i in range(start + 1, end + 1):
        day = calendar[i]
        px = closes.get(day)
        if px is None or px <= 0:
            continue
        reason = exit_reason(rule, i - start, rsi.get(day))
        if reason:
            return {"exit_date": day, "exit_px": px, "held": i - start, "reason": reason}
    return None


def is_low20(
    ticker: str, session: str, calendar: list[str], bars: dict[str, dict[str, float]]
) -> bool:
    """True when the session's close is the lowest of the last 20 sessions.

    Same test as ``experiment_athdip_entry_timing.py`` (``filter_20d_low``): the
    window is the SPY calendar, missing closes are skipped, and the window
    needs 20 sessions of history.
    """
    if session not in calendar:
        return False
    i = calendar.index(session)
    if i < LOW_WINDOW - 1:
        return False
    days = bars.get(ticker, {})
    px = days.get(session)
    if px is None or px <= 0:
        return False
    window = [days[d] for d in calendar[i - LOW_WINDOW + 1 : i + 1] if d in days]
    return min(window) == px


def _close_trade(pos: dict, exit_info: dict, cost: float) -> dict:
    closed = dict(pos)
    closed.update(exit_info)
    closed["gross_ret"] = closed["exit_px"] / closed["entry_px"] - 1.0
    closed["net_ret"] = net_return(closed["entry_px"], closed["exit_px"], cost)
    return closed


def mark_exits(
    book_name: str,
    positions: list[dict],
    calendar: list[str],
    up_to: str,
    bars: dict[str, dict[str, float]],
    rsi_by_ticker: dict[str, dict[str, float]],
    cost: float,
) -> tuple[list[dict], list[dict]]:
    still: list[dict] = []
    closed: list[dict] = []
    for pos in positions:
        info = first_exit(
            EXIT_RULE[book_name],
            calendar,
            pos["entry_date"],
            up_to,
            bars.get(pos["ticker"], {}),
            rsi_by_ticker.get(pos["ticker"], {}),
        )
        if info is None:
            still.append(pos)
        else:
            closed.append(_close_trade(pos, info, cost))
    return still, closed


def select_entries(
    ranked: list[dict],
    *,
    open_tickers: set[str],
    session: str,
    closes_on_day: dict[str, float],
    slots: int,
) -> list[dict]:
    picked: list[dict] = []
    for row in ranked:
        if len(picked) >= slots:
            break
        ticker = row["ticker"]
        if ticker in open_tickers:
            continue
        px = closes_on_day.get(ticker)
        if px is None or px <= 0:
            continue
        picked.append(
            {
                "ticker": ticker,
                "entry_date": session,
                "entry_px": px,
                "score": row["score"],
            }
        )
    return picked


def advance(
    state: dict,
    *,
    session: str,
    ranked: list[dict],
    allow_entries: bool,
    calendar: list[str],
    bars: dict[str, dict[str, float]],
    rsi_by_ticker: dict[str, dict[str, float]],
) -> list[str]:
    """Update both books through ``session``. Returns human-readable notes.

    Exits always run. Entries run only when ``allow_entries`` is true and this
    session has not already opened trades. ``ranked`` must already be limited
    to names that triggered on ``session``.
    """
    notes: list[str] = []
    cost = float(state.get("cost_per_side", COST_PER_SIDE))

    for name in BOOKS:
        book = state["books"][name]
        still, newly_closed = mark_exits(
            name, book["open"], calendar, session, bars, rsi_by_ticker, cost
        )
        book["open"] = still
        book["closed"].extend(newly_closed)
        for trade in newly_closed:
            notes.append(
                f"exit {name} {trade['ticker']} {trade['exit_date']} "
                f"{trade['reason']} held {trade['held']} "
                f"net {trade['net_ret'] * 100:.2f}%"
            )

    if not allow_entries:
        notes.append(f"exits marked through {session}; entries skipped")
        return notes

    if state.get("last_entry_session") == session:
        notes.append(f"entries already applied for {session}")
        return notes

    for name in BOOKS:
        book = state["books"][name]
        slots = int(state.get("max_positions", MAX_POSITIONS)) - len(book["open"])
        opens = {p["ticker"] for p in book["open"]}
        candidates = ranked
        if name in LOW20_BOOKS:
            candidates = [
                r for r in ranked if is_low20(r["ticker"], session, calendar, bars)
            ]
        closes_on_day = {t: days.get(session) for t, days in bars.items()}
        picked = select_entries(
            candidates,
            open_tickers=opens,
            session=session,
            closes_on_day=closes_on_day,
            slots=max(0, slots),
        )
        book["open"].extend(picked)
        for pos in picked:
            notes.append(
                f"entry {name} {pos['ticker']} {session} @ {pos['entry_px']:.2f}"
            )
        if slots <= 0:
            skipped = [r["ticker"] for r in candidates if r["ticker"] not in opens]
            if skipped:
                notes.append(f"{name} is full; skipped {', '.join(skipped[:5])}")

    state["last_entry_session"] = session
    return notes


def rsi_table(closes: dict[str, float]) -> dict[str, float]:
    """Wilder RSI(14) keyed by session date. Same series the backtest uses."""
    import pandas as pd

    from stock_screener.features import compute_rsi_series

    if len(closes) < 16:
        return {}
    dates = sorted(closes)
    series = pd.Series([closes[d] for d in dates], index=pd.to_datetime(dates), dtype=float)
    rsi = compute_rsi_series(series, 14)
    out: dict[str, float] = {}
    for ts, value in rsi.dropna().items():
        out[pd.Timestamp(ts).strftime("%Y-%m-%d")] = float(value)
    return out


def flatten_trades(state: dict, bars: dict[str, dict[str, float]]) -> list[dict]:
    rows: list[dict] = []
    for name in BOOKS:
        book = state["books"][name]
        for pos in book["closed"]:
            rows.append(
                {
                    "book": name,
                    "status": "closed",
                    "ticker": pos["ticker"],
                    "entry_date": pos["entry_date"],
                    "entry_px": round(pos["entry_px"], 4),
                    "exit_date": pos["exit_date"],
                    "exit_px": round(pos["exit_px"], 4),
                    "held": pos["held"],
                    "reason": pos["reason"],
                    "gross_pct": round(pos["gross_ret"] * 100.0, 2),
                    "net_pct": round(pos["net_ret"] * 100.0, 2),
                    "last_px": "",
                    "mtm_net_pct": "",
                }
            )
        for pos in book["open"]:
            days = bars.get(pos["ticker"], {})
            last_day = max(days) if days else ""
            last_px = days.get(last_day) if last_day else None
            mtm = ""
            if last_px:
                mtm = round(net_return(pos["entry_px"], last_px) * 100.0, 2)
            rows.append(
                {
                    "book": name,
                    "status": "open",
                    "ticker": pos["ticker"],
                    "entry_date": pos["entry_date"],
                    "entry_px": round(pos["entry_px"], 4),
                    "exit_date": "",
                    "exit_px": "",
                    "held": held_sessions(
                        sorted(days), pos["entry_date"], last_day
                    )
                    if last_day
                    else "",
                    "reason": "",
                    "gross_pct": "",
                    "net_pct": "",
                    "last_px": round(last_px, 4) if last_px else "",
                    "mtm_net_pct": mtm,
                }
            )
    return rows


def write_trades(rows: list[dict], path: Path = TRADES_PATH) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def format_report(state: dict, session: str | None, notes: list[str]) -> str:
    lines = [f"Paper book through {session or 'n/a'} · 10 bps/side · 10 slots · no orders"]
    for name in BOOKS:
        book = state["books"][name]
        closed = book["closed"]
        nets = [t["net_ret"] for t in closed]
        wins = sum(1 for v in nets if v > 0)
        avg = (sum(nets) / len(nets) * 100.0) if nets else 0.0
        win = (wins / len(nets) * 100.0) if nets else 0.0
        lines.append(
            f"{name}: open {len(book['open'])}  closed {len(closed)}  "
            f"avg net {avg:.2f}%  win {win:.0f}%"
        )
        for pos in book["open"]:
            lines.append(
                f"  open {pos['ticker']} {pos['entry_date']} @ {pos['entry_px']:.2f}"
            )
        for pos in closed[-5:]:
            lines.append(
                f"  closed {pos['ticker']} {pos['entry_date']} → {pos['exit_date']} "
                f"{pos['reason']} net {pos['net_ret'] * 100:.2f}%"
            )
    lines.extend(notes)
    return "\n".join(lines)


def bars_from_frames(frames: dict) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Turn Schwab daily frames into a SPY calendar and close maps."""
    import pandas as pd

    mapped: dict[str, dict[str, float]] = {}
    for ticker, frame in frames.items():
        if frame is None or frame.empty or "Close" not in frame.columns:
            continue
        idx = pd.to_datetime(frame.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(NY).tz_localize(None)
        days = idx.normalize()
        series = pd.Series(frame["Close"].to_numpy(), index=days)
        series = series[~series.index.duplicated(keep="last")].sort_index()
        mapped[ticker] = {ts.strftime("%Y-%m-%d"): float(px) for ts, px in series.items() if px > 0}
    calendar = sorted(mapped.get("SPY", {}))
    return calendar, mapped


def fetch_frames(tickers: list[str]):
    from stock_screener.data.schwab import get_client, price_history_daily

    client = get_client(interactive=False)
    frames = {}
    for ticker in tickers:
        frames[ticker] = price_history_daily(ticker, client=client)
    return frames


def fetch_yahoo_frames(tickers: list[str], years: int = 2):
    """Yahoo daily closes, dividend-adjusted, shaped like the Schwab frames."""
    import pandas as pd
    import yfinance as yf

    start = (datetime.now(CHICAGO) - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    raw = yf.download(
        tickers=list(tickers),
        start=start,
        auto_adjust=True,
        group_by="column",
        threads=True,
        progress=False,
    )
    frames = {}
    for ticker in tickers:
        if raw is None or raw.empty or ("Close", ticker) not in raw.columns:
            frames[ticker] = None
            continue
        close = raw[("Close", ticker)].dropna()
        if close.empty:
            frames[ticker] = None
            continue
        frames[ticker] = pd.DataFrame(
            {"Close": close.to_numpy()}, index=pd.to_datetime(close.index)
        )
    return frames


def load_prices(tickers: list[str]) -> tuple[dict, str | None]:
    """Schwab daily frames, or Yahoo if Schwab fails.

    Returns (frames, note). ``note`` is None when Schwab worked; otherwise it
    says Schwab failed and which source was used. Raises only if both fail.
    """
    try:
        return fetch_frames(tickers), None
    except Exception as exc:  # noqa: BLE001 — fall back, then report
        schwab_error = exc.__class__.__name__
    try:
        frames = fetch_yahoo_frames(tickers)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Schwab failed ({schwab_error}) and Yahoo failed ({exc.__class__.__name__})"
        ) from exc
    return frames, f"Schwab daily history failed ({schwab_error}); used Yahoo daily closes."


def run(now_ct: datetime | None = None, *, screen_path: Path = SCREEN_PATH) -> int:
    now_ct = now_ct or datetime.now(CHICAGO)
    state = load_state()
    screen = load_screen(screen_path)
    needed = {"SPY"}
    for name in BOOKS:
        needed.update(p["ticker"] for p in state["books"][name]["open"])
    needed.update(r["ticker"] for r in screen)
    try:
        frames, price_note = load_prices(sorted(needed))
    except RuntimeError as exc:
        print(f"Paper book: no daily prices ({exc}). State not changed.")
        return 1
    calendar, bars = bars_from_frames(frames)
    session = completed_session(calendar, now_ct)
    if session is None:
        print("Paper book: no completed daily session.")
        return 1
    screen_day = chicago_file_day(screen_path)
    allow_entries = bool(screen) and screen_day == session
    rsi_by_ticker = {t: rsi_table(days) for t, days in bars.items() if t != "SPY"}
    triggered = [r for r in screen if r.get("trigger_date") == session]
    notes = advance(
        state,
        session=session,
        ranked=triggered if allow_entries else [],
        allow_entries=allow_entries,
        calendar=calendar,
        bars=bars,
        rsi_by_ticker=rsi_by_ticker,
    )
    if price_note:
        notes.insert(0, price_note)
    if not allow_entries:
        if not screen:
            notes.append("no screen_athdip.csv")
        else:
            notes.append(
                f"screen file is {screen_day}, completed session is {session}"
            )
    elif not triggered:
        notes.append(f"no athdip name triggered on {session}")
    save_state(state)
    rows = flatten_trades(state, bars)
    write_trades(rows)
    print(format_report(state, session, notes))
    _publish_canvas()
    return 0


def _publish_canvas() -> None:
    """Refresh the Slack canvas. Any failure is reported and ignored."""
    try:
        import paper_book_canvas

        state = load_state()
        markdown = paper_book_canvas.build_markdown(
            state, paper_book_canvas.last_prices(), state.get("last_entry_session") or "n/a"
        )
        ok, msg = paper_book_canvas.publish(markdown)
        print(f"Paper book canvas: {msg}" if ok else f"Paper book canvas skipped: {msg}")
    except Exception as exc:  # noqa: BLE001 — the book is already saved
        print(f"Paper book canvas skipped: {exc.__class__.__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    reexec_venv_if_needed(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Print the saved book and exit")
    args = parser.parse_args(argv)
    if args.status:
        state = load_state()
        print(format_report(state, state.get("last_entry_session"), []))
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
