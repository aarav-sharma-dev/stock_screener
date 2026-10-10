from __future__ import annotations

from dataclasses import dataclass

from stock_screener.config import ScreenerConfig
from stock_screener.data.prices import latest_vix


@dataclass(frozen=True)
class RegimeParams:
    """VIX-driven gate tweaks for support / catalyst modes."""

    max_proximity: float
    require_above_sma_slow: bool
    catalyst_min_news: float
    catalyst_min_volume: float
    catalyst_min_day_return: float
    catalyst_require_price_and_volume: bool
    support_market_weight_boost: float  # added to market weight, taken from news
    catalyst_momentum_boost: float
    note: str


@dataclass(frozen=True)
class MarketSentiment:
    vix: float | None
    score: float  # 0–100, higher = more risk-on
    label: str
    reason: str
    regime: RegimeParams


def _regime_for(label: str, config: ScreenerConfig) -> RegimeParams:
    if label == "risk-on":
        return RegimeParams(
            max_proximity=min(0.55, config.max_proximity_for_long + 0.08),
            require_above_sma_slow=config.require_above_sma_slow,
            catalyst_min_news=max(52.0, config.catalyst_min_news_score - 4.0),
            catalyst_min_volume=max(1.2, config.catalyst_min_volume_ratio - 0.15),
            catalyst_min_day_return=max(0.2, config.catalyst_min_day_return_pct - 0.2),
            catalyst_require_price_and_volume=False,
            support_market_weight_boost=0.0,
            catalyst_momentum_boost=0.03,
            note="risk-on: slightly looser support prox; catalyst more permissive",
        )
    if label == "risk-off":
        return RegimeParams(
            max_proximity=max(0.30, config.max_proximity_for_long - 0.10),
            require_above_sma_slow=True,  # force uptrend filter in risk-off
            catalyst_min_news=min(70.0, config.catalyst_min_news_score + 8.0),
            catalyst_min_volume=config.catalyst_min_volume_ratio + 0.25,
            catalyst_min_day_return=config.catalyst_min_day_return_pct + 0.5,
            catalyst_require_price_and_volume=True,
            support_market_weight_boost=0.05,
            catalyst_momentum_boost=-0.05,
            note="risk-off: stricter support; catalyst needs vol+green day",
        )
    return RegimeParams(
        max_proximity=config.max_proximity_for_long,
        require_above_sma_slow=config.require_above_sma_slow,
        catalyst_min_news=config.catalyst_min_news_score,
        catalyst_min_volume=config.catalyst_min_volume_ratio,
        catalyst_min_day_return=config.catalyst_min_day_return_pct,
        catalyst_require_price_and_volume=False,
        support_market_weight_boost=0.0,
        catalyst_momentum_boost=0.0,
        note="neutral: default gates",
    )


def assess_market_sentiment(config: ScreenerConfig) -> MarketSentiment:
    """Long-only market bias + regime params from free ^VIX data."""
    vix = latest_vix(source=getattr(config, "price_source", "yahoo"))
    if vix is None:
        label = "neutral"
        score = 50.0
        reason = "VIX unavailable — assuming neutral"
    elif vix <= config.vix_risk_on:
        score = 80.0 + max(0.0, (config.vix_risk_on - vix) * 2.0)
        label = "risk-on"
        reason = f"VIX {vix:.1f} ≤ {config.vix_risk_on:.0f}"
    elif vix >= config.vix_risk_off:
        score = max(5.0, 40.0 - (vix - config.vix_risk_off) * 2.0)
        label = "risk-off"
        reason = f"VIX {vix:.1f} ≥ {config.vix_risk_off:.0f}"
    else:
        span = config.vix_risk_off - config.vix_risk_on
        t = (vix - config.vix_risk_on) / span
        score = 80.0 - t * 40.0
        label = "neutral"
        reason = f"VIX {vix:.1f} in mid range"

    regime = _regime_for(label, config)
    return MarketSentiment(
        vix=vix,
        score=min(100.0, max(0.0, score)),
        label=label,
        reason=f"{reason} · {regime.note}",
        regime=regime,
    )
