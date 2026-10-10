"""Post-earnings-announcement drift (PEAD) data helpers.

Shared by the production ``pead`` screener mode and ``scripts/backtest_pead.py``.

Definitions (Quantpedia / Brandt et al. style):
  * **Reaction session R** — the first regular session that fully reflects the
    print. Reports time-stamped ≥ 16:00 ET map to the *next* session, otherwise
    the same session. Unknown time (midnight) is treated as BMO.
  * **EAR (earnings-announcement return)** — close[R-1] → close[R+1] return
    minus the same-window SPY return. Captures the market reaction regardless of
    whether the print was BMO/AMC.
  * **Surprise %** — yfinance ``Surprise(%)`` when present, else
    ``(actual − estimate) / |estimate| × 100``.
  * **Entry** — close of session R+1 (the first close at which EAR is known).

Only free data (yfinance ``Ticker.get_earnings_dates``) is used; estimates are
Yahoo consensus, which is noisier than I/B/E/S. Treat SUE here as a proxy.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class EarningsEvent:
    ticker: str
    event_ts: pd.Timestamp  # raw yfinance timestamp (tz-aware, America/New_York)
    eps_estimate: float | None
    eps_actual: float | None
    surprise_pct: float | None

    @property
    def after_close(self) -> bool:
        return bool(self.event_ts.hour >= 16)


def _to_ny(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize("America/New_York")
    return ts.tz_convert("America/New_York")


def fetch_earnings_events(ticker: str, limit: int = 12) -> list[EarningsEvent]:
    """Reported (actual EPS present) earnings events, newest first."""
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings_dates failed for %s: %s", ticker, exc)
        return []
    if ed is None or ed.empty:
        return []
    out: list[EarningsEvent] = []
    for ts, row in ed.iterrows():
        est = row.get("EPS Estimate")
        act = row.get("Reported EPS")
        sur = row.get("Surprise(%)")
        if act is None or (isinstance(act, float) and np.isnan(act)):
            continue
        est_f = None if est is None or pd.isna(est) else float(est)
        act_f = float(act)
        if sur is None or pd.isna(sur):
            if est_f is not None and abs(est_f) > 1e-9:
                sur_f: float | None = (act_f - est_f) / abs(est_f) * 100.0
            else:
                sur_f = None
        else:
            sur_f = float(sur)
        out.append(EarningsEvent(ticker, _to_ny(ts), est_f, act_f, sur_f))
    out.sort(key=lambda e: e.event_ts, reverse=True)
    return out


def fetch_earnings_events_map(
    tickers: list[str], limit: int = 12, max_workers: int = 6
) -> dict[str, list[EarningsEvent]]:
    out: dict[str, list[EarningsEvent]] = {}
    if not tickers:
        return out
    logger.info("Fetching earnings history for %d tickers (limit=%d)", len(tickers), limit)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_earnings_events, t, limit): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                out[t] = fut.result()
            except Exception:  # noqa: BLE001
                out[t] = []
    return out


def _naive_sessions(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.to_datetime(index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return pd.DatetimeIndex(idx).normalize()


def reaction_session(
    event: EarningsEvent, sessions: pd.DatetimeIndex
) -> pd.Timestamp | None:
    """First session that fully reflects the print (see module docstring)."""
    sessions = _naive_sessions(sessions)
    day = pd.Timestamp(event.event_ts.date())
    if event.after_close:
        later = sessions[sessions > day]
    else:
        later = sessions[sessions >= day]
    if len(later) == 0:
        return None
    return later[0]


@dataclass
class ReactionStats:
    reaction_date: pd.Timestamp
    entry_date: pd.Timestamp  # R+1
    entry_price: float
    raw_ret_pct: float  # close[R-1] -> close[R+1]
    ear_pct: float  # raw minus SPY same window
    gap_pct: float | None  # close[R-1] -> open[R] (if open available)
    sessions_since_reaction: int  # as of the last bar in `close`


def earnings_reaction(
    close: pd.Series,
    spy_close: pd.Series,
    reaction_date: pd.Timestamp,
    open_: pd.Series | None = None,
) -> ReactionStats | None:
    """Compute EAR around ``reaction_date``; ``close`` must extend ≥ R+1."""
    c = close.dropna().copy()
    c.index = _naive_sessions(c.index)
    s = spy_close.dropna().copy()
    s.index = _naive_sessions(s.index)
    r = pd.Timestamp(reaction_date).normalize()
    if r not in c.index:
        return None
    pos = c.index.get_loc(r)
    if pos < 1 or pos + 1 >= len(c):
        return None
    pre, post = c.index[pos - 1], c.index[pos + 1]
    if pre not in s.index or post not in s.index:
        return None
    raw = float(c.loc[post] / c.loc[pre] - 1.0) * 100.0
    spy_ret = float(s.loc[post] / s.loc[pre] - 1.0) * 100.0
    gap: float | None = None
    if open_ is not None:
        o = open_.dropna().copy()
        o.index = _naive_sessions(o.index)
        if r in o.index and float(c.loc[pre]) > 0:
            gap = float(o.loc[r] / c.loc[pre] - 1.0) * 100.0
    return ReactionStats(
        reaction_date=r,
        entry_date=post,
        entry_price=float(c.loc[post]),
        raw_ret_pct=raw,
        ear_pct=raw - spy_ret,
        gap_pct=gap,
        sessions_since_reaction=int(len(c) - 1 - pos),
    )


def pead_composite(
    *,
    ear_pct: float,
    surprise_pct: float | None,
    sessions_since_reaction: int,
    max_sessions: int,
) -> float:
    """Heuristic 0–100 rank score: EAR (≤60) + surprise (≤25) + freshness (≤15)."""
    ear_c = float(np.clip(ear_pct / 10.0, 0.0, 1.0)) * 60.0
    sur_c = float(np.clip((surprise_pct or 0.0) / 25.0, 0.0, 1.0)) * 25.0
    fresh = 1.0 - float(np.clip((sessions_since_reaction - 1) / max(max_sessions - 1, 1), 0.0, 1.0))
    return round(ear_c + sur_c + fresh * 15.0, 2)
