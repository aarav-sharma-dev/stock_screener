#!/usr/bin/env python3
"""Build today's focus + Cursor canvas from output/screen_*.csv.

Reproducible dashboard for anyone with a fresh screener run:

  stock-screener --mode all --universe sp500 --limit 80 --top 25 \\
    --allow-below-200 --no-reddit --no-grok --no-trump-x
  python3 scripts/build_screen_dashboard.py

Writes:
  output/today_focus.csv
  wiki/Home.md (Today's focus section prepended)
  ~/.cursor/projects/<workspace>/canvases/screen-dashboard.canvas.tsx
  docs/screen-dashboard.canvas.tsx (repo copy for git)
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
WIKI_MD = ROOT / "wiki" / "Home.md"
DOCS_CANVAS = ROOT / "docs" / "screen-dashboard.canvas.tsx"
BACKTEST_JSON = OUTPUT / "backtest_ytd_summary.json"


def _canvas_out() -> Path:
    override = os.environ.get("SCREEN_DASHBOARD_CANVAS")
    if override:
        return Path(override)
    # Default Cursor managed canvases folder for this workspace
    home = Path.home()
    candidates = [
        home
        / ".cursor"
        / "projects"
        / "Users-asharma-Projects-stock-screener"
        / "canvases"
        / "screen-dashboard.canvas.tsx",
        home
        / ".cursor"
        / "projects"
        / f"Users-{os.environ.get('USER', 'user')}-Projects-stock-screener"
        / "canvases"
        / "screen-dashboard.canvas.tsx",
    ]
    for c in candidates:
        if c.parent.exists() or c.parent.parent.exists():
            c.parent.mkdir(parents=True, exist_ok=True)
            return c
    DOCS_CANVAS.parent.mkdir(parents=True, exist_ok=True)
    return DOCS_CANVAS


def read_df(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(x: Any, default: float | None = None) -> float | None:
    if x is None or x == "":
        return default
    try:
        v = float(x)
        if v != v:  # NaN
            return default
        return v
    except (TypeError, ValueError):
        return default


def score_key(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "score"
    return "composite" if "composite" in rows[0] else "score"


def parse_run_meta(log_path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "asOf": datetime.now(timezone.utc).date().isoformat(),
        "vix": None,
        "regime": "neutral",
        "cape": None,
        "buffett": None,
        "cycle": "fair",
        "trumpPosts": None,
        "trumpTickers": None,
        "truth": None,
        "news": None,
        "wh": None,
    }
    if not log_path.exists():
        return meta
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"VIX=([0-9.]+)", text)
    if m:
        meta["vix"] = float(m.group(1))
    m = re.search(r"Market: (\w+)", text)
    if m:
        meta["regime"] = m.group(1)
    m = re.search(r"CAPE=([0-9.]+)", text)
    if m:
        meta["cape"] = float(m.group(1))
    m = re.search(r"Buffett=([0-9.]+)%", text)
    if m:
        meta["buffett"] = float(m.group(1))
    m = re.search(r"Market cycle: (\w+)", text)
    if m:
        meta["cycle"] = m.group(1)
    m = re.search(r"Trump tracker collected (\d+)", text)
    if m:
        meta["trumpPosts"] = int(m.group(1))
    m = re.search(r"Trump tracker scored (\d+)", text)
    if m:
        meta["trumpTickers"] = int(m.group(1))
    m = re.search(r"Truth Social mirror: (\d+)", text)
    if m:
        meta["truth"] = int(m.group(1))
    m = re.search(r"Trump news RSS: (\d+)", text)
    if m:
        meta["news"] = int(m.group(1))
    m = re.search(r"White House RSS: (\d+)", text)
    if m:
        meta["wh"] = int(m.group(1))
    return meta


def enrich_fundamentals(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fill gaps via Yahoo for focus names missing PE/PEG/FCF/ROE."""
    out: dict[str, dict[str, Any]] = {}
    if not tickers:
        return out
    try:
        import yfinance as yf
    except ImportError:
        return out
    for t in tickers:
        try:
            info = yf.Ticker(t).info or {}
        except Exception:
            continue
        fcf = info.get("freeCashflow")
        mcap = info.get("marketCap")
        out[t] = {
            "sector": info.get("sector") or "",
            "pe": fnum(info.get("trailingPE")),
            "peg": fnum(info.get("pegRatio") or info.get("trailingPegRatio")),
            "fcfy": (fcf / mcap * 100.0) if fcf and mcap else None,
            "roe": (fnum(info.get("returnOnEquity")) or 0) * 100.0
            if info.get("returnOnEquity") is not None
            else None,
            "price": fnum(info.get("currentPrice") or info.get("regularMarketPrice")),
        }
    return out


def build_index(
    modes: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = defaultdict(dict)
    for name, rows in modes.items():
        sk = score_key(rows)
        for r in rows:
            t = r["ticker"]
            info[t][name] = fnum(r.get(sk))
            if name == "politicians":
                info[t]["pol_buys"] = int(float(r.get("buy_count") or 0))
                info[t]["pol_sells"] = int(float(r.get("sell_count") or 0))
            if "price" in r and fnum(r.get("price")) is not None:
                info[t].setdefault("price", fnum(r.get("price")))
            if name == "athdip":
                info[t]["rsi_4h"] = fnum(r.get("rsi_4h"))
                info[t]["pct_from_ath"] = fnum(r.get("pct_from_ath"))
                info[t]["trigger_date"] = (r.get("trigger_date") or "").strip()
                info[t]["trigger_price"] = fnum(r.get("trigger_price"))
                m = re.search(r"ATH=([0-9.]+)", r.get("why") or "")
                if m:
                    info[t]["ath"] = float(m.group(1))
                # Parse trigger from why if columns missing: trigger=YYYY-MM-DD @price
                if not info[t].get("trigger_date"):
                    m2 = re.search(
                        r"trigger=(\d{4}-\d{2}-\d{2})(?: @([0-9.]+))?",
                        r.get("why") or "",
                    )
                    if m2:
                        info[t]["trigger_date"] = m2.group(1)
                        if m2.group(2):
                            info[t]["trigger_price"] = float(m2.group(2))
                info[t]["days_since_ath"] = int(float(r.get("days_since_ath") or -1))
                info[t]["rsi14"] = fnum(r.get("rsi14"))
            if name == "pead":
                info[t]["ear_pct"] = fnum(r.get("ear_pct"))
                info[t]["surprise_pct"] = fnum(r.get("surprise_pct"))
                info[t]["reaction_date"] = (r.get("reaction_date") or "").strip()
                info[t]["sessions_since_reaction"] = int(
                    float(r.get("sessions_since_reaction") or 0)
                )
                info[t].setdefault("rsi14", fnum(r.get("rsi14")))
            if name in ("support", "catalyst") and fnum(r.get("rsi14")) is not None:
                info[t].setdefault("rsi14", fnum(r.get("rsi14")))
            if name in ("support", "fundamentals", "catalyst"):
                for src, dst, scale in (
                    ("sector", "sector", 1.0),
                    ("peg", "peg", 1.0),
                    ("trailing_pe", "pe", 1.0),
                    ("fcf_yield", "fcfy", 100.0),
                    ("roe", "roe", 100.0),
                ):
                    if src in r and fnum(r.get(src)) is not None:
                        v = fnum(r.get(src))
                        if v is None:
                            continue
                        if dst in ("fcfy", "roe") and abs(v) <= 1.5:
                            v *= scale
                        elif dst in ("fcfy", "roe") and scale != 1.0 and abs(v) > 1.5:
                            pass
                        info[t].setdefault(dst, v if dst != "sector" else r.get(src))
                    elif src == "sector" and r.get("sector"):
                        info[t].setdefault("sector", r.get("sector"))
            if name == "support" and fnum(r.get("fund_score")) is not None:
                info[t].setdefault("fundamentals", fnum(r.get("fund_score")))
                for src, dst in (
                    ("peg", "peg"),
                    ("trailing_pe", "pe"),
                    ("fcf_yield", "fcfy"),
                    ("roe", "roe"),
                ):
                    v = fnum(r.get(src))
                    if v is None:
                        continue
                    if dst in ("fcfy", "roe") and abs(v) <= 1.5:
                        v *= 100.0
                    info[t].setdefault(dst, v)
    return info


def athdip_keep_for_today(
    info_row: dict[str, Any],
    *,
    as_of: str,
) -> tuple[bool, str]:
    """Keep athdip in Today only if trigger was today or px still below trigger px."""
    trig = (info_row.get("trigger_date") or "").strip()[:10]
    trig_px = info_row.get("trigger_price")
    cur_px = info_row.get("price")
    if trig and trig == as_of[:10]:
        return True, f"trigger today ({trig})"
    if (
        trig_px is not None
        and cur_px is not None
        and float(trig_px) > 0
        and float(cur_px) < float(trig_px)
    ):
        return True, f"px {float(cur_px):.2f} < trigger {float(trig_px):.2f} ({trig or '?'})"
    return False, (
        f"stale athdip (trigger={trig or 'n/a'} @ {trig_px}; px={cur_px})"
    )


def classify_signal(
    kind: str,
    tags: str,
    *,
    pol_buys: int | None = None,
    pol_sells: int | None = None,
) -> str:
    """Long-only watchlist label: BUY / SELL / MIXED."""
    kind_l = (kind or "").lower()
    tags_l = (tags or "").lower()
    buys = int(pol_buys or 0)
    sells = int(pol_sells or 0)
    pol_sell_lean = "politicians" in tags_l and sells > buys
    long_modes = any(
        m in tags_l
        for m in ("athdip", "pead", "support", "catalyst", "fundamentals", "trump")
    )
    if kind_l == "conflict" or (pol_sell_lean and long_modes):
        return "MIXED"
    if pol_sell_lean and not long_modes:
        return "SELL"
    if "politicians" in tags_l and buys > sells and not long_modes:
        return "BUY"
    return "BUY"


def pick_focus(
    modes: dict[str, list[dict[str, str]]],
    info: dict[str, dict[str, Any]],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    sets = {name: {r["ticker"] for r in rows} for name, rows in modes.items()}
    ath = sets.get("athdip", set())
    pol = sets.get("politicians", set())
    # Only athdip names that triggered today OR are still below trigger price
    ath_today: set[str] = set()
    for t in ath:
        keep, _ = athdip_keep_for_today(info[t], as_of=as_of)
        if keep:
            ath_today.add(t)
    ath = ath_today
    pol = sets.get("politicians", set())
    sup = sets.get("support", set())
    cat = sets.get("catalyst", set())
    trm = sets.get("trump", set())
    fnd = sets.get("fundamentals", set())
    ped = sets.get("pead", set())

    picks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def tags_for(t: str) -> list[str]:
        order = [
            "athdip",
            "pead",
            "support",
            "catalyst",
            "fundamentals",
            "politicians",
            "trump",
        ]
        return [
            m
            for m in order
            if m in info[t]
            and info[t].get(m) is not None
            and (m != "athdip" or t in ath)
        ]

    def row(t: str, kind: str, why: str) -> dict[str, Any]:
        i = info[t]
        tags = " · ".join(tags_for(t))
        signal = classify_signal(
            kind,
            tags,
            pol_buys=i.get("pol_buys"),
            pol_sells=i.get("pol_sells"),
        )
        return {
            "kind": kind,
            "ticker": t,
            "signal": signal,
            "why": why,
            "tags": tags,
            "price": i.get("price"),
            "athdip": i.get("athdip"),
            "pead": i.get("pead"),
            "support": i.get("support"),
            "catalyst": i.get("catalyst"),
            "fund": i.get("fundamentals"),
            "pol": i.get("politicians"),
            "trump": i.get("trump"),
            "rsi14": i.get("rsi14"),
            "rsi4h": i.get("rsi_4h"),
            "pctAth": i.get("pct_from_ath"),
            "polBuys": i.get("pol_buys"),
            "polSells": i.get("pol_sells"),
            "sector": (i.get("sector") or "")[:22],
            "pe": i.get("pe"),
            "peg": i.get("peg"),
            "fcfy": i.get("fcfy"),
            "roe": i.get("roe"),
            "trigger_date": i.get("trigger_date"),
            "trigger_price": i.get("trigger_price"),
        }

    # Athdip (filtered) always
    for t in sorted(ath, key=lambda x: -(info[x].get("athdip") or 0)):
        kind = "athdip"
        _, keep_why = athdip_keep_for_today(info[t], as_of=as_of)
        why = f"Athdip · {keep_why}"
        if t in pol:
            b, s = info[t].get("pol_buys", 0), info[t].get("pol_sells", 0)
            if s > b:
                kind = "conflict"
                why = f"Athdip dip vs Congress sells ({b}/{s}) · {keep_why}"
            elif b > s:
                kind = "confirm"
                why = f"Athdip + Congress buys ({b}/{s}) · {keep_why}"
            else:
                kind = "intersection"
                why = f"Athdip ∩ politicians · {keep_why}"
        elif len(tags_for(t)) >= 3:
            kind = "intersection"
            why = f"Athdip + multi-mode overlap · {keep_why}"
        picks.append(row(t, kind, why))
        seen.add(t)

    # PEAD (top 3 by composite): fresh positive surprise + positive reaction
    for t in sorted(ped, key=lambda x: -(info[x].get("pead") or 0))[:3]:
        if t in seen:
            continue
        i = info[t]
        ear = i.get("ear_pct")
        sur = i.get("surprise_pct")
        why = (
            f"PEAD · EAR {ear:+.1f}% vs SPY" if ear is not None else "PEAD"
        )
        if sur is not None:
            why += f" · EPS surprise {sur:+.1f}%"
        if i.get("reaction_date"):
            why += f" · reaction {i['reaction_date']} ({i.get('sessions_since_reaction', 0)}d ago)"
        kind = "pead"
        if t in pol and (i.get("pol_sells") or 0) > (i.get("pol_buys") or 0):
            kind = "conflict"
            why = f"PEAD vs Congress sells ({i.get('pol_buys', 0)}/{i.get('pol_sells', 0)}) · " + why
        elif len(tags_for(t)) >= 3:
            kind = "intersection"
        picks.append(row(t, kind, why))
        seen.add(t)

    # Support + fund strong
    for t in sorted(sup & fnd):
        if t in seen:
            continue
        if (info[t].get("support") or 0) >= 75 and (info[t].get("fundamentals") or 0) >= 70:
            note = "Support+fund confirm"
            if t in pol and (info[t].get("pol_sells") or 0) > (info[t].get("pol_buys") or 0):
                note += f" · pol sell={info[t].get('pol_sells')}"
            picks.append(row(t, "intersection", note))
            seen.add(t)

    # Single-mode leaders
    for name, label in (
        ("support", "Top support score today"),
        ("catalyst", "Top catalyst score today"),
        ("trump", "Top Trump tracker"),
        ("politicians", "Top politicians"),
        ("fundamentals", "Top fundamentals"),
    ):
        rows = modes.get(name) or []
        if not rows:
            continue
        sk = score_key(rows)
        top = max(rows, key=lambda r: fnum(r.get(sk), -1e9) or -1e9)
        t = top["ticker"]
        if t in seen:
            continue
        why = label
        if name == "politicians":
            why = (
                f"{label} · buys={info[t].get('pol_buys', 0)} "
                f"sells={info[t].get('pol_sells', 0)}"
            )
        if name == "trump" and (info[t].get("pol_sells") or 0) > 0:
            why += f" · pol sells={info[t].get('pol_sells')}"
        picks.append(row(t, "single", why))
        seen.add(t)
        if sum(1 for p in picks if p["kind"] == "single") >= 4:
            break

    return picks


def round_or_none(v: Any, n: int = 1) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def write_focus_csv(picks: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "kind",
        "ticker",
        "signal",
        "price",
        "why",
        "tags",
        "athdip",
        "pead",
        "support",
        "catalyst",
        "fund",
        "pol",
        "trump",
        "rsi14",
        "rsi4h",
        "pctAth",
        "polBuys",
        "polSells",
        "sector",
        "pe",
        "peg",
        "fcfy",
        "roe",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in picks:
            row = {k: p.get(k) for k in fields}
            w.writerow(row)


def js_num(v: Any, digits: int = 2) -> str:
    if v is None:
        return "null"
    return str(round(float(v), digits))


def js_str(s: Any) -> str:
    return json.dumps("" if s is None else str(s))


def top_rows(rows: list[dict[str, str]], n: int, sk: str) -> list[dict[str, str]]:
    return sorted(rows, key=lambda r: fnum(r.get(sk), -1e9) or -1e9, reverse=True)[:n]


def render_canvas(
    meta: dict[str, Any],
    modes: dict[str, list[dict[str, str]]],
    picks: list[dict[str, Any]],
    info: dict[str, dict[str, Any]],
    backtest: list[dict[str, Any]],
) -> str:
    # RUN block
    ath_rows = modes.get("athdip") or []
    lowest_rsi = None
    if ath_rows:
        lowest_rsi = min(
            (fnum(r.get("rsi_4h")) for r in ath_rows if fnum(r.get("rsi_4h")) is not None),
            default=None,
        )

    run = {
        "asOf": meta["asOf"],
        "vix": meta.get("vix") or 0.0,
        "regime": meta.get("regime") or "neutral",
        "cape": meta.get("cape") or 0.0,
        "buffett": int(meta.get("buffett") or 0),
        "cycle": meta.get("cycle") or "fair",
        "universe": 80,
        "support": len(modes.get("support") or []),
        "catalyst": len(modes.get("catalyst") or []),
        "fund": len(modes.get("fundamentals") or []),
        "pol": len(modes.get("politicians") or []),
        "trumpPosts": meta.get("trumpPosts") or 0,
        "trumpTickers": meta.get("trumpTickers")
        or len(modes.get("trump") or []),
        "athdipPass": len(ath_rows),
        "athdipLowestRsi4h": lowest_rsi or 0.0,
        "peadPass": len(modes.get("pead") or []),
        "spyYtd": next((b["ret"] for b in backtest if b.get("mode") == "SPY"), 0.0),
    }

    focus_js = []
    for p in picks:
        focus_js.append(
            "{"
            + f'kind:{js_str(p["kind"])},ticker:{js_str(p["ticker"])},'
            + f'signal:{js_str(p.get("signal") or "BUY")},'
            + f'price:{js_num(p.get("price"), 2)},'
            + f'tags:{js_str(p.get("tags"))},why:{js_str(p.get("why"))},'
            + f'athdip:{js_num(p.get("athdip"), 1)},'
            + f'pead:{js_num(p.get("pead"), 1)},'
            + f'support:{js_num(p.get("support"), 1)},'
            + f'catalyst:{js_num(p.get("catalyst"), 1)},'
            + f'fund:{js_num(p.get("fund"), 1)},'
            + f'pol:{js_num(p.get("pol"), 1)},'
            + f'trump:{js_num(p.get("trump"), 1)},'
            + f'rsi14:{js_num(p.get("rsi14"), 1)},'
            + f'rsi4h:{js_num(p.get("rsi4h"), 1)},'
            + f'sector:{js_str(p.get("sector"))},'
            + f'pe:{js_num(p.get("pe"), 1)},'
            + f'peg:{js_num(p.get("peg"), 2)},'
            + f'fcfy:{js_num(p.get("fcfy"), 1)},'
            + f'roe:{js_num(p.get("roe"), 1)}'
            + "}"
        )

    athdip_js = []
    for r in ath_rows:
        t = r["ticker"]
        athdip_js.append(
            "{"
            + f'ticker:{js_str(t)},price:{js_num(fnum(r.get("price")), 2)},'
            + f'ath:{js_num(info[t].get("ath"), 2)},'
            + f'pctAth:{js_num(fnum(r.get("pct_from_ath")), 2)},'
            + f'days:{int(float(r.get("days_since_ath") or 0))},'
            + f'rsi4h:{js_num(fnum(r.get("rsi_4h")), 1)},'
            + f'rsiD:{js_num(fnum(r.get("rsi14")), 1)},'
            + f'score:{js_num(fnum(r.get("composite")), 2)}'
            + "}"
        )

    def mode_table(name: str, n: int = 8) -> list[str]:
        rows = modes.get(name) or []
        sk = score_key(rows)
        out = []
        for r in top_rows(rows, n, sk):
            t = r["ticker"]
            if name == "support":
                out.append(
                    "{"
                    + f'ticker:{js_str(t)},price:{js_num(fnum(r.get("price")), 2)},'
                    + f'rsi:{js_num(fnum(r.get("rsi14")), 2)},'
                    + f'rsiBias:{js_str(r.get("rsi_bias") or "")},'
                    + f'rsiSlope:{js_num(fnum(r.get("rsi_slope")), 2)},'
                    + f'prox:{js_num(fnum(r.get("proximity")), 3)},'
                    + f'pct52h:{js_num(fnum(r.get("pct_from_52w_high")), 2)},'
                    + f'peg:{js_num(fnum(r.get("peg")), 3)},'
                    + f'fcfy:{js_num(fnum(r.get("fcf_yield")), 4)},'
                    + f'roe:{js_num(fnum(r.get("roe")), 3)},'
                    + f'score:{js_num(fnum(r.get(sk)), 2)}'
                    + "}"
                )
            elif name == "catalyst":
                out.append(
                    "{"
                    + f'ticker:{js_str(t)},price:{js_num(fnum(r.get("price")), 2)},'
                    + f'rsi:{js_num(fnum(r.get("rsi14")), 2)},'
                    + f'rsiBias:{js_str(r.get("rsi_bias") or "")},'
                    + f'rsiSlope:{js_num(fnum(r.get("rsi_slope")), 2)},'
                    + f'day:{js_num(fnum(r.get("day_return_pct")), 2)},'
                    + f'vol:{js_num(fnum(r.get("volume_ratio")), 2)},'
                    + f'pct52h:{js_num(fnum(r.get("pct_from_52w_high")), 2)},'
                    + f'score:{js_num(fnum(r.get(sk)), 2)}'
                    + "}"
                )
            elif name == "fundamentals":
                fcfy = fnum(r.get("fcf_yield"))
                roe = fnum(r.get("roe"))
                out.append(
                    "{"
                    + f'ticker:{js_str(t)},sector:{js_str((r.get("sector") or "")[:18])},'
                    + f'peg:{js_num(fnum(r.get("peg")), 2)},'
                    + f'fcfy:{js_num(fcfy, 4)},'
                    + f'roe:{js_num(roe, 3)},'
                    + f'score:{js_num(fnum(r.get(sk)), 2)}'
                    + "}"
                )
            elif name == "politicians":
                pols = (r.get("politicians") or "")[:42]
                out.append(
                    "{"
                    + f'ticker:{js_str(t)},score:{js_num(fnum(r.get(sk)), 2)},'
                    + f'buys:{int(float(r.get("buy_count") or 0))},'
                    + f'sells:{int(float(r.get("sell_count") or 0))},'
                    + f'filedAfter:{js_num(fnum(r.get("avg_filed_after")), 1)},'
                    + f'pols:{js_str(pols)},sources:{js_str(r.get("sources") or "")}'
                    + "}"
                )
            elif name == "trump":
                why = r.get("why") or ""
                ment = re.search(r"mentions=(\d+)", why)
                themes = r.get("themes") or ""
                if not themes:
                    m = re.search(r"themes=([^;]+)", why)
                    themes = m.group(1).strip() if m else ""
                out.append(
                    "{"
                    + f'ticker:{js_str(t)},score:{js_num(fnum(r.get(sk)), 2)},'
                    + f'mentions:{int(ment.group(1)) if ment else 0},'
                    + f'themes:{js_str(themes[:40])},sources:{js_str("news, truth, WH")}'
                    + "}"
                )
        return out

    bt_js = [
        "{"
        + f'mode:{js_str(b["mode"])},ret:{js_num(b["ret"], 2)},'
        + f'excess:{js_num(b["excess"], 2)},win:{js_num(b["win"], 1)},'
        + f'dd:{js_num(b["dd"], 2)},avg:{js_num(b["avg"], 1)}'
        + "}"
        for b in backtest
    ]

    truth = meta.get("truth") or 80
    news = meta.get("news") or 0
    wh = meta.get("wh") or 0

    sep = ",\n  "
    focus_block = sep.join(focus_js)
    athdip_block = sep.join(athdip_js) if athdip_js else ""
    bt_block = sep.join(bt_js) if bt_js else '{ mode: "SPY", ret: 0, excess: 0, win: 0, dd: 0, avg: 1 }'
    trump_block = sep.join(mode_table("trump", 10))
    support_block = sep.join(mode_table("support", 8))
    catalyst_block = sep.join(mode_table("catalyst", 8))
    pol_block = sep.join(mode_table("politicians", 8))
    fund_block = sep.join(mode_table("fundamentals", 8))

    return f"""import {{
  BarChart,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  IconButton,
  Pill,
  Row,
  Spacer,
  Stack,
  Stat,
  Table,
  Text,
  UsageBar,
  useState,
}} from "cursor/canvas";

// Auto-generated by scripts/build_screen_dashboard.py — do not edit by hand.
const RUN = {{
  asOf: {js_str(run["asOf"])},
  vix: {js_num(run["vix"], 2)},
  regime: {js_str(run["regime"])},
  cape: {js_num(run["cape"], 1)},
  buffett: {run["buffett"]},
  cycle: {js_str(run["cycle"])},
  universe: {run["universe"]},
  support: {run["support"]},
  catalyst: {run["catalyst"]},
  fund: {run["fund"]},
  pol: {run["pol"]},
  trumpPosts: {run["trumpPosts"]},
  trumpTickers: {run["trumpTickers"]},
  athdipPass: {run["athdipPass"]},
  athdipLowestRsi4h: {js_num(run["athdipLowestRsi4h"], 1)},
  peadPass: {run["peadPass"]},
  spyYtd: {js_num(run["spyYtd"], 2)},
  note: "Generated from output/screen_*.csv · watchlist only, not advice",
}};

const FOCUS = [
  {focus_block}
];

const ATHDIP = [
  {athdip_block}
];

const BACKTEST_YTD = [
  {bt_block}
];

const TRUMP_SOURCES = [
  {{ label: "Truth Social", count: {truth} }},
  {{ label: "Google News", count: {news} }},
  {{ label: "White House", count: {wh} }},
];

const TRUMP = [
  {trump_block}
];

const SUPPORT = [
  {support_block}
];

const CATALYST = [
  {catalyst_block}
];

const POLITICIANS = [
  {pol_block}
];

const FUNDAMENTALS = [
  {fund_block}
];

function biasTone(bias: string): "success" | "warning" | "danger" | "info" | "neutral" {{
  if (bias === "oversold" || bias === "toward oversold") return "success";
  if (bias === "overbought" || bias === "toward overbought") return "warning";
  return "neutral";
}}

function focusTone(kind: string): "success" | "warning" | "danger" | "info" | "neutral" {{
  if (kind === "conflict") return "danger";
  if (kind === "intersection" || kind === "confirm" || kind === "athdip" || kind === "pead") return "success";
  if (kind === "single") return "info";
  return "neutral";
}}

function dash(n: number | null | undefined, digits = 1): string {{
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}}

function polTone(buys: number, sells: number): "success" | "warning" | "danger" | "info" | "neutral" {{
  if (buys > sells) return "success";
  if (sells > buys) return "warning";
  return "neutral";
}}

function trumpTone(score: number): "success" | "warning" | "info" | "neutral" {{
  if (score >= 85) return "success";
  if (score >= 70) return "info";
  return "neutral";
}}

function formatBias(bias: string, slope: number): string {{
  const arrow = slope < -2 ? " ↓" : slope > 2 ? " ↑" : "";
  return `${{bias}}${{arrow}}`;
}}

function pct(x: number): string {{
  return `${{(x * 100).toFixed(1)}}%`;
}}

/** Click ? to reveal the definition in the tip Callout (canvas has no browser hover tooltips). */
function signalTone(sig: string): "success" | "warning" | "danger" | "info" | "neutral" {{
  if (sig === "BUY") return "success";
  if (sig === "SELL") return "danger";
  if (sig === "MIXED") return "warning";
  return "neutral";
}}

export default function ScreenDashboard() {{
  const [tip, setTip] = useState<string | null>(null);
  const showTip = (label: string, text: string) => setTip(`${{label}} — ${{text}}`);

  function th(label: string, tipText: string) {{
    return (
      <Row gap={{4}} align="center">
        <span>{{label}}</span>
        <IconButton
          size="sm"
          title={{tipText}}
          onClick={{() => showTip(label, tipText)}}
        >
          ?
        </IconButton>
      </Row>
    );
  }}

  function withTip(label: string, tipText: string, node: JSX.Element) {{
    return (
      <Stack gap={{4}}>
        <div
          onClick={{() => showTip(label, tipText)}}
          style={{{{ cursor: "pointer" }}}}
          title={{tipText}}
        >
          {{node}}
        </div>
        <IconButton
          size="sm"
          title={{tipText}}
          onClick={{() => showTip(label, tipText)}}
        >
          ?
        </IconButton>
      </Stack>
    );
  }}

  const srcTotal = TRUMP_SOURCES.reduce((a, s) => a + s.count, 0);
  const cross = FOCUS.filter((r) => r.kind !== "single");
  const singles = FOCUS.filter((r) => r.kind === "single");

  const crossHeaders = [
    th(
      "Kind",
      "Row type: conflict means modes disagree; intersection means modes agree; athdip or confirm means setup-driven signals",
    ),
    th("Ticker", "Stock ticker symbol"),
    th(
      "Signal",
      "Watchlist lean for this long-only screener: BUY = long setup; SELL = Congress sell-flow dominates without other long modes; MIXED = conflict (for example athdip vs Congress sells)",
    ),
    th(
      "Price",
      "Latest screen price (usually prior session close or last available quote)",
    ),
    th("RSI", "Daily fourteen-period relative strength index when available"),
    th("Why", "Reason this name was selected for the Today list"),
    th("Modes", "Which screener modes tagged this ticker"),
    th(
      "Ath",
      "Athdip-mode composite score (higher means deeper four-hour relative strength index and a fresher all-time-high setup)",
    ),
    th("Sup", "Support-mode composite score"),
    th(
      "Cat",
      "Catalyst-mode composite score (Yahoo news ± X-quoting headlines ± Polymarket ± 13F social)",
    ),
    th("Fund", "Fundamentals-mode composite score"),
    th(
      "Pol",
      "Politicians-mode score based on freshness-weighted United States Congress trading flow",
    ),
    th("Trump", "Trump-tracker theme score from Truth Social, news, and White House feeds"),
    th("Sector", "Company sector classification from Yahoo Finance"),
    th("PE", "Trailing price-to-earnings ratio"),
    th(
      "PEG",
      "Price/earnings-to-growth ratio (lower often means cheaper relative to expected growth)",
    ),
    th("FCF%", "Free-cash-flow yield as a percent of market value"),
    th("ROE%", "Return on equity as a percent"),
  ];
  const singleHeaders = [
    th("Mode", "Primary single-mode leader category"),
    th("Ticker", "Stock ticker symbol"),
    th(
      "Signal",
      "Watchlist lean: BUY = long setup; SELL = Congress sell-flow dominates; MIXED = conflicting modes",
    ),
    th(
      "Price",
      "Latest screen price (usually prior session close or last available quote)",
    ),
    th("RSI", "Daily fourteen-period relative strength index when available"),
    th("Why", "Reason this name is listed as a single-mode leader"),
    th("Lead", "Composite or mode score in the leading category"),
    th("Sector", "Company sector classification from Yahoo Finance"),
    th("PE", "Trailing price-to-earnings ratio"),
    th("PEG", "Price/earnings-to-growth ratio"),
    th("FCF%", "Free-cash-flow yield as a percent of market value"),
    th("ROE%", "Return on equity as a percent"),
  ];

  return (
    <Stack gap={{28}} style={{{{ maxWidth: 1100 }}}}>
      <Stack gap={{8}}>
        <H1>Screener dashboard</H1>
        <Text tone="secondary">
          All modes · SP500/{{RUN.universe}} · {{RUN.asOf}}
        </Text>
        <Text tone="secondary" style={{{{ fontSize: 12 }}}}>
          Click any ? to show that column or metric definition below (canvas does not show browser hover tooltips).
        </Text>
        {{tip ? (
          <Callout tone="info" title="Definition">
            <Stack gap={{8}}>
              <Text>{{tip}}</Text>
              <Button variant="secondary" onClick={{() => setTip(null)}}>
                Dismiss
              </Button>
            </Stack>
          </Callout>
        ) : null}}
      </Stack>

      <Grid columns={{4}} gap={{12}}>
        {{withTip(
          "VIX",
          "CBOE Volatility Index (^VIX) — market fear gauge. Risk-on at or below about 15; risk-off at or above about 25; mid range is neutral.",
          <Stat value={{RUN.vix.toFixed(1)}} label="VIX" />,
        )}}
        {{withTip(
          "Regime",
          "VIX-driven trading regime (risk-on / neutral / risk-off). Tightens or loosens support and catalyst filters and weights.",
          <Stat value={{RUN.regime}} label="Regime" tone="info" />,
        )}}
        {{withTip(
          "CAPE",
          "Shiller CAPE (cyclically adjusted price-to-earnings). Rough bands: under 20 cheap, 20–28 fair, over 28 expensive. Used with Buffett for the market-cycle label.",
          <Stat value={{String(RUN.cape)}} label="CAPE" tone="warning" />,
        )}}
        {{withTip(
          "Buffett",
          "Buffett Indicator ≈ total US equity market cap / GDP (percent). Rough bands: under 100% cheap, 100–140% fair, over 140% expensive. Label after the · is the combined cycle read with CAPE.",
          <Stat value={{`${{RUN.buffett}}%`}} label={{`Buffett · ${{RUN.cycle}}`}} tone="warning" />,
        )}}
      </Grid>

      <Grid columns={{6}} gap={{10}}>
        {{withTip(
          "Support",
          "Count of names that passed Support-mode filters in this screen run (near support / mean-reversion).",
          <Stat value={{String(RUN.support)}} label="Support" />,
        )}}
        {{withTip(
          "Catalyst",
          "Count of names that passed Catalyst-mode filters (news / earnings / momentum; social may include Polymarket).",
          <Stat value={{String(RUN.catalyst)}} label="Catalyst" />,
        )}}
        {{withTip(
          "Fundamentals",
          "Count of Fundamentals-mode leaders (growth, profitability, balance sheet, ROIC, valuation).",
          <Stat value={{String(RUN.fund)}} label="Fundamentals" />,
        )}}
        {{withTip(
          "Politicians",
          "Count of tickers with freshness-weighted United States Congress trading flow in Politicians mode.",
          <Stat value={{String(RUN.pol)}} label="Politicians" />,
        )}}
        {{withTip(
          "Trump",
          "Count of tickers mapped from Trump-tracker themes (Truth Social, Google News, White House feeds).",
          <Stat value={{String(RUN.trumpTickers)}} label="Trump" tone="info" />,
        )}}
        {{withTip(
          "Athdip",
          "Count of Athdip hard passes: recent multi-year all-time-high setup with four-hour relative strength index near oversold.",
          <Stat value={{String(RUN.athdipPass)}} label="Athdip" tone="success" />,
        )}}
        {{withTip(
          "PEAD",
          "Count of post-earnings-announcement-drift passes: EPS surprise ≥ 5% and a ≥ 3% excess reaction vs SPY within the last 15 sessions.",
          <Stat value={{String(RUN.peadPass)}} label="PEAD" tone="success" />,
        )}}
      </Grid>

      <Callout tone="info" title={{RUN.note}}>
        Rebuild: python3 scripts/build_screen_dashboard.py
      </Callout>

      <Stack gap={{10}}>
        <Row align="center" gap={{10}}>
          {{withTip(
            "Today",
            "Cross-mode intersections, conflicts, and single-mode leaders with fundamentals",
            <H2>Today</H2>,
          )}}
          <Pill
            tone="warning"
            title="Informational watchlist only — not investment advice"
            onClick={{() =>
              showTip(
                "Watchlist",
                "Informational watchlist only — not investment advice",
              )
            }}
          >
            watchlist · not advice
          </Pill>
        </Row>
        <Text tone="secondary">
          Intersections / conflicts first, then top single-mode leaders — with fundamentals.
        </Text>

        {{withTip(
          "Cross-mode",
          "Names tagged by more than one mode, or where modes disagree",
          <Text weight="semibold">Cross-mode intersections & conflicts</Text>,
        )}}
        <Table
          headers={{crossHeaders}}
          columnAlign={{["left","left","left","right","right","left","left","right","right","right","right","right","right","left","right","right","right","right"]}}
          rows={{cross.map((r) => [
            r.kind,
            r.ticker,
            r.signal || "BUY",
            dash(r.price, 2),
            dash(r.rsi14),
            r.why,
            r.tags,
            dash(r.athdip),
            dash(r.support),
            dash(r.catalyst),
            dash(r.fund),
            dash(r.pol),
            dash(r.trump),
            r.sector || "—",
            dash(r.pe),
            dash(r.peg, 2),
            dash(r.fcfy),
            dash(r.roe),
          ])}}
          rowTone={{cross.map((r) => signalTone(r.signal || "BUY"))}}
        />

        {{withTip(
          "Single-mode",
          "Top names that only lead in a single mode (not already listed in the cross-mode section)",
          <Text weight="semibold">Single-mode leaders</Text>,
        )}}
        <Table
          headers={{singleHeaders}}
          columnAlign={{["left","left","left","right","right","left","right","left","right","right","right","right"]}}
          rows={{singles.map((r) => {{
            const lead = r.support ?? r.catalyst ?? r.trump ?? r.pol ?? r.fund ?? null;
            return [
              r.tags.split(" · ")[0] || r.tags,
              r.ticker,
              r.signal || "BUY",
              dash(r.price, 2),
              dash(r.rsi14),
              r.why,
              dash(lead),
              r.sector || "—",
              dash(r.pe),
              dash(r.peg, 2),
              dash(r.fcfy),
              dash(r.roe),
            ];
          }})}}
          rowTone={{singles.map((r) => signalTone(r.signal || "BUY"))}}
        />
        <Text tone="secondary" style={{{{ fontSize: 12 }}}}>
          Source: output/today_focus.csv · {{RUN.asOf}}
        </Text>
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        <Row align="center" gap={{10}}>
          {{withTip(
            "Athdip",
            "Uptrend near a multi-year all-time high with a four-hour relative strength index dip near oversold",
            <H2>Athdip</H2>,
          )}}
          <Pill
            tone="success"
            title="Requires a recent multi-year ATH setup plus four-hour RSI near about 30"
            onClick={{() =>
              showTip(
                "Athdip filter",
                "Requires a recent multi-year ATH setup plus four-hour RSI near about 30",
              )
            }}
          >
            ATH setup + 4h RSI dip
          </Pill>
        </Row>
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Price", "Latest screen price (usually prior session close or last available quote)"),
            th("ATH", "Multi-year all-time high used for the athdip setup"),
            th("%ATH", "Percent below the all-time high (more negative means a deeper pullback)"),
            th("Days", "Trading days since the multi-year all-time high"),
            th("RSI 4h", "Minimum four-hour relative strength index over the lookback window (trigger when at or below about 31)"),
            th("RSI D", "Daily fourteen-period relative strength index"),
            th("Score", "Athdip-mode composite score"),
          ]}}
          columnAlign={{["left","right","right","right","right","right","right","right"]}}
          rows={{ATHDIP.map((r) => [
            r.ticker,
            dash(r.price, 2),
            dash(r.ath, 2),
            `${{dash(r.pctAth)}}%`,
            String(r.days),
            dash(r.rsi4h),
            dash(r.rsiD),
            dash(r.score),
          ])}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        <Row align="center" gap={{10}}>
          {{withTip(
            "Trump tracker",
            "Maps Truth Social, Google News, and White House items to liquid tickers by cashtag, company name, and policy theme",
            <H2>Trump tracker</H2>,
          )}}
          <Pill
            tone="warning"
            title="Freshness-weighted theme scores — not a trading signal by itself"
            onClick={{() =>
              showTip(
                "Trump themes",
                "Freshness-weighted theme scores — not a trading signal by itself",
              )
            }}
          >
            policy / themes
          </Pill>
        </Row>
        {{withTip(
          "Feed mix",
          "Share of Trump-tracker items by source: Truth Social mirror, Google News RSS, and White House RSS",
          <UsageBar
            total={{srcTotal || 1}}
            segments={{TRUMP_SOURCES.map((s) => ({{ id: s.label, value: s.count }}))}}
            topLeftLabel="Feed mix"
            topRightLabel={{`${{srcTotal}} items`}}
          />,
        )}}
        <BarChart
          categories={{TRUMP.slice(0, 10).map((r) => r.ticker)}}
          series={{[{{ name: "Trump score", data: TRUMP.slice(0, 10).map((r) => r.score), tone: "warning" }}]}}
          height={{200}}
        />
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Score", "Trump-tracker theme score from Truth Social, news, and White House feeds"),
            th("Mentions", "Count of matched posts or headlines mapped to this ticker"),
            th("Themes", "Policy or company themes that triggered the mapping"),
          ]}}
          columnAlign={{["left","right","right","left"]}}
          rows={{TRUMP.map((r) => [r.ticker, dash(r.score), String(r.mentions), r.themes])}}
          rowTone={{TRUMP.map((r) => trumpTone(r.score))}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        {{withTip(
          "YTD backtest",
          "Year-to-date paper returns by mode versus SPY (from the optional backtest script)",
          <H2>YTD backtest</H2>,
        )}}
        <BarChart
          categories={{BACKTEST_YTD.map((r) => r.mode)}}
          series={{[{{ name: "YTD return %", data: BACKTEST_YTD.map((r) => r.ret), tone: "info" }}]}}
          height={{200}}
        />
        <Table
          headers={{[
            th("Mode", "Backtest mode or strategy label"),
            th("Return %", "Year-to-date strategy return in percent"),
            th("Excess %", "Strategy return minus SPY over the same window"),
            th("Win %", "Share of rebalance periods with a positive return"),
            th("Max DD %", "Maximum peak-to-trough drawdown in percent"),
            th("Avg #", "Average number of holdings per rebalance"),
          ]}}
          columnAlign={{["left","right","right","right","right","right"]}}
          rows={{BACKTEST_YTD.map((r) => [
            r.mode,
            dash(r.ret, 2),
            dash(r.excess, 2),
            dash(r.win, 1),
            dash(r.dd, 2),
            dash(r.avg, 1),
          ])}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        {{withTip(
          "Politicians",
          "United States Congress STOCK Act disclosures (Capitol Trades plus House/Senate), freshness-weighted",
          <H2>Politicians</H2>,
        )}}
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Score", "Politicians-mode score from freshness-weighted United States Congress trading flow"),
            th("Buys", "Count of buy-side disclosures in the lookback"),
            th("Sells", "Count of sell-side disclosures in the lookback"),
            th("Filed after", "Average days between trade and filing (lower is fresher)"),
            th("Politicians", "Lawmakers associated with the matched trades"),
          ]}}
          columnAlign={{["left","right","right","right","right","left"]}}
          rows={{POLITICIANS.map((r) => [
            r.ticker,
            dash(r.score),
            String(r.buys),
            String(r.sells),
            dash(r.filedAfter),
            r.pols,
          ])}}
          rowTone={{POLITICIANS.map((r) => polTone(r.buys, r.sells))}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        {{withTip(
          "Support",
          "Near support or SMA pullback setups with VIX-regime gates",
          <H2>Support</H2>,
        )}}
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Price", "Latest screen price"),
            th("RSI", "Daily fourteen-period relative strength index"),
            th("Bias", "Whether RSI is bending toward oversold or overbought (arrow shows slope)"),
            th("Prox", "Proximity to support versus resistance (lower means closer to support)"),
            th("%52wH", "Percent from the fifty-two-week high"),
            th("PEG", "Price/earnings-to-growth ratio"),
            th("FCF", "Free-cash-flow yield"),
            th("ROE", "Return on equity"),
            th("Score", "Support-mode composite score"),
          ]}}
          columnAlign={{["left","right","right","left","right","right","right","right","right","right"]}}
          rows={{SUPPORT.map((r) => [
            r.ticker,
            dash(r.price, 2),
            dash(r.rsi),
            formatBias(r.rsiBias, r.rsiSlope ?? 0),
            dash(r.prox, 3),
            `${{dash(r.pct52h)}}%`,
            dash(r.peg, 2),
            r.fcfy == null ? "—" : pct(r.fcfy),
            r.roe == null ? "—" : pct(r.roe),
            dash(r.score, 2),
          ])}}
          rowTone={{SUPPORT.map((r) => biasTone(r.rsiBias))}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        {{withTip(
          "Catalyst",
          "News, earnings proximity, and momentum pops; social may include Polymarket odds",
          <H2>Catalyst</H2>,
        )}}
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Price", "Latest screen price"),
            th("RSI", "Daily fourteen-period relative strength index"),
            th("Bias", "Whether RSI is bending toward oversold or overbought (arrow shows slope)"),
            th("Day %", "One-day percent price change"),
            th("Vol×", "Volume versus recent average (above about 1.4 often counts as a catalyst signal)"),
            th("%52wH", "Percent from the fifty-two-week high"),
            th("Score", "Catalyst-mode composite (includes Yahoo news ± X-quoting headlines ± Polymarket social)"),
          ]}}
          columnAlign={{["left","right","right","left","right","right","right","right"]}}
          rows={{CATALYST.map((r) => [
            r.ticker,
            dash(r.price, 2),
            dash(r.rsi),
            formatBias(r.rsiBias, r.rsiSlope ?? 0),
            dash(r.day, 2),
            dash(r.vol, 2),
            `${{dash(r.pct52h)}}%`,
            dash(r.score, 2),
          ])}}
          rowTone={{CATALYST.map((r) => biasTone(r.rsiBias))}}
        />
      </Stack>

      <Divider />

      <Stack gap={{10}}>
        {{withTip(
          "Fundamentals",
          "Growth, profitability, balance sheet, capital efficiency, and valuation composite",
          <H2>Fundamentals</H2>,
        )}}
        <Table
          headers={{[
            th("Ticker", "Stock ticker symbol"),
            th("Sector", "Company sector classification from Yahoo Finance"),
            th("PEG", "Price/earnings-to-growth ratio (lower often means cheaper relative to expected growth)"),
            th("FCF yld", "Free-cash-flow yield"),
            th("ROE", "Return on equity"),
            th("Score", "Fundamentals-mode composite score"),
          ]}}
          columnAlign={{["left","left","right","right","right","right"]}}
          rows={{FUNDAMENTALS.map((r) => [
            r.ticker,
            r.sector,
            dash(r.peg, 2),
            r.fcfy == null ? "—" : pct(r.fcfy),
            r.roe == null ? "—" : pct(r.roe),
            dash(r.score, 2),
          ])}}
        />
      </Stack>

      <Card>
        <CardHeader>Reproduce</CardHeader>
        <CardBody>
          <Stack gap={{6}}>
            <Text style={{{{ fontFamily: "monospace", fontSize: 12 }}}}>
              stock-screener --mode all --universe sp500 --limit 80 --no-reddit --no-grok --no-trump-x
            </Text>
            <Text style={{{{ fontFamily: "monospace", fontSize: 12 }}}}>
              python3 scripts/build_screen_dashboard.py
            </Text>
            <Spacer height={{4}} />
            <Text tone="secondary" style={{{{ fontSize: 13 }}}}>
              Project skill: .cursor/skills/daily-screen
            </Text>
          </Stack>
        </CardBody>
      </Card>
    </Stack>
  );
}}
"""


def update_wiki(picks: list[dict[str, Any]], as_of: str) -> None:
    WIKI_MD.parent.mkdir(parents=True, exist_ok=True)
    focus_rows = []
    for p in picks:
        focus_rows.append(
            [
                p["kind"],
                p["ticker"],
                p.get("signal") or "BUY",
                f"{p['price']:.2f}" if p.get("price") is not None else "—",
                f"{p['rsi14']:.1f}" if p.get("rsi14") is not None else "—",
                p.get("why") or "",
                p.get("tags") or "",
                p.get("sector") or "—",
                f"{p['pe']:.1f}" if p.get("pe") is not None else "—",
                f"{p['peg']:.2f}" if p.get("peg") is not None else "—",
                f"{p['fcfy']:.1f}%" if p.get("fcfy") is not None else "—",
                f"{p['roe']:.1f}%" if p.get("roe") is not None else "—",
            ]
        )
    headers = [
        "Kind",
        "Ticker",
        "Signal",
        "Price",
        "RSI",
        "Why",
        "Modes",
        "Sector",
        "PE",
        "PEG",
        "FCF%",
        "ROE%",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in focus_rows:
        lines.append("| " + " | ".join(row) + " |")
    focus_md = "\n".join(lines)

    section = f"""# Screener dashboard

**As of:** {as_of}

## Today's focus

Watchlist only — not investment advice.

{focus_md}

Source: `output/today_focus.csv` · rebuild with `python3 scripts/build_screen_dashboard.py`

---
"""
    if WIKI_MD.exists():
        old = WIKI_MD.read_text(encoding="utf-8")
        # Replace leading dashboard header if present, else prepend focus
        if "## Today's focus" in old:
            # keep rest after first --- following Today's focus if possible
            rest = re.split(r"\n---\n", old, maxsplit=1)
            tail = rest[1] if len(rest) > 1 else ""
            # drop old support sections that follow? Keep publish_wiki structure if present
            if "## Support" in old:
                idx = old.find("## Support")
                tail = old[idx:]
            WIKI_MD.write_text(section + "\n" + tail, encoding="utf-8")
        else:
            WIKI_MD.write_text(section + "\n" + old, encoding="utf-8")
    else:
        WIKI_MD.write_text(section, encoding="utf-8")


def default_backtest() -> list[dict[str, Any]]:
    if BACKTEST_JSON.exists():
        return json.loads(BACKTEST_JSON.read_text(encoding="utf-8"))
    # Last known YTD sketch (update by writing output/backtest_ytd_summary.json)
    return [
        {"mode": "trump*", "ret": 60.68, "excess": 47.97, "win": 58.3, "dd": -11.8, "avg": 10.0},
        {"mode": "athdip†", "ret": 39.57, "excess": 26.85, "win": 22.4, "dd": -11.56, "avg": 0.7},
        {"mode": "catalyst", "ret": 16.5, "excess": 3.79, "win": 50.0, "dd": -7.23, "avg": 9.0},
        {"mode": "support", "ret": 16.1, "excess": 3.39, "win": 52.8, "dd": -7.58, "avg": 10.0},
        {"mode": "value", "ret": 1.36, "excess": -11.35, "win": 61.1, "dd": -16.7, "avg": 7.0},
        {"mode": "SPY", "ret": 12.71, "excess": 0.0, "win": 0.0, "dd": 0.0, "avg": 1.0},
    ]


def main() -> None:
    modes = {
        "support": read_df(OUTPUT / "screen_support.csv"),
        "catalyst": read_df(OUTPUT / "screen_catalyst.csv"),
        "fundamentals": read_df(OUTPUT / "screen_fundamentals.csv"),
        "politicians": read_df(OUTPUT / "screen_politicians.csv"),
        "trump": read_df(OUTPUT / "screen_trump.csv"),
        "athdip": read_df(OUTPUT / "screen_athdip.csv"),
        "pead": read_df(OUTPUT / "screen_pead.csv"),
    }
    if not any(modes.values()):
        raise SystemExit("No output/screen_*.csv found. Run stock-screener --mode all first.")

    meta = parse_run_meta(OUTPUT / "run_today.log")
    # Prefer CSV mtime date if log missing asOf
    for name in ("support", "athdip", "catalyst"):
        p = OUTPUT / f"screen_{name}.csv"
        if p.exists():
            meta["asOf"] = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            break

    info = build_index(modes)

    # Fallback trigger_date/price from backtest signals when screen CSV lacks them
    sig_path = OUTPUT / "athdip_backtest_signals.csv"
    if sig_path.exists():
        sig_rows = read_df(sig_path)
        latest: dict[str, dict[str, str]] = {}
        for r in sig_rows:
            t = r.get("ticker") or ""
            td = (r.get("trigger_date") or r.get("asof_date") or "")[:10]
            if not t or not td:
                continue
            prev = latest.get(t)
            if prev is None or td >= (prev.get("trigger_date") or "")[:10]:
                latest[t] = r
        for t, r in latest.items():
            if t not in info:
                continue
            if not info[t].get("trigger_date"):
                info[t]["trigger_date"] = (r.get("trigger_date") or "")[:10]
            if info[t].get("trigger_price") is None:
                info[t]["trigger_price"] = fnum(r.get("price"))

    picks = pick_focus(modes, info, as_of=str(meta["asOf"]))

    # Fill missing fundamentals for focus tickers
    need = [
        p["ticker"]
        for p in picks
        if p.get("pe") is None or p.get("peg") is None or p.get("price") is None
    ]
    yahoo = enrich_fundamentals(need)
    for p in picks:
        y = yahoo.get(p["ticker"]) or {}
        for k in ("sector", "pe", "peg", "fcfy", "roe", "price"):
            if p.get(k) is None and y.get(k) is not None:
                p[k] = y[k]
        # normalize rounded fields
        for k, d in (("price", 2), ("athdip", 1), ("pead", 1), ("support", 1), ("catalyst", 1), ("fund", 1), ("pol", 1), ("trump", 1), ("rsi4h", 1), ("pe", 1), ("peg", 2), ("fcfy", 1), ("roe", 1)):
            p[k] = round_or_none(p.get(k), d)

    focus_path = OUTPUT / "today_focus.csv"
    write_focus_csv(picks, focus_path)
    print(f"Wrote {focus_path} ({len(picks)} rows)")

    update_wiki(picks, meta["asOf"])
    print(f"Updated {WIKI_MD}")

    canvas = render_canvas(meta, modes, picks, info, default_backtest())
    out_canvas = _canvas_out()
    out_canvas.parent.mkdir(parents=True, exist_ok=True)
    out_canvas.write_text(canvas, encoding="utf-8")
    print(f"Wrote {out_canvas}")

    DOCS_CANVAS.parent.mkdir(parents=True, exist_ok=True)
    DOCS_CANVAS.write_text(canvas, encoding="utf-8")
    print(f"Wrote {DOCS_CANVAS}")

    print("\nToday's focus:")
    for p in picks:
        sig = p.get("signal") or "BUY"
        rsi = p.get("rsi14")
        rsi_s = f" RSI={rsi:.0f}" if rsi is not None else ""
        print(f"  [{p['kind']}] {p['ticker']} · {sig}{rsi_s}: {p['why']}")

    # New-symbol alerts vs previous snapshot (stdout + optional desktop/Slack)
    try:
        import importlib.util

        alert_path = ROOT / "scripts" / "alert_new_signals.py"
        spec = importlib.util.spec_from_file_location("alert_new_signals", alert_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print("\n--- New-signal check ---")
            mod.run(desktop=True, slack=True, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        print(f"New-signal alert skipped: {exc}")


if __name__ == "__main__":
    main()
