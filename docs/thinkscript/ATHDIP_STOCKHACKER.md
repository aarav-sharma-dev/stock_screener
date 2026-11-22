# Athdip thinkScript — Stock Hacker + Alert

## Corrected scan: two filters

Use **All of the following**, with two custom study filters:

1. Paste `athdip_scan_daily.ts`, aggregation **Day**. This checks that the
   highest daily high in available history occurred at most 63 sessions ago,
   and that the close on that ATH session exceeded the range preceding its
   21-session breakout window. Tied highs use the first occurrence, as in Python.
2. Paste `athdip_scan_rsi4h.ts`, aggregation **4 hours**, **EXT off**. This checks
   RSI(14) <= 31 on any bar dated from the latest bar's date minus five calendar
   days through that latest date, inclusive. It explicitly initializes its
   recursive state and does not require a long intraday ATH lookback.

Both replacements compiled in thinkorswim build 1994 on 2026-10-27. The RSI
filter alone returned 53 S&P 500 matches; the combined scan returned 11,
including FCX, KEYS, and VLO from that day's Slack alerts. The combined query
was saved as `AthDip4hRTH_SP500`. These counts are a snapshot, not a fixed
expected result or proof of exact production parity.

Save a query only after both filters are installed and checked. Editing a named study alone does not refresh
an existing scan's captured copy: replace its filter expression too.

The old `athdip_scan.ts` combines both conditions on 4h bars. Its 900-bar ATH
lookback exceeds the RTH history available to a 4h scan. Do not solve that by
silently shortening the ATH lookback, because that changes the setup definition.

### Limits when comparing with production

- Schwab limits 1–4h study scans to **270 calendar days**, and daily scans to
  **four years**. Production requests **five years** of daily prices, so even
  the two-filter version is an approximation when a higher high exists only
  in production's extra year. Exact parity needs a production-generated ATH
  setup watchlist as the scan universe, with only the 4h RSI filter in ToS.
- **S&P 500** excludes production's extra theme symbols. Match the universe
  before expecting all Slack symbols to appear.
- Stock Hacker disables **Start aggregations at market open** when EXT is off.
  Production groups RTH bars at 09:30/13:30 Eastern; compare a candidate's actual
  4h RSI in ToS before claiming exact agreement.
- Slack alerts are timestamped observations, not a complete current result set.
  Resolve production authentication/data errors before using later alerts as
  proof of fresh data.

Reference: [Schwab study-filter rules and data limits](https://toslc.thinkorswim.com/center/howToTos/thinkManual/Scan/Stock-Hacker/studyfilters.html).

## Legacy single-filter setup (superseded)

The instructions below describe the older scan. Use the two-filter setup above
for new installations; keep the chart study separate.

## Fix: `Secondary period not allowed: 4 hours`

That message means the script ToS is running still has
`AggregationPeriod.FOUR_HOURS` (usually an **old** `AthdipScan` study).

Stock Hacker **never** allows a secondary 4h period. True 4h RSI = set the
scan’s own aggregation to **4 hours** and use plain `RSI(close)` (no `period=`).

### Do this

1. **Studies → Edit studies** → find **AthdipScan** / any athdip scan → **Delete** it  
   (or rename; do not leave the old script loaded)
2. Stock Hacker → **gear / aggregation** → **4 hours** (not Day, not 1 hour)
3. Add condition → **thinkScript Editor** (not “Study → AthdipScan”)
4. Paste **only** `docs/thinkscript/athdip_scan.ts` → OK → Scan  

If you prefer a saved study: Create a **new** study with that same paste (new name
like `AthdipScan4h`), then filter Study → `scan` is true — still with scan agg = 4h.

### Chart study (`athdip_study.ts`)

Separate file. Use on a **4 Hour** chart only. It uses secondary **Day** for
SMA/ATH (allowed). Do **not** add `AthdipRTH` as a Stock Hacker study filter.

| | Stock Hacker | Chart |
|--|--------------|-------|
| Aggregation | **4 Hours**, extended hours off | **4 Hour** chart, extended hours off |
| RSI | lowest native 4h RSI in the last **5 calendar days** ≤ 31 | same |
| ATH age | **63 trading days**, counted once per day | same, daily high via secondary Day |

Paste the updated script over the old study. Thinkorswim keeps the previous copy until you replace it. A name stays true for the whole 5-day window, and the bubble prints on the first bar of that window.

### Alert

Scan results → Alert when new symbol enters.  
Or chart: `AthdipRTH` → plot **Trigger** is true.

### Exit overlays (`athdip_exits_study.ts`)

The production paper book (`scripts/paper_book.py`) tracks two exits after an
athdip trigger close: **hold 10 sessions** and **daily RSI(14) > 50**, with a
**20-session** stop. Thinkorswim cannot share that entry list with Stock Hacker,
so the chart study marks exits from a **manual** entry date:

1. Keep `athdip_study.ts` on a **4 Hour** chart for the entry bubble.
2. Open a **Day** chart of the same symbol.
3. Paste `athdip_exits_study.ts` as a new study.
4. Set **entrySession** to the trigger day's `YYYYMMDD` (the cash close you
   treat as the fill). Leave it at `0` to disable.
5. Yellow bubble = hold-10, magenta = first RSI>50 close after entry, red =
   session 20.

This is a chart overlay only. It does not place orders, and it does not open
positions from the scan. The live P&L books stay in
`output/paper_book.json` / `output/paper_book.csv` on the VM.
