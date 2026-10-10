from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenerConfig:
    """Defaults for long-only screens (support + catalyst + fundamentals)."""

    # Lookbacks
    history_days: int = 320
    donchian_window: int = 55
    sma_fast: int = 50
    sma_slow: int = 200
    news_hours: int = 72

    # Universe
    universe: str = "us"  # "us" | "sp500"
    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    avg_volume_window: int = 20
    price_chunk_size: int = 150

    # Mode: support | catalyst | fundamentals | politicians | trump | athdip | both | all
    mode: str = "all"

    # Politicians mode
    enable_politicians: bool = True
    politicians_capitol_pages: int = 5
    politicians_house_filings: int = 30
    politicians_enable_capitol: bool = True
    politicians_enable_house: bool = True
    politicians_enable_senate: bool = True
    politicians_lag_cap_days: int = 45
    politicians_min_trades: int = 1

    # Trump tracker (Truth Social + news + WH + optional X/Grok)
    enable_trump: bool = True
    trump_enable_truth: bool = True
    trump_enable_news: bool = True
    trump_enable_wh: bool = True
    trump_enable_x: bool = True
    trump_truth_max_posts: int = 80

    # Athdip: recent multi-year ATH setup → later 4h RSI dip (sequential)
    athdip_max_pct_from_ath: float = -50.0  # optional depth cap when near-ATH gate on
    athdip_rsi_4h_target: float = 25.0  # prefer deeper oversold
    athdip_rsi_4h_max: float = 31.0  # trigger: 4h RSI ≤ this (TV~30; Yahoo often ~30–31)
    athdip_require_uptrend: bool = False
    athdip_require_sma50: bool = False
    athdip_use_4h_ema200: bool = False
    athdip_ema200_max_pct: float = -5.0
    athdip_history_days: int = 1825  # ~5y for multi-year prior high
    athdip_intraday_period: str = "730d"  # Yahoo 4h history (~2–3y)
    athdip_fresh_high_days: int = 21  # breakout window used at ATH time
    athdip_max_days_since_ath: int = 63  # watch pullbacks for ~3 months after ATH
    athdip_rsi_lookback_days: int = 5  # min 4h RSI over this many calendar days (catch mid-week dips)
    athdip_require_multi_year_break: bool = True  # require break-at-ATH setup
    athdip_require_near_ath_pct: bool = False
    athdip_drop_death_cross: bool = False
    # Optional entry-bar confirmation: Internal Bar Strength = (close-low)/(high-low)
    # of the daily trigger bar must be ≤ this (Alvarez: ≤0.25 keeps ~63% of RSI(2)
    # trades with ~+21% avg P/L). None = off (production default).
    athdip_ibs_max: float | None = None
    # Optional regime gates (pre-chosen from literature, not tuned here):
    #   athdip_gate_vix_min      — prior-session VIX close ≥ this (e.g. 20.0)
    #   athdip_gate_spx_mom_days — SPY return over N sessions must be > 0 (e.g. 126)
    athdip_gate_vix_min: float | None = None
    athdip_gate_spx_mom_days: int | None = None
    # PEAD mode (post-earnings-announcement drift, long side only)
    enable_pead: bool = True
    pead_max_sessions_since_report: int = 15  # entry window after reaction day R
    pead_min_surprise_pct: float = 5.0  # Yahoo EPS surprise (SUE proxy)
    pead_min_ear_pct: float = 3.0  # close[R-1]→close[R+1] minus SPY
    pead_earnings_limit: int = 8  # yfinance earnings_dates rows per ticker
    pead_prefilter_abs_move_pct: float = 2.5  # skip names with no |1d move| ≥ this recently
    pead_require_above_entry: bool = False  # last close ≥ close[R+1] (drift intact)
    # Support-mode hard filters (neutral baseline; VIX regime adjusts)
    min_range_pct: float = 0.05
    max_proximity_for_long: float = 0.45
    require_above_sma_slow: bool = True
    drop_death_cross: bool = True

    # Catalyst-mode hard filters / gates (neutral baseline)
    catalyst_min_news_score: float = 58.0
    catalyst_min_volume_ratio: float = 1.4
    catalyst_min_day_return_pct: float = 0.5  # %
    catalyst_require_signal: bool = True

    # Support composite weights (sum ≈ 1.0)
    weight_proximity: float = 0.25
    weight_sma: float = 0.18
    weight_rsi: float = 0.12
    weight_market: float = 0.13
    weight_news: float = 0.12
    weight_earnings: float = 0.10
    weight_social: float = 0.10

    # Catalyst composite weights (sum ≈ 1.0)
    cat_weight_news: float = 0.28
    cat_weight_momentum: float = 0.22
    cat_weight_rsi: float = 0.10
    cat_weight_earnings: float = 0.18
    cat_weight_social: float = 0.12
    cat_weight_market: float = 0.10

    # Soft RSI preferences (not hard filters)
    support_prefer_rsi_below: float = 45.0
    catalyst_prefer_rsi_above: float = 50.0
    catalyst_rsi_overbought: float = 80.0

    # Fundamentals enrich on top technical survivors
    fundamentals_enrich_top: int = 20
    enable_fundamentals: bool = True

    # Market sentiment via VIX
    vix_risk_on: float = 15.0
    vix_risk_off: float = 25.0

    # Runtime
    max_tickers: int | None = None
    sleep_between_news: float = 0.15
    top_n: int = 25
    grok_top_n: int = 15
    enable_reddit: bool = True
    enable_earnings: bool = True
    enable_grok: bool = True
    # Google News RSS that quotes X/Twitter — blended into Yahoo news for
    # support/catalyst (not a separate mode; StockTwits left experimental).
    enable_x_quote_news: bool = True
    x_quote_news_weight: float = 0.35  # share of news score when both fire
    # Polymarket per-ticker odds — blended into social for support/catalyst
    enable_polymarket: bool = True
    polymarket_weight: float = 0.35  # share of social when open markets exist
    polymarket_min_volume: float = 500.0
    # Curated 13F managers — soft social overlay (lagged quarterly holdings)
    enable_institutions: bool = True
    institutions_weight: float = 0.30  # share of social when ≥1 manager holds
    # OHLCV for a screen run. "schwab" tries Schwab first and uses Yahoo only
    # for symbols Schwab did not return. News, fundamentals, and earnings
    # dates stay on Yahoo either way. Backtests keep the default "yahoo".
    price_source: str = "yahoo"


DEFAULT_CONFIG = ScreenerConfig()

FALLBACK_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "BRK-B",
    "JPM",
    "XOM",
    "UNH",
    "V",
    "MA",
    "HD",
    "PG",
    "JNJ",
    "COST",
    "ABBV",
    "AVGO",
    "MRK",
    "LLY",
    "PEP",
    "KO",
    "WMT",
    "BAC",
    "CRM",
    "AMD",
    "NFLX",
    "DIS",
    "ADBE",
    "CSCO",
]
