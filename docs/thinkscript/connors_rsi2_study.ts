# ConnorsRSI2 — CHART only (Daily aggregation)
#
# Long entry when: close > SMA200, close < SMA5, RSI(2) < rsiMax.
# Classic exit cue: close crosses back above SMA5 (plot ExitMark).
# Alert: plot Trigger is true (or ExitMark for exit watch).

declare upper;

input smaSlowLength = 200;
input smaFastLength = 5;
input rsiLength = 2;
input rsiMax = 10;
input showBubbles = yes;

def c = close;
def smaSlow = Average(c, smaSlowLength);
def smaFast = Average(c, smaFastLength);
def rsi2 = RSI(length = rsiLength, price = c);

def entryOk = c > smaSlow and c < smaFast and rsi2 < rsiMax;
def exitOk = c > smaFast and c[1] <= smaFast[1];

plot SMA200 = smaSlow;
SMA200.SetDefaultColor(Color.YELLOW);

plot SMA5 = smaFast;
SMA5.SetDefaultColor(Color.CYAN);

plot TriggerMark = if entryOk then low * 0.995 else Double.NaN;
TriggerMark.SetPaintingStrategy(PaintingStrategy.POINTS);
TriggerMark.SetDefaultColor(Color.GREEN);
TriggerMark.SetLineWeight(4);

plot ExitMark = if exitOk then high * 1.005 else Double.NaN;
ExitMark.SetPaintingStrategy(PaintingStrategy.POINTS);
ExitMark.SetDefaultColor(Color.RED);
ExitMark.SetLineWeight(3);

AddChartBubble(
    showBubbles and entryOk and !entryOk[1],
    low,
    "RSI2 " + Round(rsi2, 0),
    Color.GREEN,
    no
);

plot Trigger = entryOk;
Trigger.Hide();
