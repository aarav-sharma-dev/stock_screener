---
name: strategy-research
description: >-
  Skeptical, data-driven trading strategy research for this repo: peer
  literature, same-engine YTD backtests, ToS thinkScript, and ranked pick
  lists from Stock Hacker scans. Use when the user says research strategy,
  compare peers, backtest vs Connors/Fallen Angels, rank this scan list,
  which name from the scan, or validate a new edge.
---

# Strategy research (stock-screener)

Adapted from a general “elite trading research analyst” prompt — scoped to
**this repo’s engines**, not a blank-slate 3–10 strategy factory.

## Stance

- Skeptical, data-driven, deployable. Prefer evidence over narrative.
- Separate **facts** (backtest numbers, scan membership) from **interpretation**.
- Never claim an edge without a same-engine (or clearly caveated) backtest.
- Account for costs/slippage when claiming production readiness; if the engine
  omits them, say so in the write-up.
- Do **not** treat social sentiment as primary validation.
- Not personalized financial advice — rank by rules, don’t say “you should buy X”.

## When to use which path

| User ask | Do this |
|----------|---------|
| Daily screen / dashboard | Follow `.cursor/skills/daily-screen` instead |
| Peer / literature compare | Run peer scripts + canvas; cite public cousins |
| Faithful ToS script vs ours | Implement rules in Python; compare YTD |
| “Best from this scan list” | Rank by **that strategy’s** score (not vibes) |
| New idea | Ideate → code → backtest → canvas; keep 1–3 variants max |

## Repo tools (prefer these)

```bash
# Athdip vs peers (Connors, Fallen Angels loose, ATH proxies) — at 0 / 5 / 10 bps per side
.venv/bin/python scripts/compare_athdip_peers_ytd.py

# Faithful FallenAngels_Scanner.ts vs athdip
.venv/bin/python scripts/compare_fallen_angels_ytd.py

# SMA gate ablation
.venv/bin/python scripts/compare_athdip_sma_ytd.py

# Mode YTD, with per-side cost in percent (0.1 = 10 bps)
.venv/bin/python scripts/backtest_ytd.py --modes athdip --limit 80 --top 10 --cost 0.1

# Athdip robustness: costs × regime gates (VIX≥20, SPY126>0) × IBS≤0.25 × exits (hold N, RSI14>50, >SMA5)
.venv/bin/python scripts/experiment_athdip_robustness.py --limit 80          # YTD
.venv/bin/python scripts/experiment_athdip_robustness.py --limit 80 --months 3

# PEAD event study + fixed-hold portfolio (full S&P 500, 2024→)
.venv/bin/python scripts/backtest_pead.py --limit 500 --start 2024-01-01

# Production PEAD watchlist (also runs inside --mode all; --no-pead to skip)
.venv/bin/python -m stock_screener.cli --mode pead --universe sp500 --no-news

# Live athdip crosses (Schwab)
.venv/bin/python scripts/schwab_athdip_watch.py --once --source screen
```

Config hooks (all **off by default**, tested and rejected for athdip in 2026 — see
Findings): `ScreenerConfig.athdip_ibs_max`, `athdip_gate_vix_min`,
`athdip_gate_spx_mom_days`. PEAD knobs: `pead_min_surprise_pct` (5),
`pead_min_ear_pct` (3), `pead_max_sessions_since_report` (15), CLI
`--pead-min-surprise/--pead-min-ear/--pead-max-sessions/--pead-require-drift`.

Cost model (`backtest_ytd.turnover_cost`): entries pay cost × new weight, exits
pay cost × old weight, carried names pay nothing. Always report at least 0 and
10 bps/side; high-turnover rules (Connors ~830 signals/yr) flip sign at 10 bps.

ThinkScript lives in `docs/thinkscript/`:

| Strategy | Scan (Stock Hacker) | Chart study | Setup doc |
|----------|---------------------|-------------|-----------|
| Athdip | `athdip_scan.ts` (**4h** agg) | `athdip_study.ts` | `ATHDIP_STOCKHACKER.md` |
| Connors RSI(2) | `connors_rsi2_scan.ts` (**Day**) | `connors_rsi2_study.ts` | `CONNORS_RSI2_STOCKHACKER.md` |

Outputs: `output/*compare*.csv`, `output/athdip_*`, `output/athdip_robustness_*.csv`,
`output/pead_*.csv`, canvases under `~/.cursor/projects/.../canvases/`
(`athdip-peer-strategy-compare`, `athdip-robustness-pead`).

## Findings to date (don't re-test blindly; extend instead)

2026 YTD, S&P 500 limit 80, SPY +14.3%:

- **Costs**: athdip +19.9 pp → +13.7 (5 bps) → +7.8 (10 bps) excess; Connors
  +24.2 → +11.1 → −0.8. Sparse exposure is what survives costs.
- **Gates/IBS (pre-registered, rejected)**: SPY126>0 keeps 88% of trades, excess
  +8.5 pp; IBS≤0.25 keeps 53%, −2.5 pp; VIX≥20 keeps 27%, −2.1 pp; combos worse.
  The best dips were in the Q1 correction when momentum gates were off.
- **Exits** (position sim, 1/10 slots): hold-10 +28.8% (70 trades, avg +3.8%),
  RSI14>50 exit +25.2% (73% win), hold-1 +9.3%. Longer holds are cost-robust.
  → candidate for forward test, not yet in thinkScript.
- **PEAD** (4,051 S&P 500 prints 2024→): PROD bucket 20d +0.21% (t 0.6), 60d
  +0.37% (t 0.6); nothing |t|>2. Large-cap PEAD is a watchlist, not an edge.
- **Iran / oil-shock** (`scripts/experiment_iran_oil_shock.py`, war window from
  2026-02-28, shock = WTI ≥ +3%): the move is same-day. Producers +2.1% excess
  (94% hit), refiners +2.2%, airlines −1.1%; next session is flat. SKYQ
  same-day median +3.7% but next-day median −3.9%, corr with WTI only 0.25,
  ~$4M median dollar volume, and the war-window round-trip is about flat
  ($2.66 → $2.52) after a $12.59 April spike. Do not trade the headline after
  the close. Defense does not beta to these oil days (trump lexicon's
  `iran war` → LMT/RTX basket is the wrong channel).

## Process (keep short)

1. **Frame** — Which market/universe? Which existing mode (athdip / Connors / …)?
2. **Peers** — Search for public cousins; note exact vs proxy (README ≠ full .ts).
3. **Implement** — One clear rule set in Python matching the claim; reuse
   `backtest_ytd` / peer runners when possible.
4. **Backtest** — Same universe/engine as peers (default S&P500 limit=80, top=10).
   Report return, excess vs SPY, max DD, avg holdings, signal count. Label YTD
   vs longer windows.
5. **Rank picks** (scan lists) — Score by strategy rules, e.g. Connors → lowest
   RSI(2) among names still `> SMA200` and `< SMA5`; flag quality (EPS, liquidity).
   Cross-check `output/screen_athdip.csv` / `today_focus.csv` for multi-mode hits.
6. **Deliver** — Prefer a Cursor canvas for tables/charts; include “what could go
   wrong” (survivorship, costs, regime, overfitting).

## Evaluation bar (honest)

Call a variant **interesting** only if most of these hold in-sample *and* you
state out-of-sample / cost caveats:

- Beats SPY on the tested window **or** clearly better risk (DD / exposure)
- Max DD discussed (repo engines often omit Sharpe; compute if easy)
- Plausible economic thesis (ATH pullback, trend+oversold, etc.)
- Implementable: liquid names, ToS/Schwab path exists or is sketched

Hard rejects: curve-fit on &lt;1 year with no peer/ablation; “All Stocks” scans
without liquidity; claiming ToS parity without reading the actual `.ts`.

## Output skeleton

1. **Verdict** — 2–3 sentences
2. **Rules** — entry / exit / universe
3. **Backtest table** — vs SPY and vs named peers
4. **Pick ranking** (if a scan list) — top 3–5 with metric columns
5. **Deploy** — thinkScript path, cron/watcher, alert seeding
6. **What could go wrong** — 3 bullets max

## Final check

- Edge real or overfitting?
- Same engine as the peer, or apples-to-oranges?
- Costs/slippage stated?
- Would a down-regime break this? What evidence would kill the thesis?
