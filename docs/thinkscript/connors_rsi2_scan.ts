# Connors RSI(2) — Stock Hacker (aggregation = Day)
#
# Matches scripts/compare_athdip_peers_ytd.py peer "connors_rsi2":
#   close > SMA200  AND  close < SMA5  AND  RSI(2) < rsiMax
# plus liquidity gates so "All Stocks" does not return hundreds of ADRs.
#
# Stock Hacker: set "Scan in" → S&P 500 (same universe family as our YTD peer).
# NOT the ConnorsRSI composite. Plain RSI(2) only.

input smaSlowLength = 200;
input smaFastLength = 5;
input rsiLength = 2;
input rsiMax = 10;
input minPrice = 10.0;
input minAvgVolume = 1000000;
input avgVolumeLength = 50;

def c = close;
def aboveSlow = c > Average(c, smaSlowLength);
def belowFast = c < Average(c, smaFastLength);
def rsi2 = RSI(length = rsiLength, price = c);
def liquid = c >= minPrice and Average(volume, avgVolumeLength) >= minAvgVolume;

plot scan = aboveSlow and belowFast and rsi2 < rsiMax and liquid;
