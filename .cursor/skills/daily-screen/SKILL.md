---
name: daily-screen
description: >-
  Run the stock-screener all-mode daily refresh, build Today's focus
  (intersections/conflicts + single-mode leaders with fundamentals), and
  regenerate the Cursor screen dashboard canvas. Use when the user says
  update for today, refresh the screen, rebuild dashboard, today's focus,
  or reproduce the screener dashboard.
---

# Daily screen + dashboard

## Goal

Anyone with this repo can reproduce today's multi-mode screen and the **Today's focus** dashboard section.

## Reproduce (canonical)

```bash
cd /path/to/stock-screener
source .venv/bin/activate

# 1) Live screen on the full S&P 500 plus theme extras (writes output/screen_*.csv
# and output/athdip_setups.csv).
stock-screener --mode all --universe sp500 --top 25 \
  --allow-below-200 --no-reddit --no-grok --no-trump-x \
  --price-source schwab \
  --output output/screen.csv 2>&1 | tee output/run_today.log

# 2) Build focus CSV + Cursor canvas + wiki section (+ new-symbol alerts)
python3 scripts/build_screen_dashboard.py

# Optional: alert-only re-check (same as end of dashboard build)
# python3 scripts/alert_new_signals.py
```

Optional YTD backtest (slow):

```bash
python3 scripts/backtest_ytd.py --modes support,catalyst,value,trump,athdip --limit 80 --top 10 \
  2>&1 | tee output/backtest_ytd_all.log
# Optionally save summary JSON for the canvas YTD chart:
# output/backtest_ytd_summary.json
```

## New-symbol alerts

After each dashboard build, `scripts/alert_new_signals.py` diffs current
`output/screen_*.csv` + `today_focus.csv` against `output/signal_snapshot.json`.

- **First run** seeds a baseline (no alert flood).
- Later runs alert on tickers that were **not** in the previous snapshot.
- Channels: stdout always; macOS Notification Center on Darwin; Slack if
  `SCREENER_SLACK_WEBHOOK` or `SLACK_WEBHOOK_URL` is set in `.env`.

Near-real-time: on the VM cron, mid-day `*/30` reruns athdip on the full universe.
Full screens at **06:00** and **19:00** CT use `--price-source schwab` for prices
and fundamentals. Schwab has no headline feed, next earnings date, or EPS
surprise, so those inputs stay neutral. A rejected Schwab login Slacks once per day.
Locally you can still use Cursor `/loop 30m` with this skill or:

```bash
python3 scripts/schwab_athdip_watch.py --once --source setups --no-compare
```

```bash
python3 scripts/alert_new_signals.py          # after a screen run
python3 scripts/alert_new_signals.py --reset  # re-baseline
python3 scripts/alert_new_signals.py --dry-run
```

## Outputs

| Path | Purpose |
|------|---------|
| `output/screen_*.csv` | Per-mode screener results |
| `output/today_focus.csv` | Today's focus rows (intersections + singles + fundamentals) |
| `output/signal_snapshot.json` | Previous signal set for new-symbol alerts |
| `output/run_today.log` | VIX / Trump feed counts for the canvas header |
| `docs/screen-dashboard.canvas.tsx` | Repo copy of the dashboard (git-friendly) |
| Cursor `canvases/screen-dashboard.canvas.tsx` | Live IDE canvas (managed folder) |
| `wiki/Home.md` | Markdown dashboard including Today's focus |

## Today's focus rules

The builder (`scripts/build_screen_dashboard.py`) picks:

1. **Athdip hard passes** — only if **trigger day is today** OR **current price < trigger price**
2. **Athdip ∩ politicians** — `conflict` if sells > buys, else `confirm` / `intersection`
3. **Support ≥ 75 and fundamentals ≥ 70** — intersection
4. **Top single-mode leaders** — support, catalyst, trump, politicians (fundamentals if room)

Each row includes mode scores when present plus **sector / PE / PEG / FCF% / ROE%** (from screener CSVs, else Yahoo fill-in).

## Agent checklist

When the user asks to "update for today":

1. Run the screener command above (network required).
2. Run `python3 scripts/build_screen_dashboard.py`.
3. Open the canvas: `canvases/screen-dashboard.canvas.tsx` (or the path printed by the script).
4. Summarize Today's focus briefly in chat (conflicts first).
5. If new-symbol alerts fired, list those tickers explicitly.

Do **not** hand-edit the canvas; regenerate it from CSVs so results stay reproducible.

## Notes

- `output/` is gitignored — commit the **scripts + skill + docs canvas**, not daily CSVs.
- Label results as a **watchlist**, not investment advice.
- Athdip setup = multi-year ATH break; trigger = min 4h RSI ≤ 31 over 5 calendar days while ATH age ≤ 63 sessions.
