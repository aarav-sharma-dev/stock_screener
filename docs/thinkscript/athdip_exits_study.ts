# AthDipExitsStudy — CHART only (Day aggregation)
#
# Overlay for the two exits in the Python paper book
# (scripts/paper_book.py / 2026 robustness test):
#   hold10  — 10 trading sessions after a manual entry mark
#   rsi50   — first later close with daily RSI(14) > 50
#   time20  — hard stop at 20 sessions
#
# Chart entry is not auto-detected. Thinkorswim cannot share one entry list
# with the Stock Hacker scan. After an athdip trigger, set entrySession to
# that day's YYYYMMDD (or leave 0 and use the chart's leftmost bar only for
# tests). Bubbles fire on the exit bars. This does not place orders.
#
# Use with AthDipStudy on a 4 Hour chart for the entry, then this study on
# a Day chart for the exit marks.

declare upper;

input rsiLength = 14;
input rsiExitLevel = 50;
input holdSessions = 10;
input timeStopSessions = 20;
# YYYYMMDD of the entry session (the trigger day's cash close). 0 = off.
input entrySession = 0;
input showBubbles = yes;

def entryOn = entrySession > 0 and GetYYYYMMDD() == entrySession;
def newDay = GetYYYYMMDD() != GetYYYYMMDD()[1];
def held = if entryOn then 0
    else if entrySession == 0 then Double.NaN
    else if IsNaN(held[1]) then Double.NaN
    else if newDay then held[1] + 1
    else held[1];

def rsi14 = RSI(length = rsiLength, price = close);
def holdExit = !IsNaN(held) and held == holdSessions;
def rsiExit = !IsNaN(held) and held >= 1 and rsi14 > rsiExitLevel
    and !(held[1] >= 1 and rsi14[1] > rsiExitLevel);
def timeExit = !IsNaN(held) and held == timeStopSessions;

plot EntryMark = if entryOn then low * 0.995 else Double.NaN;
EntryMark.SetPaintingStrategy(PaintingStrategy.POINTS);
EntryMark.SetDefaultColor(Color.CYAN);
EntryMark.SetLineWeight(4);

plot Hold10Mark = if holdExit then high * 1.005 else Double.NaN;
Hold10Mark.SetPaintingStrategy(PaintingStrategy.POINTS);
Hold10Mark.SetDefaultColor(Color.YELLOW);
Hold10Mark.SetLineWeight(4);

plot Rsi50Mark = if rsiExit then high * 1.01 else Double.NaN;
Rsi50Mark.SetPaintingStrategy(PaintingStrategy.POINTS);
Rsi50Mark.SetDefaultColor(Color.MAGENTA);
Rsi50Mark.SetLineWeight(4);

plot Time20Mark = if timeExit then high * 1.015 else Double.NaN;
Time20Mark.SetPaintingStrategy(PaintingStrategy.POINTS);
Time20Mark.SetDefaultColor(Color.RED);
Time20Mark.SetLineWeight(4);

AddChartBubble(
    showBubbles and holdExit,
    high,
    "hold " + holdSessions,
    Color.YELLOW,
    yes
);
AddChartBubble(
    showBubbles and rsiExit,
    high,
    "RSI>" + rsiExitLevel,
    Color.MAGENTA,
    yes
);
AddChartBubble(
    showBubbles and timeExit,
    high,
    "time " + timeStopSessions,
    Color.RED,
    yes
);

plot HeldSessions = held;
HeldSessions.Hide();
