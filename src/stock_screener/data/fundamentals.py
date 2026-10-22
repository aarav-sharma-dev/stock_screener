from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class FundamentalsSnapshot:
    ticker: str
    sector: str
    industry: str
    # Growth
    revenue_growth: float | None
    earnings_growth: float | None
    # Profitability
    gross_margin: float | None
    operating_margin: float | None
    profit_margin: float | None
    fcf: float | None
    # Balance sheet
    debt_to_equity: float | None
    current_ratio: float | None
    # Capital allocation / returns
    roe: float | None
    roa: float | None
    dividend_yield: float | None
    payout_ratio: float | None
    # Valuation
    trailing_pe: float | None
    forward_pe: float | None
    price_to_sales: float | None
    price_to_book: float | None
    ev_ebitda: float | None
    peg: float | None  # PE / growth (lower = cheaper growth)
    market_cap: float | None
    fcf_yield: float | None  # FCF / market cap
    # Pillar scores 0–100
    growth_score: float
    profitability_score: float
    balance_score: float
    capital_score: float
    valuation_score: float
    composite: float
    pass_filters: bool
    filter_reason: str
    why: str

    def to_dict(self) -> dict:
        return asdict(self)


def _f(info: dict, *keys: str) -> float | None:
    for k in keys:
        v = info.get(k)
        if v is None:
            continue
        try:
            x = float(v)
            if x != x:  # NaN
                continue
            return x
        except (TypeError, ValueError):
            continue
    return None


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _score_growth(rev: float | None, eps: float | None) -> float:
    parts: list[float] = []
    for g in (rev, eps):
        if g is None:
            continue
        # 0% → 40, 10% → 70, 25%+ → 95; negative growth penalized
        parts.append(_clip(40.0 + g * 200.0))
    return sum(parts) / len(parts) if parts else 50.0


def _score_profitability(
    gross: float | None,
    op: float | None,
    profit: float | None,
    fcf: float | None,
) -> float:
    parts: list[float] = []
    for m in (gross, op, profit):
        if m is None:
            continue
        parts.append(_clip(30.0 + m * 120.0))  # 20% margin ~ 54, 40% ~ 78
    if fcf is not None:
        parts.append(70.0 if fcf > 0 else 25.0)
    return sum(parts) / len(parts) if parts else 50.0


def _score_balance(de: float | None, current: float | None) -> float:
    parts: list[float] = []
    if de is not None:
        # debt/equity often reported as percent-like (e.g. 50 = 0.5) or ratio
        ratio = de / 100.0 if de > 5 else de
        if ratio <= 0.5:
            parts.append(85.0)
        elif ratio <= 1.0:
            parts.append(70.0)
        elif ratio <= 2.0:
            parts.append(50.0)
        else:
            parts.append(25.0)
    if current is not None:
        if current >= 1.5:
            parts.append(80.0)
        elif current >= 1.0:
            parts.append(60.0)
        else:
            parts.append(30.0)
    return sum(parts) / len(parts) if parts else 50.0


def _score_capital(roe: float | None, roa: float | None, div: float | None) -> float:
    parts: list[float] = []
    for r in (roe, roa):
        if r is None:
            continue
        parts.append(_clip(40.0 + r * 150.0))
    if div is not None and div > 0:
        # modest dividend is fine; very high may be value trap — soft bump
        parts.append(_clip(55.0 + min(div, 0.05) * 400.0))
    return sum(parts) / len(parts) if parts else 50.0


def _score_valuation(
    pe: float | None,
    fwd_pe: float | None,
    ps: float | None,
    pb: float | None,
    ev: float | None,
    peg: float | None = None,
    fcf_yield: float | None = None,
) -> float:
    """Higher score = cheaper / more reasonable multiple (long-only value bias)."""
    parts: list[float] = []

    def pe_score(x: float) -> float:
        if x <= 0:
            return 20.0
        if x < 12:
            return 90.0
        if x < 20:
            return 75.0
        if x < 30:
            return 55.0
        if x < 45:
            return 40.0
        return 25.0

    for x in (pe, fwd_pe):
        if x is not None:
            parts.append(pe_score(x))
    if ps is not None:
        parts.append(85.0 if ps < 2 else 70.0 if ps < 5 else 50.0 if ps < 10 else 30.0)
    if pb is not None and pb > 0:
        parts.append(80.0 if pb < 2 else 60.0 if pb < 5 else 40.0 if pb < 10 else 25.0)
    if ev is not None and ev > 0:
        parts.append(85.0 if ev < 10 else 65.0 if ev < 15 else 45.0 if ev < 25 else 30.0)
    if peg is not None and peg > 0:
        # PEG < 1 cheap growth; > 2 expensive
        if peg < 1.0:
            parts.append(90.0)
        elif peg < 1.5:
            parts.append(75.0)
        elif peg < 2.0:
            parts.append(55.0)
        elif peg < 3.0:
            parts.append(40.0)
        else:
            parts.append(25.0)
    if fcf_yield is not None:
        # 5%+ strong cash yield; negative = poor
        if fcf_yield >= 0.06:
            parts.append(90.0)
        elif fcf_yield >= 0.04:
            parts.append(75.0)
        elif fcf_yield >= 0.02:
            parts.append(60.0)
        elif fcf_yield > 0:
            parts.append(45.0)
        else:
            parts.append(20.0)
    return sum(parts) / len(parts) if parts else 50.0


def _normalize_roe(roe: float | None) -> float | None:
    """Yahoo may return 0.15 or 15 for 15% ROE."""
    if roe is None:
        return None
    return roe / 100.0 if roe > 1.5 else roe


def _compute_peg(
    peg_raw: float | None,
    pe: float | None,
    earnings_growth: float | None,
) -> float | None:
    if peg_raw is not None and peg_raw > 0:
        return peg_raw
    if pe is None or pe <= 0 or earnings_growth is None:
        return None
    # earningsGrowth often decimal (0.12 = 12%)
    g = earnings_growth * 100.0 if abs(earnings_growth) < 1.5 else earnings_growth
    if g <= 0:
        return None
    return pe / g


def _compute_fcf_yield(fcf: float | None, market_cap: float | None) -> float | None:
    if fcf is None or market_cap is None or market_cap <= 0:
        return None
    return fcf / market_cap


def _schwab_info(raw: dict) -> dict:
    """Map a Schwab fundamental block onto the Yahoo info keys the scorer uses.

    Schwab sends margins, growth, and dividend yield as percents (48.6 = 48.6%).
    The scorer expects fractions. Multiples (P/E, PEG, P/S, P/B) stay as ratios.
    """

    def num(*keys: str) -> float | None:
        return _f(raw, *keys)

    def pct(*keys: str) -> float | None:
        v = num(*keys)
        return None if v is None else v / 100.0

    return {
        "revenueGrowth": pct("revChangeTTM", "revChangeYear"),
        "earningsGrowth": pct("epsChangePercentTTM"),
        "grossMargins": pct("grossMarginTTM"),
        "operatingMargins": pct("operatingMarginTTM"),
        "profitMargins": pct("netProfitMarginTTM"),
        "debtToEquity": num("totalDebtToEquity"),
        "currentRatio": num("currentRatio"),
        "returnOnEquity": num("returnOnEquity"),
        "returnOnAssets": num("returnOnAssets"),
        "dividendYield": pct("dividendYield"),
        "trailingPE": num("peRatio"),
        "priceToSalesTrailing12Months": num("prRatio"),
        "priceToBook": num("pbRatio"),
        "pegRatio": num("pegRatio"),
        "marketCap": num("marketCap"),
    }


def fetch_fundamentals(
    ticker: str, info: dict | None = None
) -> FundamentalsSnapshot | None:
    if info is None:
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fundamentals fetch failed for %s: %s", ticker, exc)
            return None
    if not info or info.get("trailingPegRatio") == "None" and not info.get("sector"):
        # still try — many fields may exist
        pass

    rev = _f(info, "revenueGrowth")
    eps_g = _f(info, "earningsGrowth", "earningsQuarterlyGrowth")
    gross = _f(info, "grossMargins")
    op = _f(info, "operatingMargins")
    profit = _f(info, "profitMargins")
    fcf = _f(info, "freeCashflow")
    de = _f(info, "debtToEquity")
    current = _f(info, "currentRatio")
    roe = _f(info, "returnOnEquity")
    roa = _f(info, "returnOnAssets")
    div = _f(info, "dividendYield")
    payout = _f(info, "payoutRatio")
    pe = _f(info, "trailingPE")
    fwd = _f(info, "forwardPE")
    ps = _f(info, "priceToSalesTrailing12Months")
    pb = _f(info, "priceToBook")
    ev = _f(info, "enterpriseToEbitda")
    peg_raw = _f(info, "trailingPegRatio", "pegRatio")
    mcap = _f(info, "marketCap")
    peg = _compute_peg(peg_raw, pe if pe is not None else fwd, eps_g)
    fcf_y = _compute_fcf_yield(fcf, mcap)
    roe_n = _normalize_roe(roe)

    growth = _score_growth(rev, eps_g)
    profitability = _score_profitability(gross, op, profit, fcf)
    balance = _score_balance(de, current)
    capital = _score_capital(roe_n, roa, div)
    valuation = _score_valuation(pe, fwd, ps, pb, ev, peg=peg, fcf_yield=fcf_y)

    composite = round(
        0.20 * growth
        + 0.25 * profitability
        + 0.20 * balance
        + 0.15 * capital
        + 0.20 * valuation,
        2,
    )

    # Soft quality gate: need at least some real fields
    known = sum(
        1
        for x in (rev, eps_g, gross, op, profit, de, current, roe, pe, ps)
        if x is not None
    )
    if known < 3:
        return FundamentalsSnapshot(
            ticker=ticker,
            sector=str(info.get("sector") or ""),
            industry=str(info.get("industry") or ""),
            revenue_growth=rev,
            earnings_growth=eps_g,
            gross_margin=gross,
            operating_margin=op,
            profit_margin=profit,
            fcf=fcf,
            debt_to_equity=de,
            current_ratio=current,
            roe=roe_n,
            roa=roa,
            dividend_yield=div,
            payout_ratio=payout,
            trailing_pe=pe,
            forward_pe=fwd,
            price_to_sales=ps,
            price_to_book=pb,
            ev_ebitda=ev,
            peg=peg,
            market_cap=mcap,
            fcf_yield=fcf_y,
            growth_score=growth,
            profitability_score=profitability,
            balance_score=balance,
            capital_score=capital,
            valuation_score=valuation,
            composite=float("nan"),
            pass_filters=False,
            filter_reason="insufficient fundamentals",
            why="insufficient fundamentals",
        )

    ok = True
    reason = ""
    # Hard-ish quality floors
    if profit is not None and profit < -0.05 and (fcf is None or fcf < 0):
        ok = False
        reason = "unprofitable without FCF"
    elif de is not None:
        ratio = de / 100.0 if de > 5 else de
        if ratio > 3.5:
            ok = False
            reason = "excessive leverage"

    bits = [
        f"g={growth:.0f}",
        f"p={profitability:.0f}",
        f"bs={balance:.0f}",
        f"cap={capital:.0f}",
        f"val={valuation:.0f}",
    ]
    if peg is not None:
        bits.append(f"peg={peg:.2f}")
    if fcf_y is not None:
        bits.append(f"fcfy={fcf_y:.1%}")
    if info.get("sector"):
        bits.insert(0, str(info.get("sector")))

    return FundamentalsSnapshot(
        ticker=ticker,
        sector=str(info.get("sector") or ""),
        industry=str(info.get("industry") or ""),
        revenue_growth=rev,
        earnings_growth=eps_g,
        gross_margin=gross,
        operating_margin=op,
        profit_margin=profit,
        fcf=fcf,
        debt_to_equity=de,
        current_ratio=current,
        roe=roe_n,
        roa=roa,
        dividend_yield=div,
        payout_ratio=payout,
        trailing_pe=pe,
        forward_pe=fwd,
        price_to_sales=ps,
        price_to_book=pb,
        ev_ebitda=ev,
        peg=round(peg, 3) if peg is not None else None,
        market_cap=mcap,
        fcf_yield=round(fcf_y, 4) if fcf_y is not None else None,
        growth_score=round(growth, 1),
        profitability_score=round(profitability, 1),
        balance_score=round(balance, 1),
        capital_score=round(capital, 1),
        valuation_score=round(valuation, 1),
        composite=composite if ok else float("nan"),
        pass_filters=ok,
        filter_reason=reason,
        why="; ".join(bits) if ok else reason,
    )


def fetch_fundamentals_map(
    tickers: list[str],
    max_workers: int = 6,
    source: str = "yahoo",
) -> dict[str, FundamentalsSnapshot]:
    out: dict[str, FundamentalsSnapshot] = {}
    if not tickers:
        return out
    if (source or "yahoo").lower() == "schwab":
        from stock_screener.data.schwab import (
            SchwabAPIError,
            SchwabNotConfiguredError,
            get_client,
            instrument_fundamental,
        )

        logger.info("Fetching fundamentals for %d tickers via Schwab", len(tickers))
        try:
            client = get_client()
        except (SchwabAPIError, SchwabNotConfiguredError) as exc:
            logger.warning("Schwab fundamentals unavailable (%s)", exc)
            return out

        def _one_schwab(t: str) -> tuple[str, FundamentalsSnapshot | None]:
            try:
                raw = instrument_fundamental(t, client=client)
            except Exception as exc:  # noqa: BLE001
                logger.info("Schwab fundamental %s failed: %s", t, exc.__class__.__name__)
                return t, None
            if not raw:
                return t, None
            return t, fetch_fundamentals(t, info=_schwab_info(raw))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_one_schwab, t) for t in tickers]
            for fut in as_completed(futures):
                t, snap = fut.result()
                if snap is not None:
                    out[t] = snap
        return out

    logger.info("Fetching fundamentals for %d tickers via yfinance", len(tickers))

    def _one(t: str) -> tuple[str, FundamentalsSnapshot | None]:
        return t, fetch_fundamentals(t)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futures):
            t, snap = fut.result()
            if snap is not None:
                out[t] = snap
    return out
