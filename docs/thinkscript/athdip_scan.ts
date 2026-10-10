# Athdip — Stock Hacker (aggregation = 4 hours, EXT off)
# Matches production: recent ATH + RSI<=31; SMAs optional (off by default).

input smaFastLength = 100;
input smaSlowLength = 400;
input rsiLength = 14;
input rsiMax = 31;
input athLookbackBars = 900;
input maxBarsSinceAth = 126;
input requireAboveSmas = no;

def c = close;
def h = high;

def smaFast = Average(c, smaFastLength);
def smaSlow = Average(c, smaSlowLength);
def uptrend = c > smaFast and c > smaSlow;

def ath = Highest(h, athLookbackBars);
def barsSinceAth = if h >= ath then 0 else barsSinceAth[1] + 1;
def recentAth = barsSinceAth <= maxBarsSinceAth;

def rsiVal = RSI(length = rsiLength, price = c);

plot scan = recentAth and rsiVal <= rsiMax and (!requireAboveSmas or uptrend);
