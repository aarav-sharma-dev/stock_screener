# ATHDIP setup filter — Stock Hacker aggregation = Day.
# Combine with athdip_scan_rsi4h.ts under ALL of the following.
# Uses all available daily scan history (Schwab caps it at 4 years).
# Production requests 5 years, so this is a scanner approximation.
# Scanner only: HighestAll uses the whole loaded range, not a backtest signal.

input maxDaysSinceAth = 63;
input freshHighDays = 21;

def bn = BarNumber();
def ath = HighestAll(high);
# Production uses the first occurrence when highs tie.
def athBar = LowestAll(if high == ath then bn else Double.NaN);
def daysSinceAth = bn - athBar;
def athClose = HighestAll(if bn == athBar then close else Double.NaN);
# At the ATH bar, exclude the most recent 21 sessions from the prior range.
def priorHigh = HighestAll(if bn <= athBar - freshHighDays
    then high else Double.NaN);

plot scan = !IsNaN(close)
    and athBar >= freshHighDays + 2
    and daysSinceAth >= 0
    and daysSinceAth <= maxDaysSinceAth
    and priorHigh > 0
    and athClose > priorHigh;
