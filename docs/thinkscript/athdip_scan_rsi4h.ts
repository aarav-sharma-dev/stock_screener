# ATHDIP RSI filter — Stock Hacker aggregation = 4 hours, EXT off.
# Combine with athdip_scan_daily.ts under ALL of the following.
# Includes every bar on dates from the latest bar's date minus 5 days
# through the latest bar's date, matching production's inclusive date window.

input rsiLength = 14;
input rsiMax = 31;
input rsiLookbackDays = 5;

def rsiVal = RSI(length = rsiLength, price = close);
def dipDate = CompoundValue(1,
    if !IsNaN(rsiVal) and rsiVal <= rsiMax then GetYYYYMMDD()
    else dipDate[1],
    if !IsNaN(rsiVal) and rsiVal <= rsiMax then GetYYYYMMDD() else 0);
# Keep the function argument valid even before the first RSI dip: thinkScript
# validates DaysFromDate arguments even inside an unselected conditional branch.
def dipAge = DaysFromDate(if dipDate > 0 then dipDate else GetYYYYMMDD());

plot scan = !IsNaN(close) and dipDate > 0
    and dipAge >= 0 and dipAge <= rsiLookbackDays;
