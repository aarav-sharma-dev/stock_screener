from __future__ import annotations

import logging
import re
from io import StringIO

import pandas as pd
import requests

from stock_screener.config import FALLBACK_UNIVERSE

logger = logging.getLogger(__name__)

# When --limit truncates an alphabetical SP500 list, keep mega-caps in the scan.
# (Alphabetical A…CBRE @ limit=80 previously dropped NVDA at index ~339.)
# Liquid AI, semiconductor, energy, and nuclear names the S&P 500 does not
# hold. Appended to every athdip scan. A name that later joins the index is
# a harmless duplicate.
ATHDIP_THEME_EXTRAS = (
    # AI and AI infrastructure
    "AI",
    "ALAB",
    "ARM",
    "CRDO",
    "CRWV",
    "APLD",
    "BBAI",
    "CORZ",
    "IONQ",
    "IREN",
    "MDB",
    "NBIS",
    "NET",
    "PATH",
    "SNOW",
    "WULF",
    # Semiconductors and equipment
    "AAOI",
    "ACLS",
    "ACMR",
    "AEHR",
    "AMBA",
    "AMKR",
    "ASML",
    "ASX",
    "CAMT",
    "CLS",
    "COHU",
    "CRUS",
    "DIOD",
    "ENTG",
    "FN",
    "FORM",
    "GFS",
    "ICHR",
    "KLIC",
    "LSCC",
    "MKSI",
    "MTSI",
    "MXL",
    "NVTS",
    "ONTO",
    "PLAB",
    "POWI",
    "PSTG",
    "QRVO",
    "RMBS",
    "SANM",
    "SIMO",
    "SLAB",
    "SMTC",
    "STM",
    "TSEM",
    "TSM",
    "UCTT",
    "UMC",
    "VECO",
    "VICR",
    # Energy: integrated oils, Canadian producers, US names outside the index
    "AMR",
    "AR",
    "BP",
    "BTU",
    "CEIX",
    "CNQ",
    "CTRA",
    "CVE",
    "EC",
    "ENB",
    "EQNR",
    "HCC",
    "IMO",
    "LNG",
    "OVV",
    "PBR",
    "RRC",
    "SHEL",
    "SU",
    "TRP",
    "TTE",
    # Nuclear utilities, SMR developers, and uranium
    "BWXT",
    "CCJ",
    "DNN",
    "LEU",
    "NNE",
    "NXE",
    "OKLO",
    "SMR",
    "TLN",
    "UEC",
    "UUUU",
    # Cooling outside the index. VRT, FIX, EME, and PWR are already in the S&P.
    "MOD",
    # Unlevered AI and semiconductor ETFs. Daily-reset funds (SOXL, USD) are
    # left out: a 4h RSI dip there is not the same setup as on a stock.
    "AIQ",
    "ARTY",
    "BOTZ",
    "CHAT",
    "FTXL",
    "IRBO",
    "PSI",
    "QTUM",
    "ROBO",
    "SMH",
    "SOXQ",
    "SOXX",
    "XSD",
)

PRIORITY_LIQUID_TICKERS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "AVGO",
    "TSLA",
    "BRK-B",
    "JPM",
    "V",
    "MA",
    "XOM",
    "UNH",
    "LLY",
    "JNJ",
    "WMT",
    "PG",
    "HD",
    "COST",
    "AMD",
    "NFLX",
    "CRM",
    "ORCL",
]
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
USER_AGENT = (
    "stock-screener/0.1 (+https://github.com/local/stock-screener; research use)"
)

# Common non-equity / junk suffixes in free symbol directories
_SKIP_SUFFIXES = (
    "W",
    "R",
    "U",
    "V",
    "P",
    "Q",
)  # warrants/rights/units often end this way when len>4 — filtered separately
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-")


def _is_common_stock_symbol(symbol: str) -> bool:
    s = _normalize_symbol(symbol)
    if not _SYMBOL_RE.match(s):
        return False
    if any(ch.isdigit() for ch in s):
        return False
    # Rights / units / warrants often end in R/U/W (AACBR, AACBU, …)
    if len(s) >= 5 and s[-1] in {"W", "R", "U", "V"}:
        return False
    if s.endswith(("-WT", "-W", "-U", "-R", "-P")):
        return False
    return True


def _name_looks_like_equity(name: str) -> bool:
    n = (name or "").lower()
    blocked = (
        " etf",
        "etf ",
        "warrant",
        " right",
        "rights",
        " unit",
        "units",
        "preferred",
        "depositary",
        "note ",
        "notes",
        "debenture",
        "test ",
    )
    return not any(b in n for b in blocked)


def fetch_sp500_tickers(timeout: float = 20.0) -> list[str]:
    """Load current S&P 500 constituents from Wikipedia (free, no API key)."""
    try:
        resp = requests.get(
            SP500_WIKI_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        table = next(
            t for t in tables if "Symbol" in t.columns or "Ticker" in t.columns
        )
        col = "Symbol" if "Symbol" in table.columns else "Ticker"
        tickers = [_normalize_symbol(s) for s in table[col].astype(str).tolist()]
        tickers = sorted({t for t in tickers if t and t != "NAN"})
        if len(tickers) < 100:
            raise ValueError(f"Unexpectedly small universe: {len(tickers)}")
        logger.info("Loaded %d S&P 500 tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as exc:  # noqa: BLE001
        logger.warning("S&P 500 fetch failed (%s); using fallback universe", exc)
        return list(FALLBACK_UNIVERSE)


def _read_nasdaq_pipe(url: str, symbol_col: str, timeout: float) -> pd.DataFrame:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    # Last line is a file creation timestamp — skip via comment-like filter
    lines = [
        ln
        for ln in resp.text.splitlines()
        if ln and not ln.startswith("File Creation")
    ]
    return pd.read_csv(StringIO("\n".join(lines)), sep="|")


def fetch_us_listed_tickers(timeout: float = 30.0) -> list[str]:
    """
    Full US listed common-stock universe from NASDAQ Trader symbol directories (free).

    Includes NASDAQ + NYSE/AMEX/ARCA via otherlisted.txt. Filters ETFs, test
    issues, and most warrants/units when the directory flags them.
    """
    try:
        nasdaq = _read_nasdaq_pipe(NASDAQ_LISTED_URL, "Symbol", timeout)
        other = _read_nasdaq_pipe(OTHER_LISTED_URL, "ACT Symbol", timeout)

        symbols: set[str] = set()

        def _take(frame: pd.DataFrame, sym_col: str) -> None:
            cols = set(frame.columns)
            work = frame
            if "ETF" in cols:
                work = work[work["ETF"] == "N"]
            if "Test Issue" in cols:
                work = work[work["Test Issue"] == "N"]
            name_col = "Security Name" if "Security Name" in cols else None
            for _, row in work.iterrows():
                sym = _normalize_symbol(str(row[sym_col]))
                if not _is_common_stock_symbol(sym):
                    continue
                if name_col and not _name_looks_like_equity(str(row[name_col])):
                    continue
                symbols.add(sym)

        _take(nasdaq, "Symbol")
        act = "ACT Symbol" if "ACT Symbol" in other.columns else "Symbol"
        _take(other, act)

        tickers = sorted(symbols)
        if len(tickers) < 1000:
            raise ValueError(f"Unexpectedly small US universe: {len(tickers)}")
        logger.info("Loaded %d US listed tickers from NASDAQ Trader", len(tickers))
        return tickers
    except Exception as exc:  # noqa: BLE001
        logger.warning("US listed fetch failed (%s); falling back to S&P 500", exc)
        return fetch_sp500_tickers(timeout=timeout)


def resolve_universe(
    limit: int | None = None,
    tickers: list[str] | None = None,
    universe: str = "us",
) -> list[str]:
    """
    Resolve the scan universe.

    universe: "us" (NASDAQ directories), "sp500" (Wikipedia), or ignored when tickers set.
    When ``limit`` is set, liquid mega-caps are kept first so names like NVDA
    are not dropped by alphabetical truncation.
    """
    if tickers:
        resolved = [_normalize_symbol(t) for t in tickers]
    elif universe == "sp500":
        resolved = fetch_sp500_tickers()
    else:
        resolved = fetch_us_listed_tickers()
    if limit is not None:
        resolved = _apply_limit_with_priority(resolved, limit)
    return resolved


def _apply_limit_with_priority(tickers: list[str], limit: int) -> list[str]:
    if limit <= 0:
        return []
    if len(tickers) <= limit:
        return list(tickers)
    seen: set[str] = set()
    out: list[str] = []
    for t in PRIORITY_LIQUID_TICKERS:
        if t in tickers and t not in seen:
            out.append(t)
            seen.add(t)
            if len(out) >= limit:
                return out
    for t in tickers:
        if t not in seen:
            out.append(t)
            seen.add(t)
            if len(out) >= limit:
                break
    return out
