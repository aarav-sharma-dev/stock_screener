#!/usr/bin/env python3
"""Does an Iran-war oil shock predict the next session?

The 2026 Iran war moved SKYQ (Sky Quarry, a micro-cap Nevada refiner) because
crude spiked, not because anyone modeled the company. This script tests the
channel with thresholds fixed up front:

  shock day  — WTI (CL=F) close-to-close return ≥ +3% (also ≥ +5%)
  mirror     — WTI ≤ −3%
  baskets    — SKYQ; liquid refiners; producers; defense; airlines (hurt)
  horizons   — same day (descriptive, not a trade), then +1 / +5 / +10 sessions
  window     — war: 2026-02-28 → today; baseline: 2025 calendar year

Same-day numbers describe the reaction. A trade is the forward window, entered
at the shock-day close. Costs are not in the mean; dollar volume is printed so
SKYQ can be judged separately from VLO/XLE.

Outputs: output/iran_oil_shock_days.csv, output/iran_oil_shock_summary.csv
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

OUTPUT = ROOT / "output"
WAR_START = pd.Timestamp("2026-02-28")
PRE_START = pd.Timestamp("2025-01-01")
PRE_END = pd.Timestamp("2025-12-31")
HORIZONS = (0, 1, 5, 10)

BASKETS: dict[str, list[str]] = {
    "narrative": ["SKYQ"],
    "refiners": ["VLO", "MPC", "PSX", "DINO"],
    "producers": ["XLE", "XOP", "OXY", "CVX"],
    "defense": ["LMT", "RTX", "NOC"],
    "airlines": ["DAL", "UAL"],
    "market": ["SPY"],
}
# A few headline days with a published source. Not a scan — an annotation.
HEADLINES = {
    "2026-02-28": "US/Israel war launch (NBC)",
    "2026-10-08": "Strike-option reports, then no-attack-before-midterms post",
}


def _naive_close(frame: pd.DataFrame, ticker: str) -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    col = None
    if isinstance(frame.columns, pd.MultiIndex):
        if ("Close", ticker) in frame.columns:
            col = frame[("Close", ticker)]
        elif (ticker, "Close") in frame.columns:
            col = frame[(ticker, "Close")]
    elif "Close" in frame.columns:
        col = frame["Close"]
    if col is None:
        return pd.Series(dtype=float)
    s = col.dropna().copy()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = pd.DatetimeIndex(idx).normalize()
    return s[~s.index.duplicated(keep="last")]


def _naive_volume(frame: pd.DataFrame, ticker: str) -> pd.Series:
    if frame is None or frame.empty or not isinstance(frame.columns, pd.MultiIndex):
        return pd.Series(dtype=float)
    col = None
    if ("Volume", ticker) in frame.columns:
        col = frame[("Volume", ticker)]
    elif (ticker, "Volume") in frame.columns:
        col = frame[(ticker, "Volume")]
    if col is None:
        return pd.Series(dtype=float)
    s = col.dropna().copy()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = pd.DatetimeIndex(idx).normalize()
    return s[~s.index.duplicated(keep="last")]


def forward_return(close: pd.Series, day: pd.Timestamp, horizon: int) -> float | None:
    if day not in close.index:
        return None
    pos = close.index.get_loc(day)
    if isinstance(pos, slice):
        return None
    # h0 is that session's own move (prior close → close). h>0 is from the close.
    if horizon == 0:
        if pos < 1:
            return None
        a, b = float(close.iloc[pos - 1]), float(close.iloc[pos])
    else:
        j = pos + horizon
        if j >= len(close) or j < 0:
            return None
        a, b = float(close.iloc[pos]), float(close.iloc[j])
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def summarize(name: str, xs: pd.Series) -> dict:
    x = xs.dropna()
    if len(x) < 2:
        return {
            "name": name,
            "n": int(len(x)),
            "mean_xs": None,
            "median_xs": None,
            "hit_pct": None,
            "t": None,
        }
    t = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if x.std() > 0 else None
    return {
        "name": name,
        "n": int(len(x)),
        "mean_xs": round(float(x.mean()), 2),
        "median_xs": round(float(x.median()), 2),
        "hit_pct": round(float((x > 0).mean() * 100.0), 1),
        "t": round(t, 2) if t is not None else None,
    }


def _money(s: str) -> float:
    raw = str(s).replace("$", "").replace(",", "").strip()
    if raw in {"", "N/A", "n/a", "--"}:
        return float("nan")
    return float(raw)


def _as_naive_days(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").normalize().tz_localize(None)
    else:
        idx = idx.normalize()
    return idx


def _yahoo_daily(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Series, pd.Series]:
    """Yahoo daily close and volume. yfinance first, chart API if that returns nothing."""
    import yfinance as yf

    raw = None
    try:
        raw = yf.download(
        symbol,
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    yfinance {symbol}: {exc.__class__.__name__}", flush=True)
    close = pd.Series(dtype=float)
    vol = pd.Series(dtype=float)
    if raw is not None and not raw.empty:
        cols = raw.columns
        if isinstance(cols, pd.MultiIndex):
            c = raw["Close"].iloc[:, 0] if isinstance(raw["Close"], pd.DataFrame) else raw["Close"]
            v = raw["Volume"].iloc[:, 0] if isinstance(raw["Volume"], pd.DataFrame) else raw["Volume"]
        elif "Close" in raw.columns:
            c, v = raw["Close"], raw["Volume"]
        else:
            c, v = pd.Series(dtype=float), pd.Series(dtype=float)
        if len(c):
            idx = _as_naive_days(c.index)
            close = pd.Series(pd.to_numeric(c, errors="coerce").to_numpy(), index=idx, dtype=float).dropna()
            vol = pd.Series(pd.to_numeric(v, errors="coerce").to_numpy(), index=idx, dtype=float)
            close = close[~close.index.duplicated(keep="last")].sort_index()
            vol = vol[~vol.index.duplicated(keep="last")].sort_index()
    if len(close) >= 100:
        return close, vol

    import requests

    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={
            "interval": "1d",
            "period1": int(start.timestamp()),
            "period2": int((end + pd.Timedelta(days=1)).timestamp()),
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    idx = _as_naive_days(pd.to_datetime(ts, unit="s", utc=True))
    close = pd.Series(q["close"], index=idx, dtype=float).dropna()
    vol = pd.Series(q.get("volume") or [], index=idx, dtype=float)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    vol = vol[~vol.index.duplicated(keep="last")].sort_index()
    return close, vol


def _schwab_daily(symbol: str, client, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Series, pd.Series]:
    from stock_screener.data.schwab import price_history_daily

    df = price_history_daily(symbol, start=start, end=end + pd.Timedelta(days=1), client=client)
    if df is None or df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    idx = _as_naive_days(df.index)
    close = pd.Series(df["Close"].to_numpy(), index=idx, dtype=float).dropna()
    vol = pd.Series(df["Volume"].to_numpy(), index=idx, dtype=float)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    vol = vol.reindex(close.index)
    return close, vol


def load_bars(
    tickers: list[str], start: pd.Timestamp, end: pd.Timestamp
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], pd.Series, dict[str, str]]:
    """Schwab daily history for every name, Yahoo when Schwab has no usable series."""
    from stock_screener.data.schwab import SchwabAPIError, SchwabNotConfiguredError, get_client

    client = None
    try:
        client = get_client()
    except (SchwabAPIError, SchwabNotConfiguredError) as exc:
        print(f"  Schwab unavailable ({exc.__class__.__name__}); Yahoo for every series", flush=True)

    close: dict[str, pd.Series] = {}
    vol: dict[str, pd.Series] = {}
    source: dict[str, str] = {}
    for t in tickers:
        got = False
        if client is not None:
            try:
                c, v = _schwab_daily(t, client, start, end)
                if len(c) >= 100:
                    close[t], vol[t], source[t] = c, v, "schwab"
                    got = True
                    print(f"  {t:6} schwab  {len(c)} bars → {c.index.max().date()}", flush=True)
            except (SchwabAPIError, SchwabNotConfiguredError) as exc:
                print(f"  {t:6} schwab failed ({exc.__class__.__name__})", flush=True)
                client = None
        if not got:
            c, v = _yahoo_daily(t, start, end)
            close[t], vol[t], source[t] = c, v, "yahoo"
            print(f"  {t:6} yahoo   {len(c)} bars → {c.index.max().date() if len(c) else 'none'}", flush=True)

    wti = pd.Series(dtype=float)
    if client is not None:
        for sym in ("/CL", "CL"):
            try:
                c, _v = _schwab_daily(sym, client, start, end)
                if len(c) >= 100:
                    wti = c
                    source["WTI"] = f"schwab:{sym}"
                    print(f"  WTI    {source['WTI']}  {len(c)} bars → {c.index.max().date()}", flush=True)
                    break
            except (SchwabAPIError, SchwabNotConfiguredError) as exc:
                print(f"  WTI    schwab {sym} failed ({exc.__class__.__name__})", flush=True)
    if wti.empty:
        wti, _v = _yahoo_daily("CL=F", start, end)
        source["WTI"] = "yahoo:CL=F"
        print(f"  WTI    yahoo:CL=F  {len(wti)} bars → {wti.index.max().date() if len(wti) else 'none'}", flush=True)
    return close, vol, wti, source


def load_local(tickers: list[str]) -> tuple[dict[str, pd.Series], dict[str, pd.Series], pd.Series]:
    """Nasdaq daily bars in output/iran_raw plus Yahoo CL=F saved as CLF.json."""
    raw_dir = OUTPUT / "iran_raw"
    close: dict[str, pd.Series] = {}
    vol: dict[str, pd.Series] = {}
    for t in tickers:
        rows = json.loads((raw_dir / f"{t}.json").read_text())
        idx, px, vv = [], [], []
        for r in rows:
            idx.append(pd.Timestamp(r["date"]).normalize())
            px.append(_money(r["close"]))
            vv.append(_money(r["volume"]))
        c = pd.Series(px, index=pd.DatetimeIndex(idx), dtype=float).dropna()
        v = pd.Series(vv, index=pd.DatetimeIndex(idx), dtype=float)
        c = c[~c.index.duplicated(keep="last")].sort_index()
        v = v[~v.index.duplicated(keep="last")].sort_index()
        close[t] = c
        vol[t] = v
    doc = json.loads((raw_dir / "CLF.json").read_text())
    result = doc["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    idx = pd.to_datetime(ts, unit="s").tz_localize("UTC").tz_convert("America/New_York")
    idx = pd.DatetimeIndex(idx).normalize().tz_localize(None)
    wti = pd.Series(q["close"], index=idx, dtype=float)
    wti = wti.dropna()
    wti = wti[~wti.index.duplicated(keep="last")].sort_index()
    return close, vol, wti


def main() -> int:
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    tickers = sorted({t for names in BASKETS.values() for t in names})
    print(f"Iran oil-shock test · war {WAR_START.date()} → {today.date()} · baseline 2025", flush=True)
    print("Prices: Schwab, Yahoo if a series is missing", flush=True)
    close, vol, wti, sources = load_bars(tickers, PRE_START, today)
    n_sch = sum(1 for s in sources.values() if s.startswith("schwab"))
    print(f"  sources: {n_sch} Schwab, {len(sources) - n_sch} Yahoo", flush=True)
    if wti.empty:
        print("No WTI (CL=F) history")
        return 1
    spy = close["SPY"]

    war_idx = wti.index[(wti.index >= WAR_START) & (wti.index <= today)]
    wti_ret = wti.pct_change() * 100.0
    shocks_3 = [d for d in war_idx if d in wti_ret.index and wti_ret.loc[d] >= 3.0]
    shocks_5 = [d for d in shocks_3 if wti_ret.loc[d] >= 5.0]
    fades = [d for d in war_idx if d in wti_ret.index and wti_ret.loc[d] <= -3.0]
    print(
        f"WTI shock days in war window: ≥3% = {len(shocks_3)}, ≥5% = {len(shocks_5)}, "
        f"≤−3% = {len(fades)} (of {len(war_idx)} sessions)"
    )

    # Liquidity snapshot so SKYQ is not treated like VLO
    print("\nMedian $ volume, war window (close × shares):")
    for t in tickers:
        c, v = close[t], vol[t]
        if c.empty or v.empty:
            print(f"  {t:6}  no data")
            continue
        both = pd.concat([c.rename("c"), v.rename("v")], axis=1).dropna()
        both = both.loc[(both.index >= WAR_START) & (both.index <= today)]
        dol = float((both["c"] * both["v"]).median()) if len(both) else float("nan")
        last = float(c.loc[:today].iloc[-1]) if len(c.loc[:today]) else float("nan")
        print(f"  {t:6}  ${dol/1e6:8.2f}M   last ${last:.2f}")

    rows: list[dict] = []
    for d in sorted(set(shocks_3) | set(fades)):
        row: dict = {
            "date": d.date().isoformat(),
            "wti_ret": round(float(wti_ret.loc[d]), 2),
            "headline": HEADLINES.get(d.date().isoformat(), ""),
            "kind": "up>=3" if d in shocks_3 else "down<=-3",
        }
        for t in tickers:
            for h in HORIZONS:
                r = forward_return(close[t], d, h)
                row[f"{t}_h{h}"] = round(r, 2) if r is not None else None
        rows.append(row)
    days = pd.DataFrame(rows)
    OUTPUT.mkdir(exist_ok=True)
    days.to_csv(OUTPUT / "iran_oil_shock_days.csv", index=False)

    def basket_xs(days_list: list[pd.Timestamp], names: list[str], horizon: int) -> pd.Series:
        """Equal-weight excess vs SPY across names, one obs per shock day."""
        out = []
        for d in days_list:
            rets = []
            for t in names:
                r = forward_return(close[t], d, horizon)
                if r is not None:
                    rets.append(r)
            s = forward_return(spy, d, horizon)
            if rets and s is not None:
                out.append(float(np.mean(rets) - s))
        return pd.Series(out)

    summary_rows = []
    sets = {
        "WTI>=3 war": shocks_3,
        "WTI>=5 war": shocks_5,
        "WTI<=-3 war": fades,
    }
    # Unconditional war-day baseline: every session, horizon 1, excess vs SPY
    all_war = list(war_idx)
    for label, names in BASKETS.items():
        for h in (1, 5, 10):
            base = basket_xs(all_war, names, h)
            sm = summarize(f"{label} | all war days | +{h}d", base)
            sm.update({"basket": label, "set": "all war days", "horizon": h})
            summary_rows.append(sm)
        for set_name, dset in sets.items():
            for h in (0, 1, 5, 10):
                xs = basket_xs(dset, names, h)
                sm = summarize(f"{label} | {set_name} | h{h}", xs)
                sm.update({"basket": label, "set": set_name, "horizon": h})
                summary_rows.append(sm)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUTPUT / "iran_oil_shock_summary.csv", index=False)

    show = summary[summary["set"] != "all war days"]
    print("\n=== Excess vs SPY, equal-weight basket, entered at shock close (h0 = same day) ===")
    for set_name in sets:
        print(f"\n-- {set_name} --")
        sub = show[show["set"] == set_name]
        print(
            sub.pivot(index="basket", columns="horizon", values="mean_xs")
            .reindex(BASKETS.keys())
            .to_string()
        )
        n0 = sub[sub["horizon"] == 0]["n"]
        print(f"   n (h0) = {int(n0.iloc[0]) if len(n0) else 0}")

    print("\n=== Next-day excess vs the unconditional war-day baseline ===")
    base1 = summary[(summary["set"] == "all war days") & (summary["horizon"] == 1)].set_index(
        "basket"
    )
    shock1 = summary[(summary["set"] == "WTI>=3 war") & (summary["horizon"] == 1)].set_index(
        "basket"
    )
    for b in BASKETS:
        if b not in shock1.index or b not in base1.index:
            continue
        a, c = shock1.loc[b, "mean_xs"], base1.loc[b, "mean_xs"]
        t = shock1.loc[b, "t"]
        if a is None or c is None:
            continue
        print(f"  {b:12} shock +1d {a:+6.2f}%   all-days +1d {c:+6.2f}%   t={t}")

    # Pre-war vs war beta to WTI (does the relationship even exist before 2026-02-28?)
    print("\n=== Daily beta to WTI (return on return) ===")
    print(f"  {'':12} {'beta 2025':>10} {'beta war':>10} {'corr war':>10}")
    for t in tickers:
        def _beta(start, end) -> tuple[float | None, float | None]:
            c = close[t].pct_change() * 100.0
            both = pd.concat([c.rename("y"), wti_ret.rename("x")], axis=1).dropna()
            both = both.loc[(both.index >= start) & (both.index <= end)]
            if len(both) < 30 or both["x"].std() == 0:
                return None, None
            beta = float(np.cov(both["y"], both["x"], ddof=1)[0, 1] / both["x"].var(ddof=1))
            corr = float(both["y"].corr(both["x"]))
            return beta, corr

        b0, _ = _beta(PRE_START, PRE_END)
        b1, c1 = _beta(WAR_START, today)
        def fmt(v: float | None) -> str:
            return f"{v:+.2f}" if v is not None else "  n/a"

        print(f"  {t:12} {fmt(b0):>10} {fmt(b1):>10} {fmt(c1):>10}")

    if not days.empty and days["headline"].astype(str).str.len().gt(0).any():
        print("\n=== Dated headlines (annotation, not a sample) ===")
        ann = days[days["headline"].astype(str).str.len() > 0]
        cols = ["date", "wti_ret", "headline"] + [
            c for c in ("SKYQ_h0", "SKYQ_h1", "XLE_h0", "XLE_h1", "SPY_h0", "SPY_h1") if c in ann.columns
        ]
        print(ann[cols].to_string(index=False))

    print("\n=== Means vs medians (WTI≥3%, excess vs SPY) — SKYQ means are outlier-driven ===")
    sub = summary[(summary["set"] == "WTI>=3 war") & (summary["basket"] == "narrative")]
    print(sub[["horizon", "n", "mean_xs", "median_xs", "hit_pct", "t"]].to_string(index=False))

    print("\n=== War-window total return (2026-02-27 close → last) ===")
    for t in ("SKYQ", "XLE", "XOP", "VLO", "LMT", "UAL", "DAL", "SPY"):
        s = close[t]
        s = s.loc[(s.index >= WAR_START - pd.Timedelta(days=5)) & (s.index <= today)]
        if len(s) < 2:
            continue
        base = s.loc[:WAR_START]
        if base.empty:
            continue
        tot = float(s.iloc[-1] / base.iloc[0] - 1.0) * 100.0
        print(f"  {t:6} {tot:+8.1f}%   ({base.index[0].date()} ${base.iloc[0]:.2f} → {s.index[-1].date()} ${s.iloc[-1]:.2f})")

    print("\nWrote output/iran_oil_shock_days.csv, output/iran_oil_shock_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
