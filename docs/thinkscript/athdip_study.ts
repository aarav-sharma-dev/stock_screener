# AthDipStudy — CHART only (4 Hour aggregation, RTH)
#
# Matches production screener defaults:
#   setup  = multi-year-ish ATH within maxDaysSinceAth (daily)
#   trigger = 4h RSI <= rsiMax
# Uptrend (above SMA50 & SMA200) is OPTIONAL — off by default (same as Python
# athdip_require_uptrend=False). AMP-style pullbacks below SMA50 still alert.
#
# Alert: plot Trigger is true

declare upper;

input smaFastLength = 50;
input smaSlowLength = 200;
input rsiLength = 14;
input rsiMax = 31;
input athLookbackDays = 756;
input maxDaysSinceAth = 63;
input requireAboveSmas = no;   # set yes to also require close > SMA50 & SMA200
input showBubbles = yes;

def cD = close(period = AggregationPeriod.DAY);
def hD = high(period = AggregationPeriod.DAY);

def smaFast = Average(cD, smaFastLength);
def smaSlow = Average(cD, smaSlowLength);
def uptrend = cD > smaFast and cD > smaSlow;

def ath = Highest(hD, athLookbackDays);
def barsSinceAth = if hD >= ath then 0 else barsSinceAth[1] + 1;
def recentAth = barsSinceAth <= maxDaysSinceAth;

def rsi4h = RSI(length = rsiLength, price = close);
def rsiDip = rsi4h <= rsiMax;

def athdipOk = recentAth and rsiDip and (!requireAboveSmas or uptrend);

plot SMA50 = smaFast;
SMA50.SetDefaultColor(Color.CYAN);

plot SMA200 = smaSlow;
SMA200.SetDefaultColor(Color.YELLOW);

plot AthLevel = ath;
AthLevel.SetDefaultColor(Color.WHITE);
AthLevel.SetStyle(Curve.SHORT_DASH);

plot TriggerMark = if athdipOk then low * 0.995 else Double.NaN;
TriggerMark.SetPaintingStrategy(PaintingStrategy.POINTS);
TriggerMark.SetDefaultColor(Color.CYAN);
TriggerMark.SetLineWeight(4);

AddChartBubble(
    showBubbles and athdipOk and !athdipOk[1],
    low,
    "Athdip " + Round(rsi4h, 0),
    Color.CYAN,
    no
);

plot Trigger = athdipOk;
Trigger.Hide();
