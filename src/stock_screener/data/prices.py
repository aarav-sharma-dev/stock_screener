from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_OHLCV = ("Open", "High", "Low", "Close", "Volume")


def _normalize_download(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if raw.empty:
        return raw
    if len(tickers) == 1:
        t = tickers[0]
        frame = raw.copy()
        if not isinstance(frame.columns, pd.MultiIndex):
            frame.columns = pd.MultiIndex.from_product([frame.columns, [t]])
        return frame
    return raw


def _as_session_index(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").normalize().tz_localize(None)
    else:
        idx = idx.normalize()
    return idx


def _frame_to_multi(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for ticker, df in frames.items():
        if df is None or df.empty:
            continue
        part = df.loc[:, [c for c in _OHLCV if c in df.columns]].copy()
        if part.empty:
            continue
        part.columns = pd.MultiIndex.from_product([part.columns, [ticker]])
        pieces.append(part)
    if not pieces:
        return pd.DataFrame()
    out = pieces[0]
    for part in pieces[1:]:
        out = out.join(part, how="outer")
    return out.sort_index()


def _yahoo_download(
    tickers: list[str],
    *,
    start,
    end,
    chunk_size: int,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    logger.info(
        "Downloading prices for %d tickers via yfinance (chunk=%d)",
        len(tickers),
        chunk_size,
    )
    for i in range(0, len(tickers), chunk_size):
        batch = tickers[i : i + chunk_size]
        logger.info("Price chunk %d–%d / %d", i + 1, i + len(batch), len(tickers))
        raw = yf.download(
            tickers=batch,
            start=start.isoformat(),
            end=end.isoformat(),
            group_by="column",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        part = _normalize_download(raw, batch)
        if not part.empty:
            frames.append(part)

    if not frames:
        logger.warning("No price data returned")
        return pd.DataFrame()

    if len(frames) == 1:
        return frames[0]

    # Align on index; columns are MultiIndex (field, ticker)
    out = frames[0]
    for frame in frames[1:]:
        out = out.join(frame, how="outer")
    return out


def _extract_ticker_frame(prices: pd.DataFrame, ticker: str) -> pd.DataFrame:
    cols = {}
    for field in _OHLCV:
        s = series_for(prices, field, ticker)
        if not s.empty:
            cols[field] = s
    if "Close" not in cols:
        return pd.DataFrame()
    return pd.DataFrame(cols)


def _schwab_daily_one(symbol: str, start, end, client) -> pd.DataFrame:
    from stock_screener.data.schwab import price_history_daily, to_schwab_symbol

    raw = price_history_daily(
        to_schwab_symbol(symbol), start=start, end=end, client=client
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    frame.index = _as_session_index(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame[frame["Close"] > 0]
    return frame


def _download_prices_schwab(
    tickers: list[str],
    *,
    history_days: int,
    chunk_size: int,
) -> pd.DataFrame:
    """Schwab daily bars. A short or empty series stays empty. No Yahoo fill."""
    from stock_screener.data.schwab import (
        SchwabAPIError,
        SchwabNotConfiguredError,
        get_client,
    )

    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(days=history_days + 40)
    try:
        client = get_client()
    except (SchwabAPIError, SchwabNotConfiguredError) as exc:
        logger.warning("Schwab prices unavailable (%s)", exc)
        return pd.DataFrame()

    got: dict[str, pd.DataFrame] = {}
    logger.info("Downloading daily prices for %d tickers via Schwab", len(tickers))

    def _one(symbol: str) -> tuple[str, pd.DataFrame]:
        try:
            return symbol, _schwab_daily_one(symbol, start, end, client)
        except Exception as exc:  # noqa: BLE001
            logger.info("Schwab daily %s failed: %s", symbol, exc.__class__.__name__)
            return symbol, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futures):
            symbol, frame = fut.result()
            got[symbol] = frame

    del chunk_size  # Schwab history is one symbol per request
    n_schwab = sum(1 for t in tickers if not got.get(t, pd.DataFrame()).empty)
    n_empty = len(tickers) - n_schwab
    logger.info("Daily prices: %d Schwab, %d empty", n_schwab, n_empty)
    return _frame_to_multi(
        {t: got[t] for t in tickers if got.get(t) is not None and not got[t].empty}
    )


def download_prices(
    tickers: list[str],
    history_days: int = 320,
    chunk_size: int = 150,
    source: str = "yahoo",
) -> pd.DataFrame:
    """
    Download OHLCV for many tickers.

    ``source="schwab"`` uses Schwab market data only. A missing series stays empty.
    Returns MultiIndex columns (field, ticker).
    """
    if not tickers:
        return pd.DataFrame()
    if (source or "yahoo").lower() == "schwab":
        return _download_prices_schwab(
            tickers, history_days=history_days, chunk_size=chunk_size
        )

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=history_days + 40)
    return _yahoo_download(tickers, start=start, end=end + timedelta(days=1), chunk_size=chunk_size)


def _yahoo_intraday(
    tickers: list[str],
    *,
    interval: str,
    period: str,
    chunk_size: int,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    logger.info(
        "Downloading %s bars for %d tickers (period=%s, chunk=%d)",
        interval,
        len(tickers),
        period,
        chunk_size,
    )
    for i in range(0, len(tickers), chunk_size):
        batch = tickers[i : i + chunk_size]
        logger.info(
            "Intraday chunk %d–%d / %d", i + 1, i + len(batch), len(tickers)
        )
        raw = yf.download(
            tickers=batch,
            period=period,
            interval=interval,
            group_by="column",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        part = _normalize_download(raw, batch)
        if not part.empty:
            frames.append(part)

    if not frames:
        logger.warning("No intraday data returned")
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0]
    out = frames[0]
    for frame in frames[1:]:
        out = out.join(frame, how="outer")
    return out


def _schwab_rth_4h(symbol: str, client) -> pd.DataFrame:
    """RTH 4h bars. Minute history is capped near 10 days, so page three windows."""
    from stock_screener.data.schwab import (
        price_history_30m,
        to_rth_4h,
        to_schwab_symbol,
    )

    end = pd.Timestamp.now(tz="UTC")
    parts: list[pd.DataFrame] = []
    schwab_symbol = to_schwab_symbol(symbol)
    for i in (2, 1, 0):
        chunk_end = end - pd.Timedelta(days=10 * i)
        chunk_start = chunk_end - pd.Timedelta(days=10)
        try:
            part = price_history_30m(
                schwab_symbol,
                start=chunk_start,
                end=chunk_end,
                extended=False,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Schwab 30m %s window %s failed: %s",
                symbol,
                chunk_start.date(),
                exc.__class__.__name__,
            )
            continue
        if part is not None and not part.empty:
            parts.append(part)
    if not parts:
        return pd.DataFrame()
    bars = pd.concat(parts)
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    out = to_rth_4h(bars)
    if out is None or out.empty:
        return pd.DataFrame()
    if out.index.tz is not None:
        out = out.copy()
        out.index = out.index.tz_convert("America/New_York").tz_localize(None)
    return out


def _download_intraday_schwab(
    tickers: list[str],
    *,
    interval: str,
    period: str,
    chunk_size: int,
) -> pd.DataFrame:
    from stock_screener.data.schwab import (
        SchwabAPIError,
        SchwabNotConfiguredError,
        get_client,
    )

    try:
        client = get_client()
    except (SchwabAPIError, SchwabNotConfiguredError) as exc:
        logger.warning("Schwab intraday unavailable (%s)", exc)
        return pd.DataFrame()

    logger.info("Downloading 4h bars for %d tickers via Schwab", len(tickers))
    got: dict[str, pd.DataFrame] = {}

    def _one(symbol: str) -> tuple[str, pd.DataFrame]:
        try:
            return symbol, _schwab_rth_4h(symbol, client)
        except Exception as exc:  # noqa: BLE001
            logger.info("Schwab 4h %s failed: %s", symbol, exc.__class__.__name__)
            return symbol, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futures):
            symbol, frame = fut.result()
            got[symbol] = frame

    del interval, period, chunk_size  # Schwab builds 4h from 30m candles
    n_empty = sum(1 for t in tickers if got.get(t) is None or got[t].empty)
    n_schwab = len(tickers) - n_empty
    logger.info("4h bars: %d Schwab, %d empty", n_schwab, n_empty)
    return _frame_to_multi(
        {t: got[t] for t in tickers if got.get(t) is not None and not got[t].empty}
    )


def download_intraday(
    tickers: list[str],
    *,
    interval: str = "4h",
    period: str = "60d",
    chunk_size: int = 40,
    source: str = "yahoo",
) -> pd.DataFrame:
    """
    Download intraday OHLCV (e.g. 4h) for RSI / short-TF screens.

    ``source="schwab"`` builds RTH 4h bars from Schwab 30m candles.
    Returns MultiIndex columns (field, ticker).
    """
    if not tickers:
        return pd.DataFrame()
    if (source or "yahoo").lower() == "schwab":
        return _download_intraday_schwab(
            tickers, interval=interval, period=period, chunk_size=chunk_size
        )
    return _yahoo_intraday(
        tickers, interval=interval, period=period, chunk_size=chunk_size
    )


def series_for(prices: pd.DataFrame, field: str, ticker: str) -> pd.Series:
    if prices.empty:
        return pd.Series(dtype=float)
    if isinstance(prices.columns, pd.MultiIndex):
        if (field, ticker) in prices.columns:
            return prices[(field, ticker)].dropna()
        if (ticker, field) in prices.columns:
            return prices[(ticker, field)].dropna()
    if field in prices.columns:
        return prices[field].dropna()
    return pd.Series(dtype=float)


def _schwab_vix() -> float | None:
    from stock_screener.data.schwab import (
        SchwabAPIError,
        SchwabNotConfiguredError,
        get_client,
        quote_last,
    )

    try:
        return quote_last("$VIX", client=get_client())
    except (SchwabAPIError, SchwabNotConfiguredError) as exc:
        logger.info("Schwab VIX unavailable: %s", exc.__class__.__name__)
        return None


def latest_vix(*, source: str = "yahoo") -> float | None:
    """VIX spot. ``source="schwab"`` quotes ``$VIX`` only."""
    if (source or "yahoo").lower() == "schwab":
        vix = _schwab_vix()
        if vix is not None and vix > 0:
            logger.info("VIX from Schwab: %.2f", vix)
            return vix
        logger.info("VIX: Schwab quote missing")
        return None
    try:
        hist = yf.Ticker("^VIX").history(period="5d", auto_adjust=True)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("VIX download failed: %s", exc)
        return None
