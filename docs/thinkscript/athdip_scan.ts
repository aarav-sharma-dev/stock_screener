# LEGACY single-filter scan: do not install for production comparison.
# Use athdip_scan_daily.ts (Day) AND athdip_scan_rsi4h.ts (4h, EXT off).
# Stock Hacker limits 4h scans to 270 calendar days; the 900-bar ATH
# request below exceeds available RTH history. Retained for comparison.
# Athdip — Stock Hacker (aggregation = 4 hours, EXT off)
# Intended conditions: ATH within 63 trading days, and the lowest 4h RSI in
# the last 5 calendar days <= 31. SMAs optional (off by default).
# Age is counted once per calendar day, so extra 4h bars do not shorten it.

input smaFastLength = 100;
input smaSlowLength = 400;
input rsiLength = 14;
input rsiMax = 31;
input athLookbackBars = 900;
input maxDaysSinceAth = 63;
input rsiLookbackDays = 5;
input requireAboveSmas = no;

def c = close;
def h = high;

def smaFast = Average(c, smaFastLength);
def smaSlow = Average(c, smaSlowLength);
def uptrend = c > smaFast and c > smaSlow;

def ath = Highest(h, athLookbackBars);
def newDay = GetYYYYMMDD() != GetYYYYMMDD()[1];
def daysSinceAth = if h >= ath then 0
    else if newDay then daysSinceAth[1] + 1
    else daysSinceAth[1];
def recentAth = daysSinceAth <= maxDaysSinceAth;

def rsiVal = RSI(length = rsiLength, price = c);
def barMs = GetTime();
def dipMs = if rsiVal <= rsiMax then barMs else dipMs[1];
def rsiDip = dipMs > 0 and barMs - dipMs <= rsiLookbackDays * 24 * 60 * 60 * 1000;

plot scan = recentAth and rsiDip and (!requireAboveSmas or uptrend);
