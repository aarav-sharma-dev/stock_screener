"""Multi-quarter 13F history downloader with CUSIP-based ticker mapping.

Downloads all available 13F-HR filings for a curated set of low-turnover,
high-conviction managers. Filters out pod shops and high-turnover quant books.
Caches to ``output/institutions_13f_history.csv``.

Each row: manager_name, period_date, filing_date, cusip, ticker,
           shares, value_k, portfolio_weight_pct, shares_delta_pct
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, timedelta
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# SEC fair access.
USER_AGENT = os.environ.get(
    "SCREENER_SEC_USER_AGENT",
    "stock-screener/0.1 research@example.com",
)

_13F_FORMS = {"13F-HR", "13F-HR/A"}
SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov/Archives/edgar/data"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def _cik(n: int) -> str:
    """Format an integer CIK as a zero-padded 10-digit string."""
    return f"{n:010d}"


# ---- Managers: name → CIK (zero-padded to 10 digits) ----
# Only low-turnover, concentrated, long-biased fundamental managers
# whose positions survive the 45-day filing lag.
#
# CIK values confirmed against data.sec.gov submissions names:
#   Berkshire Hathaway, Pershing Square, Appaloosa LP,
#   Coatue Management, Baupost Group, Duquesne Family Office.
ELIGIBLE_MANAGERS: dict[str, str] = {
    "Berkshire": _cik(1067983),
    "Pershing": _cik(1336528),
    "Appaloosa": _cik(1656456),
    "Coatue": _cik(1135730),
    "Baupost": _cik(1061768),
    "Duquesne": _cik(1536411),
}

# ---- Pod / quant books explicitly excluded ----
EXCLUDED = {"Citadel", "Renaissance", "Bridgewater", "Millennium", "Point72"}

# ---- Manual CUSIP→ticker overrides for common splits ----
CUSIP_OVERRIDES: dict[str, str] = {
    "02079K305": "GOOGL",  # Alphabet CL A
    "02079K107": "GOOG",   # Alphabet CL C
    "084670702": "BRK-A",  # Berkshire CL A
    "084670207": "BRK-B",  # Berkshire CL B
    "594918104": "MSFT",
    "037833100": "AAPL",
    "17275R102": "CSCO",
    "457187106": "INTC",
    "594918104": "MSFT",
}

_NORM_NOISE = (
    " inc", " corp", " corporation", " co", " ltd", " plc",
    " class a", " class b", " class c", " class d",
    " ordinary shares", " common stock", " com",
)


@dataclass
class ManagerHolding:
    """A single row from a 13F information table."""
    cusip: str
    ticker: str = ""
    shares: float = 0.0
    value_k: float = 0.0
    portfolio_weight_pct: float = 0.0
    shares_delta_pct: float | None = None  # vs prior period


@dataclass
class ManagerFiling:
    """All holdings from one 13F-HR filing for one manager."""
    manager: str
    cik: str
    period_date: str       # quarter-end (report calendar or JUN-30 style)
    filing_date: str        # SEC filing date
    accession: str          # accession number stripped of dashes
    holdings: list[ManagerHolding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Ticker / CUSIP map
# ---------------------------------------------------------------------------
FIGI_URL = "https://api.openfigi.com/v3/mapping"
CUSIP_CACHE_PATH = Path("output/cusip_ticker_map.json")
# OpenFIGI allows 10 jobs/request and 25 requests/minute without an API key.
_FIGI_BATCH = 10
_FIGI_PAUSE = 2.6


def _pick_figi_ticker(hits: list[dict]) -> str:
    """Prefer the US composite equity listing from an OpenFIGI result."""
    equities = [
        h for h in hits
        if h.get("marketSector") == "Equity" and h.get("ticker")
    ]
    if not equities:
        return ""
    us = [h for h in equities if h.get("exchCode") == "US"]
    chosen = (us or equities)[0]
    return str(chosen["ticker"]).upper().replace("/", "-").replace(".", "-")


def _load_cusip_cache() -> dict[str, str]:
    if not CUSIP_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(CUSIP_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k).upper(): str(v).upper() for k, v in raw.items() if k}


def _save_cusip_cache(cache: dict[str, str]) -> None:
    CUSIP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSIP_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def resolve_cusips(cusips: list[str]) -> dict[str, str]:
    """Map CUSIPs to tickers. Overrides win; OpenFIGI fills the rest.

    Empty-string cache entries are remembered misses and are not retried.
    """
    cache = _load_cusip_cache()
    out: dict[str, str] = dict(CUSIP_OVERRIDES)
    out.update({k: v for k, v in cache.items() if v})
    missing = []
    seen: set[str] = set()
    for cusip in cusips:
        key = str(cusip or "").upper().strip()
        if len(key) != 9 or key in out or key in cache or key in seen:
            continue
        seen.add(key)
        missing.append(key)
    for i in range(0, len(missing), _FIGI_BATCH):
        batch = missing[i:i + _FIGI_BATCH]
        try:
            resp = requests.post(
                FIGI_URL,
                headers={"Content-Type": "application/json"},
                json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                timeout=30,
            )
        except Exception as exc:
            logger.warning("OpenFIGI request failed: %s", exc)
            break
        if resp.status_code != 200:
            logger.warning("OpenFIGI HTTP %s", resp.status_code)
            break
        try:
            payload = resp.json()
        except Exception:
            break
        for cusip, item in zip(batch, payload):
            hits = item.get("data") if isinstance(item, dict) else None
            ticker = _pick_figi_ticker(hits or [])
            cache[cusip] = ticker
            if ticker:
                out[cusip] = ticker
        _save_cusip_cache(cache)
        if i + _FIGI_BATCH < len(missing):
            time.sleep(_FIGI_PAUSE)
    logger.info("CUSIP map: %d tickers (%d new lookups)", len(out), len(missing))
    return out


def _dollar_multiplier(values: list[float], shares: list[float]) -> float:
    """Return 1000 when a filing still reports value in thousands of dollars.

    Dollar-reported books imply a share price. Thousands-reported books
    imply that price divided by 1000, so the median lands well under $2.
    """
    implied = [v / s for v, s in zip(values, shares) if s > 0 and v > 0]
    if not implied:
        return 1.0
    if float(np.median(implied)) < 2.0:
        return 1000.0
    return 1.0


def notes_for_tickers(
    tickers: list[str],
    df: pd.DataFrame | None = None,
    *,
    as_of: str | pd.Timestamp | None = None,
) -> dict[str, str]:
    """One-line 13F note for alert tickers.

    Uses the latest filing already public for each manager. A ticker is
    tagged when two or more managers hold it, or one manager holds it inside
    that manager's top 10 weights.
    """
    if df is None:
        df = load_cache()
    if df is None or df.empty or not tickers:
        return {}
    work = df.copy()
    work["filing_date"] = pd.to_datetime(work["filing_date"])
    work["period_date"] = pd.to_datetime(work["period_date"])
    stamp = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today().normalize()
    work = work[work["filing_date"] <= stamp]
    work["ticker"] = work["ticker"].fillna("").astype(str).str.upper()
    work = work[work["ticker"] != ""]
    if work.empty:
        return {}
    latest_period = work.groupby("manager")["period_date"].transform("max")
    latest = work[work["period_date"] == latest_period]
    weight_col = "portfolio_weight_pct"
    agg = latest.groupby(["manager", "ticker"], as_index=False).agg(
        weight=(weight_col, "sum"),
        filed=("filing_date", "max"),
    )
    top = (
        agg.sort_values(["manager", "weight", "ticker"], ascending=[True, False, True])
        .groupby("manager", as_index=False)
        .head(10)
    )
    top = top.assign(top10=True)
    agg = agg.merge(top[["manager", "ticker", "top10"]], on=["manager", "ticker"], how="left")
    agg["top10"] = agg["top10"].eq(True)
    want = {str(t).upper() for t in tickers}
    notes: dict[str, str] = {}
    for ticker, rows in agg.groupby("ticker"):
        if ticker not in want:
            continue
        rows = rows.sort_values("weight", ascending=False)
        top = rows[rows["top10"]]
        if len(rows) < 2 and top.empty:
            continue
        show = rows if len(rows) >= 2 else top
        parts = [f"{row.manager} {row.weight:.0f}%" for row in show.head(3).itertuples()]
        filed = pd.Timestamp(show["filed"].max()).strftime("%Y-%m-%d")
        notes[ticker] = f"13F: {', '.join(parts)} (filed {filed})"
    return notes


# ---------------------------------------------------------------------------
# Low-level SEC helpers
# ---------------------------------------------------------------------------
def _sec_get(url: str, *, timeout: float = 30.0) -> requests.Response | None:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug("SEC %s -> HTTP %s", url, resp.status_code)
            return None
        return resp
    except Exception as exc:
        logger.debug("SEC request failed %s: %s", url, exc)
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower() if tag else ""


# ---------------------------------------------------------------------------
# Parse information table XML
# ---------------------------------------------------------------------------
def _parse_infotable(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    rows: list[dict] = []
    for node in root.iter():
        if _local(node.tag) != "infotable":
            continue
        rec: dict = {}
        for ch in node.iter():
            ln = _local(ch.tag)
            v = (ch.text or "").strip()
            if ln == "nameofissuer":
                rec["issuer"] = v
            elif ln == "cusip":
                rec["cusip"] = v.upper()
            elif ln == "value":
                rec["value"] = _safe_float(v)
            elif ln == "sshprnamt":
                rec["shares"] = _safe_float(v)
            elif ln == "putcall":
                rec["putcall"] = v
        if rec.get("putcall"):
            continue  # skip options
        if rec.get("issuer"):
            rows.append(rec)
    return rows


def _safe_float(v: str) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Filing metadata from submissions JSON
# ---------------------------------------------------------------------------
def _latest_13f_entries(
    cik: str, *, max_filings: int | None = None
) -> list[tuple[str, str, str, str]]:
    """Return [(accession_no_dashes, filing_date, report_date, primary_xml_url), ...]
    sorted from most recent to oldest.
    """
    url = f"{SEC_BASE}/submissions/CIK{cik}.json"
    resp = _sec_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        recent = data["filings"]["recent"]
    except (KeyError, TypeError, ValueError):
        return []
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    filed = recent.get("filingDate") or []
    rpt = recent.get("reportDate") or []
    raw: list[tuple[str, str, str]] = []
    for i, form in enumerate(forms):
        if str(form).upper() in _13F_FORMS:
            acc = str(accs[i]).replace("-", "") if i < len(accs) else ""
            fd = str(filed[i]) if i < len(filed) else ""
            rd = str(rpt[i]) if i < len(rpt) else ""
            raw.append((acc, fd, rd))
    # Deduplicate
    seen_acc: set[str] = set()
    deduped: list[tuple[str, str, str]] = []
    for a, f, r in raw:
        if a not in seen_acc:
            seen_acc.add(a)
            deduped.append((a, f, r))

    if max_filings:
        deduped = deduped[:max_filings]

    # Resolve holdings XML only for the filings we will keep.
    entries: list[tuple[str, str, str, str]] = []
    for acc, fd, rd in deduped:
        xml_url = _resolve_primary_xml(cik, acc)
        entries.append((acc, fd, rd, xml_url))
    return entries


def _resolve_primary_xml(cik: str, accession: str) -> str:
    """Return the holdings XML URL for this accession.

    ``primary_doc.xml`` is the 13F cover sheet and has no positions.
    The holdings live in ``infotable.xml`` or another non-cover XML.
    """
    int_cik = int(cik)
    base = f"{EDGAR_BASE}/{int_cik}/{accession}"
    idx = _sec_get(f"{base}/index.json")
    names: list[str] = []
    if idx:
        try:
            names = [
                str(it.get("name", ""))
                for it in idx.json()["directory"]["item"]
            ]
        except Exception:
            names = []
    xmls = [n for n in names if n.lower().endswith(".xml")]
    ranked = sorted(
        xmls,
        key=lambda n: (
            0 if "infotable" in n.lower() or "informationtable" in n.lower() else 1,
            1 if "primary" in n.lower() or "xslform" in n.lower() else 0,
        ),
    )
    if not ranked:
        ranked = ["infotable.xml"]
    for name in ranked:
        url = f"{base}/{name}"
        raw = _sec_get(url)
        if raw and _parse_infotable(raw.content):
            return url
    return ""


# ---------------------------------------------------------------------------
# Fetch XML holdings for one filing
# ---------------------------------------------------------------------------
def _fetch_filing_holdings_from_url(xml_url: str, *, pause: float = 0.15
                                    ) -> list[dict]:
    """Fetch and parse holdings from a primary_doc.xml URL."""
    if not xml_url:
        return []
    time.sleep(pause)
    raw = _sec_get(xml_url)
    if not raw:
        return []
    return _parse_infotable(raw.content)


# ---------------------------------------------------------------------------
# Main: download all filings and return a DataFrame
# ---------------------------------------------------------------------------
def download_all_13f(
    managers: dict[str, str] | None = None,
    *,
    pause: float = 0.15,
    max_quarters: int | None = None,
) -> pd.DataFrame:
    """Download 13F history for each manager, return unified DataFrame.

    Parameters
    ----------
    managers : dict[str, str] | None
        name -> CIK. Defaults to ELIGIBLE_MANAGERS.
    pause : float
        SEC rate-limit pause between requests (sec).
    max_quarters : int | None
        Limit to the N most recent quarters per manager.

    Returns
    -------
    pd.DataFrame with columns:
        manager, period_date, filing_date, accession, cusip, issuer, ticker,
        shares, value_reported, value_usd, portfolio_weight_pct, shares_delta_pct
    """
    managers = managers or ELIGIBLE_MANAGERS

    records: list[dict] = []
    for mgr, cik in managers.items():
        if mgr in EXCLUDED:
            logger.info("Skipping excluded manager %s", mgr)
            continue

        filings = _latest_13f_entries(cik, max_filings=max_quarters)

        logger.info("13F %s (CIK %s): %d filings", mgr, cik, len(filings))
        for accession, filing_date, period_date, xml_url in filings:
            rows = _fetch_filing_holdings_from_url(xml_url, pause=pause)
            reported = [float(r.get("value") or 0.0) for r in rows]
            share_counts = [float(r.get("shares") or 0.0) for r in rows]
            scale = _dollar_multiplier(reported, share_counts)
            values = [v * scale for v in reported]
            total_val = sum(values)
            for raw, value in zip(rows, values):
                records.append({
                    "manager": mgr,
                    "period_date": period_date,
                    "filing_date": filing_date,
                    "accession": accession,
                    "cusip": str(raw.get("cusip") or "").upper(),
                    "issuer": raw.get("issuer") or "",
                    "ticker": "",
                    "shares": float(raw.get("shares") or 0.0),
                    "value_reported": float(raw.get("value") or 0.0),
                    "value_usd": value,
                    "portfolio_weight_pct": (value / total_val * 100) if total_val > 0 else 0.0,
                    "shares_delta_pct": None,
                })

            logger.info(
                "   %s -> %s: %d holdings, $%.0f%s",
                period_date, filing_date, len(rows), total_val,
                " (reported in thousands)" if scale == 1000 else "",
            )

    if not records:
        logger.warning("No 13F records downloaded.")
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["period_date"] = pd.to_datetime(df["period_date"])
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    # Prefer the full information table when a later amendment only lists a few lines.
    counts = df.groupby(["manager", "period_date", "accession"], as_index=False).agg(
        n=("cusip", "size"),
        filing_date=("filing_date", "max"),
    )
    best = (
        counts.sort_values(["manager", "period_date", "n", "filing_date"])
        .groupby(["manager", "period_date"], as_index=False)
        .tail(1)
    )
    df = df.merge(
        best[["manager", "period_date", "accession"]],
        on=["manager", "period_date", "accession"],
        how="inner",
    )

    cusip_map = resolve_cusips(df["cusip"].dropna().unique().tolist())
    df["ticker"] = df["cusip"].map(lambda c: cusip_map.get(str(c).upper(), ""))

    df = df.sort_values(["manager", "period_date", "cusip"]).reset_index(drop=True)
    df["shares_delta_pct"] = (
        df.groupby(["manager", "cusip"])["shares"]
        .pct_change(fill_method=None)
    )
    df["shares_delta_pct"] = df["shares_delta_pct"].clip(-1.0, 3.0)
    mapped = int(df["ticker"].fillna("").astype(bool).sum())
    logger.info("Ticker coverage: %d / %d rows", mapped, len(df))
    return df


# ---------------------------------------------------------------------------
# Cache / load helpers
# ---------------------------------------------------------------------------
CACHE_PATH = Path("output/institutions_13f_history.csv")


def refresh_cache(managers: dict[str, str] | None = None, *,
                  pause: float = 0.15, max_quarters: int | None = None) -> pd.DataFrame:
    """Download fresh 13F history and overwrite the cache file."""
    df = download_all_13f(managers, pause=pause, max_quarters=max_quarters)
    if df.empty:
        logger.warning("No data -- cache not updated.")
        return df
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CACHE_PATH, index=False)
    logger.info("Wrote %d rows -> %s", len(df), CACHE_PATH)
    return df


def load_cache() -> pd.DataFrame:
    """Load cached 13F history."""
    if not CACHE_PATH.exists():
        return pd.DataFrame()
    return pd.read_csv(CACHE_PATH, parse_dates=["period_date", "filing_date"])


# Quarter-end + 45 days. Weekend deadlines roll to Monday.
_13F_DEADLINES = ((2, 14), (5, 15), (8, 14), (11, 14))


def _next_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def refresh_due(today: date | None = None) -> date | None:
    """Return the 13F deadline when today is the first weekday after it.

    The statutory dates are February 14, May 15, August 14, and November 14.
    A Saturday or Sunday deadline is treated as the following Monday, and the
    refresh runs on the next weekday after that.
    """
    today = today or date.today()
    for month, day in _13F_DEADLINES:
        deadline = _next_weekday(date(today.year, month, day))
        if today == _next_weekday(deadline + timedelta(days=1)):
            return deadline
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
    )
    import sys
    args = [a for a in sys.argv[1:] if a != "--if-deadline"]
    if "--if-deadline" in sys.argv:
        due = refresh_due()
        if due is None:
            print("Not a post-13F refresh day.")
            raise SystemExit(0)
        print(f"13F deadline {due.isoformat()} has passed. Refreshing holdings.")
    max_q = int(args[0]) if args else None
    df = refresh_cache(max_quarters=max_q)
    if not df.empty:
        print(f"\nDownloaded {len(df)} rows across {df['manager'].nunique()} managers")
        print(df.groupby("manager").agg(
            filings=("period_date", "nunique"),
            positions=("cusip", "nunique"),
        ).to_string())