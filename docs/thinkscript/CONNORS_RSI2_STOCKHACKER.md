# Connors RSI(2) thinkScript — Stock Hacker + Alert

Plain **RSI(2)** pullback in an uptrend (Larry Connors style). Same core rules as
the YTD peer in `scripts/compare_athdip_peers_ytd.py`:

`close > SMA200` **and** `close < SMA5` **and** `RSI(2) < 10`

Plus script defaults: price ≥ $10, 50d avg volume ≥ 1M.

This is **not** the ConnorsRSI composite indicator (price RSI + streak + %rank).

### Stock Hacker

1. Aggregation → **Day**
2. **Scan in** → **S&P 500** (not “All Stocks” — that alone can return 500+)
3. Add condition → thinkScript Editor
4. Paste **only** `docs/thinkscript/connors_rsi2_scan.ts` → OK → Scan  
5. Optional: Alert when new symbol enters

Optional Stock filters (if still wide): Last ≥ 10, Avg Volume ≥ 1M.

### Why you saw ~525 hits

`Scan in: All Stocks` + RSI(2)&lt;10 after a weak day matches many ADRs / thin names
(e.g. AAVMY, AIBRF). Our backtest used liquid S&P names only — switch the universe
and re-paste the updated scan (liquidity baked in).

### Chart study

Daily chart → paste `docs/thinkscript/connors_rsi2_study.ts`.  
Alert on plot **Trigger** (entry). **ExitMark** = close reclaim of SMA5.

| | Stock Hacker | Chart |
|--|--------------|-------|
| Aggregation | **Day** | **Daily** chart |
| Entry | `scan` true | `Trigger` true |
| Exit cue | — | `ExitMark` / close > SMA5 |

### vs athdip

| | Connors RSI(2) | Athdip |
|--|----------------|--------|
| Agg | Day | **4 Hours** |
| Setup | Above SMA200 | Recent multi-year ATH |
| Trigger | RSI(2) &lt; 10 | RSI(14) ≤ 31 |
| Scripts | `connors_rsi2_*.ts` | `athdip_*.ts` |
