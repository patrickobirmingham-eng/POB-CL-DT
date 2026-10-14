"""
Download hourly price bars for a list of option contracts (plus hourly bars of
their underlying stocks) for offline research, e.g. replaying past credit
spreads under different exit rules.

Input (env CONTRACTS_B64): base64(gzip("OCC,YYYYMMDD,YYYYMMDD;...")) - each
contract with the first and last day to fetch. Only contract names and dates
are passed in; nothing about position size or results.
Output: option_bars.json.gz {"options": {occ: [[t,o,h,l,c,v],...]},
                             "stocks": {sym: [[t,o,h,l,c,v],...]}}
"""
import base64, gzip, json, os, sys, time
from datetime import datetime, timedelta, timezone

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

key, secret = os.environ["SPREADS_APCA_API_KEY_ID"], os.environ["SPREADS_APCA_API_SECRET_KEY"]
items = gzip.decompress(base64.b64decode(os.environ["CONTRACTS_B64"])).decode().split(";")
opt = OptionHistoricalDataClient(key, secret)
stk = StockHistoricalDataClient(key, secret)
now = datetime.now(timezone.utc)

def day(s):
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)

def rows(bars):
    return [[b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars]

out = {"options": {}, "stocks": {}}
und_win = {}
missing = 0
for i, it in enumerate(items):
    occ, a, b = it.split(",")
    start, end = day(a), min(day(b) + timedelta(days=1), now - timedelta(minutes=20))
    und = occ[:-15]
    lo, hi = und_win.get(und, (start, end))
    und_win[und] = (min(lo, start), max(hi, end))
    for attempt in range(3):
        try:
            res = opt.get_option_bars(OptionBarsRequest(symbol_or_symbols=occ, timeframe=TimeFrame.Hour, start=start, end=end))
            out["options"][occ] = rows(res.data.get(occ, []))
            break
        except Exception as e:
            print(f"{occ}: {e}", file=sys.stderr)
            time.sleep(3)
    if not out["options"].get(occ):
        missing += 1
    time.sleep(0.3)
    if i % 100 == 0:
        print(f"{i}/{len(items)} contracts", flush=True)
for und, (start, end) in und_win.items():
    try:
        res = stk.get_stock_bars(StockBarsRequest(symbol_or_symbols=und, timeframe=TimeFrame.Hour, start=start, end=end,
                                                  feed=DataFeed.IEX))
        out["stocks"][und] = rows(res.data.get(und, []))
    except Exception as e:
        print(f"{und}: {e}", file=sys.stderr)
    time.sleep(0.3)
with gzip.open("option_bars.json.gz", "wt") as f:
    json.dump(out, f)
print(f"done: {len(out['options'])} contracts ({missing} with no bars), {len(out['stocks'])} stocks")
