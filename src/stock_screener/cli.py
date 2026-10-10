from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from stock_screener.config import ScreenerConfig
from stock_screener.screener import run_screener


def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Long-only screener: support, catalyst, fundamentals, politicians, "
            "trump, athdip; VIX regime adjusts gates"
        ),
    )
    p.add_argument("--tickers", type=str, default="", help="Comma-separated tickers")
    p.add_argument(
        "--universe",
        choices=("us", "sp500"),
        default="us",
        help="Free universe source",
    )
    p.add_argument(
        "--mode",
        choices=(
            "support",
            "catalyst",
            "fundamentals",
            "politicians",
            "trump",
            "athdip",
            "pead",
            "both",
            "all",
        ),
        default="all",
        help=(
            "Screen mode (default: all). athdip = recent multi-year ATH + 4h RSI dip; "
            "pead = post-earnings drift (EPS surprise + positive reaction)"
        ),
    )
    p.add_argument("--limit", type=int, default=None, help="Cap universe size")
    p.add_argument("--top", type=int, default=25, help="Rows to print per mode")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("output/screen.csv"),
        help="CSV base path (suffixes per mode)",
    )
    p.add_argument("--no-news", action="store_true")
    p.add_argument(
        "--no-x-quote-news",
        action="store_true",
        help="Skip Google News RSS that quotes X/Twitter (blended into Yahoo news)",
    )
    p.add_argument(
        "--no-polymarket",
        action="store_true",
        help="Skip Polymarket odds (blended into support/catalyst social)",
    )
    p.add_argument(
        "--no-institutions",
        action="store_true",
        help="Skip curated 13F hedge-fund/manager holdings overlay",
    )
    p.add_argument("--no-reddit", action="store_true")
    p.add_argument("--no-earnings", action="store_true")
    p.add_argument("--no-fundamentals", action="store_true")
    p.add_argument("--no-politicians", action="store_true")
    p.add_argument("--no-trump", action="store_true", help="Disable Trump tracker")
    p.add_argument(
        "--no-trump-truth",
        action="store_true",
        help="Skip Truth Social (trumpstruth.org RSS)",
    )
    p.add_argument(
        "--no-trump-news",
        action="store_true",
        help="Skip Google News RSS for Trump/policy",
    )
    p.add_argument(
        "--no-trump-wh",
        action="store_true",
        help="Skip White House official RSS (news / EO / briefings)",
    )
    p.add_argument(
        "--no-trump-x",
        action="store_true",
        help="Skip Trump X posts via Grok (Truth/news/WH still run)",
    )
    p.add_argument(
        "--trump-max-posts",
        type=int,
        default=80,
        help="Max Truth Social posts to pull from RSS (default 80)",
    )
    p.add_argument("--no-capitoltrades", action="store_true")
    p.add_argument("--no-house", action="store_true")
    p.add_argument("--no-senate", action="store_true")
    p.add_argument(
        "--no-grok",
        action="store_true",
        help="Disable Grok (support/catalyst X sentiment AND Trump X feed)",
    )
    p.add_argument("--grok-top", type=int, default=15)
    p.add_argument("--politician-pages", type=int, default=5)
    p.add_argument("--house-filings", type=int, default=25)
    p.add_argument(
        "--athdip-max-pct",
        type=float,
        default=-100.0,
        help="Athdip: unused unless near-ATH pct gate enabled (default -100)",
    )
    p.add_argument(
        "--athdip-rsi-max",
        type=float,
        default=31.0,
        help="Athdip: 4h RSI must be ≤ this (default 31 ≈ TradingView 30)",
    )
    p.add_argument(
        "--athdip-fresh-days",
        type=int,
        default=21,
        help="Athdip: breakout window used at ATH time (default 21)",
    )
    p.add_argument(
        "--athdip-max-ath-age",
        type=int,
        default=63,
        help="Athdip: max trading days since multi-year ATH to watch (default 63)",
    )
    p.add_argument(
        "--athdip-rsi-lookback",
        type=int,
        default=5,
        help="Athdip: min 4h RSI over last N calendar days (default 5; catches mid-week dips)",
    )
    p.add_argument(
        "--pead-max-sessions",
        type=int,
        default=15,
        help="PEAD: entry window in sessions after the reaction day (default 15)",
    )
    p.add_argument(
        "--pead-min-surprise",
        type=float,
        default=5.0,
        help="PEAD: min EPS surprise %% vs Yahoo consensus (default 5)",
    )
    p.add_argument(
        "--pead-min-ear",
        type=float,
        default=3.0,
        help="PEAD: min earnings-announcement return vs SPY, close[R-1]→close[R+1] (default 3)",
    )
    p.add_argument(
        "--pead-require-drift",
        action="store_true",
        help="PEAD: also require last close ≥ entry close (drift still intact)",
    )
    p.add_argument(
        "--no-pead",
        action="store_true",
        help="Skip PEAD in --mode all (saves ~1–2 min of earnings-date lookups)",
    )
    p.add_argument(
        "--price-source",
        choices=("yahoo", "schwab"),
        default="yahoo",
        help=(
            "OHLCV source. schwab uses Yahoo only when a symbol comes back "
            "short or empty (news, fundamentals, and earnings stay on Yahoo)"
        ),
    )
    p.add_argument(
        "--allow-below-200",
        action="store_true",
        help="Support baseline: allow below SMA200 (risk-off still forces above)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _print_table(df: pd.DataFrame, cols: list[str], top: int) -> None:
    show = df.head(top)
    use = [c for c in cols if c in show.columns]
    try:
        from tabulate import tabulate

        print(tabulate(show[use], headers="keys", tablefmt="simple", showindex=False))
    except Exception:  # noqa: BLE001
        print(show[use].to_string(index=False))


def _save(df: pd.DataFrame, base: Path, mode_name: str, single: bool) -> Path:
    path = base if single else base.with_name(f"{base.stem}_{mode_name}.csv")
    df.to_csv(path, index=False)
    return path


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("yfinance").setLevel(logging.INFO)
    logging.getLogger("peewee").setLevel(logging.INFO)

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    config = ScreenerConfig(
        universe=args.universe,
        mode=args.mode,
        max_tickers=args.limit,
        top_n=args.top,
        require_above_sma_slow=not args.allow_below_200,
        enable_reddit=not args.no_reddit,
        enable_earnings=not args.no_earnings,
        enable_fundamentals=not args.no_fundamentals,
        enable_politicians=not args.no_politicians,
        politicians_enable_capitol=not args.no_capitoltrades,
        politicians_enable_house=not args.no_house,
        politicians_enable_senate=not args.no_senate,
        politicians_capitol_pages=args.politician_pages,
        politicians_house_filings=args.house_filings,
        enable_trump=not args.no_trump,
        trump_enable_truth=not args.no_trump_truth,
        trump_enable_news=not args.no_trump_news,
        trump_enable_wh=not args.no_trump_wh,
        trump_enable_x=not args.no_trump_x,
        trump_truth_max_posts=args.trump_max_posts,
        athdip_max_pct_from_ath=args.athdip_max_pct,
        athdip_rsi_4h_max=args.athdip_rsi_max,
        athdip_fresh_high_days=args.athdip_fresh_days,
        athdip_max_days_since_ath=args.athdip_max_ath_age,
        athdip_rsi_lookback_days=args.athdip_rsi_lookback,
        enable_pead=not args.no_pead,
        pead_max_sessions_since_report=args.pead_max_sessions,
        pead_min_surprise_pct=args.pead_min_surprise,
        pead_min_ear_pct=args.pead_min_ear,
        pead_require_above_entry=args.pead_require_drift,
        enable_grok=not args.no_grok,
        grok_top_n=args.grok_top,
        enable_x_quote_news=not args.no_x_quote_news,
        enable_polymarket=not args.no_polymarket,
        enable_institutions=not args.no_institutions,
        price_source=args.price_source,
    )

    (
        support_df,
        catalyst_df,
        fund_df,
        pol_df,
        trump_df,
        athdip_df,
        pead_df,
        market,
    ) = run_screener(
        config=config,
        tickers=tickers,
        fetch_news=not args.no_news,
    )

    print(
        f"\nMarket: {market.label} | VIX={market.vix if market.vix is not None else 'n/a'}"
    )
    print(f"Regime: {market.reason}")
    try:
        from stock_screener.data.market_cycle import assess_market_cycle

        cycle = assess_market_cycle()
        cape_s = f"{cycle.cape:.1f}" if cycle.cape is not None else "n/a"
        buff_s = f"{cycle.buffett_pct:.0f}%" if cycle.buffett_pct is not None else "n/a"
        print(
            f"Market cycle: {cycle.label} | CAPE={cape_s} | Buffett={buff_s} ({cycle.note})"
        )
    except Exception:  # noqa: BLE001
        pass

    args.output.parent.mkdir(parents=True, exist_ok=True)
    any_rows = False
    single = args.mode in {
        "support",
        "catalyst",
        "fundamentals",
        "politicians",
        "trump",
        "athdip",
        "pead",
    }

    if args.mode in {"support", "both", "all"}:
        print("\n=== SUPPORT (near support / mean-reversion · Polymarket + 13F social) ===")
        if support_df.empty:
            print("No names passed support filters.")
        else:
            any_rows = True
            path = _save(
                support_df, args.output, "support", single and args.mode == "support"
            )
            _print_table(
                support_df,
                [
                    "ticker",
                    "price",
                    "rsi14",
                    "rsi_bias",
                    "proximity",
                    "pct_from_52w_high",
                    "pct_from_52w_low",
                    "pct_from_1w_high",
                    "sma_regime",
                    "news_score",
                    "polymarket_score",
                    "polymarket_markets",
                    "institutions_score",
                    "institutions_managers",
                    "fund_score",
                    "peg",
                    "fcf_yield",
                    "roe",
                    "composite",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(support_df)} rows → {path}")

    if args.mode in {"catalyst", "both", "all"}:
        print("\n=== CATALYST (news / earnings / momentum pop · Polymarket + 13F social) ===")
        if catalyst_df.empty:
            print("No names passed catalyst filters.")
        else:
            any_rows = True
            path = _save(
                catalyst_df,
                args.output,
                "catalyst",
                single and args.mode == "catalyst",
            )
            _print_table(
                catalyst_df,
                [
                    "ticker",
                    "price",
                    "rsi14",
                    "rsi_bias",
                    "day_return_pct",
                    "volume_ratio",
                    "pct_from_52w_high",
                    "pct_from_1w_high",
                    "news_score",
                    "polymarket_score",
                    "polymarket_markets",
                    "institutions_score",
                    "institutions_managers",
                    "momentum_score",
                    "social_score",
                    "fund_score",
                    "peg",
                    "fcf_yield",
                    "roe",
                    "composite",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(catalyst_df)} rows → {path}")

    if args.mode in {"fundamentals", "all"} or (
        not args.no_fundamentals and args.mode in {"both", "support", "catalyst"}
    ):
        print("\n=== FUNDAMENTALS (growth / profit / BS / ROIC / valuation) ===")
        if fund_df.empty:
            print("No names passed fundamentals filters.")
        else:
            any_rows = True
            path = _save(
                fund_df,
                args.output,
                "fundamentals",
                single and args.mode == "fundamentals",
            )
            _print_table(
                fund_df,
                [
                    "ticker",
                    "sector",
                    "trailing_pe",
                    "peg",
                    "fcf_yield",
                    "roe",
                    "gross_margin",
                    "profit_margin",
                    "growth_score",
                    "profitability_score",
                    "balance_score",
                    "capital_score",
                    "valuation_score",
                    "composite",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(fund_df)} rows → {path}")

    if args.mode in {"politicians", "all"} or (
        not args.no_politicians and args.mode in {"both", "support", "catalyst"}
    ):
        print(
            "\n=== POLITICIANS (Capitol Trades + House/Senate · freshness-weighted) ==="
        )
        if pol_df.empty:
            print("No politician-trade tickers scored.")
        else:
            any_rows = True
            path = _save(
                pol_df,
                args.output,
                "politicians",
                single and args.mode == "politicians",
            )
            _print_table(
                pol_df,
                [
                    "ticker",
                    "score",
                    "buy_count",
                    "sell_count",
                    "avg_filed_after",
                    "sources",
                    "politicians",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(pol_df)} rows → {path}")

    if args.mode in {"trump", "all"} or (
        not args.no_trump and args.mode in {"both", "support", "catalyst"}
    ):
        print(
            "\n=== TRUMP TRACKER (Truth + news + WH + optional X · freshness-weighted) ==="
        )
        if trump_df.empty:
            print("No Trump-linked tickers scored.")
        else:
            any_rows = True
            path = _save(
                trump_df,
                args.output,
                "trump",
                single and args.mode == "trump",
            )
            _print_table(
                trump_df,
                [
                    "ticker",
                    "score",
                    "mention_count",
                    "mention_weight",
                    "themes",
                    "sources",
                    "sample_text",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(trump_df)} rows → {path}")

    if args.mode in {"athdip", "all"}:
        print(
            f"\n=== ATHDIP (multi-year ATH ≤{args.athdip_max_ath_age}d ago · "
            f"4h RSI ≤ {args.athdip_rsi_max:.0f}) ==="
        )
        if athdip_df.empty:
            print(
                "No names passed athdip filters "
                f"(recent multi-year ATH setup + 4h RSI ≤ {args.athdip_rsi_max:.0f}). "
                "Try --athdip-rsi-max 35 or --athdip-max-ath-age 90 for a wider list."
            )
        else:
            any_rows = True
            path = _save(
                athdip_df,
                args.output,
                "athdip",
                single and args.mode == "athdip",
            )
            _print_table(
                athdip_df,
                [
                    "ticker",
                    "price",
                    "ath_high",
                    "prior_high",
                    "pct_from_ath",
                    "days_since_ath",
                    "rsi_4h",
                    "rsi14",
                    "sma_regime",
                    "pct_from_52w_high",
                    "composite",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(athdip_df)} rows → {path}")

    if args.mode == "pead" or (args.mode == "all" and not args.no_pead):
        print(
            f"\n=== PEAD (EPS surprise ≥ {args.pead_min_surprise:.0f}% · "
            f"EAR ≥ {args.pead_min_ear:.0f}% vs SPY · ≤{args.pead_max_sessions} sessions since print) ==="
        )
        if pead_df.empty:
            print(
                "No names passed PEAD filters. Outside earnings season this is normal; "
                "try --pead-min-ear 2 --pead-max-sessions 25 for a wider list."
            )
        else:
            any_rows = True
            path = _save(pead_df, args.output, "pead", single and args.mode == "pead")
            _print_table(
                pead_df,
                [
                    "ticker",
                    "price",
                    "reaction_date",
                    "sessions_since_reaction",
                    "surprise_pct",
                    "ear_pct",
                    "gap_pct",
                    "since_entry_pct",
                    "rsi14",
                    "sma_regime",
                    "composite",
                    "why",
                ],
                args.top,
            )
            print(f"Saved {len(pead_df)} rows → {path}")

    return 0 if any_rows else 1


if __name__ == "__main__":
    sys.exit(main())
