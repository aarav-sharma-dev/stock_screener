# Athdip thinkScript — Stock Hacker + Alert

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
| Aggregation | **4 Hours** | **4 Hour** chart |
| RSI | native bars | native bars |
| SMA / ATH | scaled bar counts | secondary Day |

### Alert

Scan results → Alert when new symbol enters.  
Or chart: `AthdipRTH` → plot **Trigger** is true.
