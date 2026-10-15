# Stock screener (long-only)

Python screener with six long-only modes + **VIX regime**:

1. **Support** — near support / SMA pullback (stricter in risk-off)  
2. **Catalyst** — news / earnings / volume pops (needs vol+green day in risk-off)  
3. **Fundamentals** — Feroldi-style growth, profitability, balance sheet, ROIC, valuation  
4. **Politicians** — Congress trades from **Capitol Trades scrape** + **official House/Senate** disclosures, weighted so **shorter `filed_after` days score higher**  
5. **Trump** — Truth Social (trumpstruth.org RSS) + Google News RSS + White House RSS + optional Grok X; maps cashtags / company names / policy themes → tickers  
6. **Athdip** — uptrend near multi-year ATH with **4h RSI ~30** (pass if ≤35)

Shared free inputs: Yahoo OHLCV + news (+ X-quoting Google News RSS) + Polymarket odds + curated 13F institutional holdings + earnings + fundamentals, `^VIX`, STOCK Act public filings, optional Reddit + Grok X.

## Free data sources

| Source | Use |
|--------|-----|
| NASDAQ Trader symbol dirs | Full US listed universe (`--universe us`) |
| Wikipedia | S&P 500 (`--universe sp500`) |
| Yahoo Finance / yfinance | OHLCV, VIX, news, earnings |
| Google News RSS | Per-ticker X/Twitter-quoted headlines (blended into support/catalyst news); also Trump / tariff / Fed / trade |
| Polymarket Gamma API | Per-ticker prediction-market odds (blended into support/catalyst social; sparse outside mega-caps) |
| SEC EDGAR 13F | Curated hedge-fund / manager holdings overlay (lagged quarterly; `why` tags like `13F: Berkshire (+2)`) |
| trumpstruth.org RSS | Truth Social mirror for @realDonaldTrump |
| whitehouse.gov RSS | Official news, presidential actions, briefings |
| Reddit public JSON / optional OAuth | Retail mention / polarity |
| xAI Grok API | Optional X sentiment + Trump X posts |

Reddit often blocks anonymous `.json` calls (403). Create a free Reddit “script” app and set `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (see `.env.example`) for reliable access. Without that, the screener continues with neutral Reddit scores.

Robinhood / SoFi / Public are not used (no free scan APIs). Webull OpenAPI is paid/approval-based and not wired in.

### Charles Schwab Trader API (optional)

Live quotes / history / L1 streamer via [Individual Trader API](https://developer.schwab.com/products/trader-api--individual) + `schwab-py`. Does **not** replace Yahoo for news/fundamentals.

```bash
pip install -e .
# Optional: WebSocket streamer needs Python ≥3.10 + schwab-py
# pip install -e '.[schwab]'
# Set SCHWAB_API_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL in .env
python scripts/schwab_auth.py          # browser OAuth → .schwab_token.json
python scripts/schwab_auth.py --manual # paste code if callback fails
python scripts/schwab_quotes.py AAPL MSFT
python scripts/schwab_quotes.py --history-4h NVDA --days 10
python scripts/schwab_stream_watchlist.py --from-focus --seconds 90

# Live athdip: Schwab RTH 4h RSI cross ≤31 (+ Yahoo compare)
python3 scripts/schwab_athdip_watch.py --once
python3 scripts/schwab_athdip_watch.py --interval 300   # every 5m
```

Athdip on thinkorswim (Stock Hacker + study alerts): see `docs/thinkscript/ATHDIP_STOCKHACKER.md`.

## Setup

```bash
cd ~/Projects/stock-screener
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install -e .
```

Optional Grok X enrichment:

```bash
export XAI_API_KEY=...
# optional: export XAI_MODEL=grok-4-1-fast-non-reasoning
```

## Run

```bash
# Both modes on a custom basket
stock-screener --mode both --tickers AAPL,MSFT,NVDA,META,AMD,GOOGL,AVGO --top 10 --no-reddit --no-grok

# Catalyst pops only
stock-screener --mode catalyst --universe sp500 --limit 80 --top 20

# Support dips only
stock-screener --mode support --tickers AAPL,MSFT,NVDA,JPM --allow-below-200

# Politicians (Capitol Trades + House PTRs; Senate best-effort)
stock-screener --mode politicians --politician-pages 3 --house-filings 20 --top 25

# Trump tracker (Truth + news + WH; add Grok for X if credits available)
stock-screener --mode trump --no-trump-x --top 25

# Near ATH + 4h RSI ~30 (dip in uptrend)
stock-screener --mode athdip --universe sp500 --limit 80 --top 25

# Post-earnings drift watchlist (EPS surprise ≥5% + reaction ≥3% vs SPY, ≤15 sessions old)
stock-screener --mode pead --universe sp500 --no-news

# Full scan the cron runs at 06:00 and 19:00 CT: Schwab OHLCV, Yahoo fills gaps
stock-screener --mode all --universe sp500 --limit 80 --top 25 \
  --allow-below-200 --no-reddit --no-grok --no-trump-x --price-source schwab
```

Politician freshness weight: `freshness = max(0, 1 - filed_after_days/45)` then  
`trade_w = ±log1p(size) * (0.25 + 0.75 * freshness)` — late filings barely count.

Trump tracker weights Truth Social > White House > X > news, with ~36h freshness half-life, then maps themes (tariffs, China, energy, defense, banks, etc.) to liquid tickers/ETFs.

Athdip: **setup** = multi-year ATH break (21d fresh high at ATH time); **trigger** = **min 4h RSI ≤ `--athdip-rsi-max`** over `--athdip-rsi-lookback` days (defaults **31** / **5**) while ATH is within `--athdip-max-ath-age` sessions (default **63**). Universe `--limit` keeps liquid mega-caps (NVDA, AMD, …) before alphabetical fill.

PEAD: reaction day **R** = first session after the print (≥16:00 ET → next day); **EAR** = close[R−1]→close[R+1] minus SPY; entry reference = close[R+1]. Runs inside `--mode all` (skip with `--no-pead`). Backtest (`scripts/backtest_pead.py`, S&P 500 2024→) found **no significant drift in large caps** — treat it as a watchlist, not an edge.

Results print to the terminal and write `output/screen.csv`.

### Daily dashboard (Today's focus)

After a screen run, rebuild the Cursor canvas + `output/today_focus.csv`:

```bash
python3 scripts/build_screen_dashboard.py
```

That also diffs vs `output/signal_snapshot.json` and alerts on **new** symbols (stdout + macOS notification; Slack if `SCREENER_SLACK_WEBHOOK` is set). Alerts include **BUY/SELL/MIXED** lean plus **daily RSI** (and **4h RSI** when athdip). First run only saves a baseline.

### Cron (VM) — Schwab every 30m + Yahoo open/close

Keep your existing crontab; only the **script** switches mid-day to Schwab:

```cron
CRON_TZ=America/Chicago
*/30 6-18 * * 1-5 /home/asharma/bin/stock-screener-alert.sh
0 19 * * 1-5 /home/asharma/bin/stock-screener-alert.sh
```

| When (CT) | Mode |
|-----------|------|
| **06:00** | Every mode on the full universe (S&P 500 plus the AI, chip, energy, nuclear, and ETF names). Prices and fundamentals come from **Schwab** only. Schwab has no headline feed, next earnings date, or EPS surprise, so those inputs stay neutral. A rejected Schwab login sends one Slack message per day. Writes `athdip_setups.csv` and `screen_athdip.csv` |
| **06:30–18:30** `*/30` | Athdip again on the **full universe** (S&P 500 plus the extra AI, chip, energy, nuclear, and ETF names). Alerts only tickers that were not already in the snapshot |
| **19:00** | Same full screen again (EOD new-symbol Slack) |

Deploy after `git pull` on the VM:

```bash
cp -a scripts/stock-screener-alert.sh ~/bin/stock-screener-alert.sh
chmod +x ~/bin/stock-screener-alert.sh
# optional: export SCREENER_ROOT=$HOME/workspace/stock-screener
# smoke: SCREENER_FORCE_SCHWAB=1 ~/bin/stock-screener-alert.sh
#        SCREENER_FORCE_FULL=1   ~/bin/stock-screener-alert.sh
```

Requires working Schwab OAuth token on the VM (`.schwab_token.json` + keys in `.env`).

Per-mode Slack filter (example: only athdip + support):

```bash
python3 scripts/alert_new_signals.py --modes athdip,support
# Schwab last prices are included automatically when `.schwab_token.json` exists
# Disable: --no-schwab-quotes   or   SCREENER_SLACK_SCHWAB_QUOTES=0
# or: export SCREENER_SLACK_MODES=athdip,support
```

Project skill (for agents / teammates): `.cursor/skills/daily-screen/SKILL.md` — covers the full reproduce path (screen → focus → canvas).

## Scoring defaults

| Pillar | Weight |
|--------|--------|
| S/R proximity | 30% |
| SMA regime | 20% |
| Market (VIX) | 15% |
| News (Yahoo ± X-quote RSS) | 15% |
| Earnings proximity | 10% |
| Social (Reddit ± Grok ± Polymarket ± 13F) | 10% |

Hard filters still drop illiquid names, tight ranges, resistance-hugging prices, below-SMA200 (unless `--allow-below-200`), death crosses, and news vetoes. X-quoting headlines tilt news score (~35% when both Yahoo and X-RSS fire); disable with `--no-x-quote-news`. Polymarket tilts social (~35% when open markets exist) and `why` notes source plus news/reddit agree/diverge; disable with `--no-polymarket`. Curated 13F managers tilt social (~30% when held) with `13F: …` tags; disable with `--no-institutions`. 13F data is quarterly and lagged (~45d); SEC fair-access needs a contact email in the User-Agent (`SCREENER_SEC_USER_AGENT`, default includes `research@example.com`).

## Notes

- Full US scans should use `--limit` or a nightly job; Yahoo rate-limits.  
- Reddit matching is noisy — treat as a soft signal.  
- Grok runs only on `--grok-top` survivors (default 15).  
- Not investment advice.
