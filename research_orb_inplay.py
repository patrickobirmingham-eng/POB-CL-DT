"""
Research backtest: 5-minute Opening Range Breakout on "Stocks in Play".

Replicates the rules published by Zarattini, Barbon & Aziz, "A Profitable Day
Trading Strategy For The U.S. Equity Market" (2024), on recent data, so we can
see whether the edge they reported (2016-2023) still holds before trading it.

Rules (paper):
  Universe, each day, using only prior days' data:
    - open price > $5
    - 14-day average daily volume >= 1,000,000 shares
    - 14-day ATR > $0.50
  Stocks in play: relative volume = volume of the first 5-minute bar today /
    average first-5-minute volume over the previous 14 days. Keep RV >= 1.0 and
    take the 20 highest.
  Direction: first 5-minute candle green -> long only; red -> short only; flat -> skip.
  Entry: stop order at the 5-minute high (long) / low (short), any time after 9:35.
  Stop: 10% of the 14-day ATR from the entry price.
  Exit: at the close if the stop is not hit. No profit target.
  Sizing: capital split across 20 slots; each trade risks 1% of its slot, with
    notional capped at 4x the slot (the paper's leverage limit).

Data: Alpaca SIP (full-market) bars, free for data older than 15 minutes.
Costs: $0.01/share slippage on every stop-order fill (entry and stop exit);
fills gap through to the bar open when price jumps past a level. When the
entry bar also touches the stop, the trade is counted as stopped (pessimistic).

Known biases: the universe is today's active assets (delisted names missing),
so results lean slightly optimistic; ETFs are not excluded.

Usage:  python research_orb_inplay.py --days 250
Read-only: fetches market data, never places orders.
"""
import argparse
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

ET = ZoneInfo("America/New_York")
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
LOOKBACK = 14
SLIP = 0.01            # $/share per stop-order fill
EQUITY = 100_000.0
SLOTS = 20
RISK_PER_SLOT = 0.01   # 1% of a slot's capital
LEVERAGE = 4.0


# ---------------------------------------------------------------- data fetch
def _retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 2 ** i
            print(f"  request failed ({type(e).__name__}: {str(e)[:120]}); retrying in {wait}s")
            time.sleep(wait)


def get_bars(client, symbols, timeframe, start, end, adjustment=Adjustment.SPLIT):
    req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=timeframe, start=start,
                           end=end, feed="sip", adjustment=adjustment)
    df = _retry(lambda: client.get_stock_bars(req).df)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET)
    return df


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def universe_symbols(trading):
    assets = _retry(lambda: trading.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)))
    out = []
    for a in assets:
        ex = getattr(a.exchange, "value", str(a.exchange))
        if a.tradable and ex in EXCHANGES and a.symbol.isalpha():
            out.append(a.symbol)
    return sorted(out)


def daily_stats(client, symbols, start, end):
    """Per symbol per day: open, 14d avg volume, 14d ATR (both from PRIOR days)."""
    frames = []
    for i, part in enumerate(chunks(symbols, 400)):
        print(f"  daily bars {i * 400 + len(part)}/{len(symbols)}")
        d = get_bars(client, part, TimeFrame.Day, start, end)
        if not d.empty:
            frames.append(d)
    d = pd.concat(frames, ignore_index=True)
    d["date"] = d["timestamp"].dt.strftime("%Y-%m-%d")
    d = d.sort_values(["symbol", "date"])
    g = d.groupby("symbol")
    prev_close = g["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev_close).abs(), (d["low"] - prev_close).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.groupby(d["symbol"]).transform(lambda s: s.shift(1).rolling(LOOKBACK, min_periods=LOOKBACK).mean())
    d["avgvol14"] = g["volume"].transform(lambda s: s.shift(1).rolling(LOOKBACK, min_periods=LOOKBACK).mean())
    return d[["symbol", "date", "open", "atr14", "avgvol14"]]


def first5_bars(client, eligible_by_day):
    """First 5-minute bar (9:30-9:35) for every eligible symbol, per day."""
    rows = []
    days = sorted(eligible_by_day)
    for n, day in enumerate(days):
        syms = eligible_by_day[day]
        start = datetime.combine(pd.Timestamp(day).date(), dtime(9, 30), tzinfo=ET)
        for part in chunks(syms, 400):
            b = get_bars(client, part, TimeFrame(5, TimeFrameUnit.Minute), start, start + timedelta(minutes=5))
            if not b.empty:
                b = b[b["timestamp"] == start]
                rows.append(b[["symbol", "open", "high", "low", "close", "volume"]].assign(date=day))
        if n % 20 == 0:
            print(f"  first-5-min bars: day {n + 1}/{len(days)}")
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------- simulation
def select_in_play(f5: pd.DataFrame, daily: pd.DataFrame, eligible: pd.DataFrame,
                   top: int = 20, min_rv: float = 1.0) -> pd.DataFrame:
    """Adds rel_volume (vs previous 14 days' first-5-min volume, no look-ahead),
    keeps only symbol-days that pass the daily filters, and returns the top `top`
    per day with RV >= min_rv."""
    f = f5.sort_values(["symbol", "date"]).copy()
    prior = f.groupby("symbol")["volume"].transform(
        lambda s: s.shift(1).rolling(LOOKBACK, min_periods=LOOKBACK).mean())
    f["rel_volume"] = f["volume"] / prior
    f = f.merge(daily[["symbol", "date", "atr14"]], on=["symbol", "date"], how="left")
    f = f.dropna(subset=["rel_volume", "atr14"])
    f = f.merge(eligible[["symbol", "date"]], on=["symbol", "date"], how="inner")
    f = f[f["rel_volume"] >= min_rv]
    return f.sort_values("rel_volume", ascending=False).groupby("date").head(top)


def simulate_trade(day_bars: pd.DataFrame, orb: dict, atr: float, stop_frac: float,
                   long_only: bool = False, slip: float = SLIP):
    """day_bars: 1-min bars after 9:35 for one symbol. Returns (direction, entry,
    exit, stop_dist) or None if no trade."""
    if orb["close"] > orb["open"]:
        direction = "long"
    elif orb["close"] < orb["open"] and not long_only:
        direction = "short"
    else:
        return None
    stop_dist = stop_frac * atr
    if stop_dist <= 0 or day_bars.empty:
        return None
    entry = stop = None
    for _, bar in day_bars.iterrows():
        if entry is None:
            if direction == "long" and bar["high"] >= orb["high"]:
                entry = max(orb["high"], bar["open"]) + slip
                stop = entry - stop_dist
                if bar["low"] <= stop:          # pessimistic: stopped in the entry bar
                    return direction, entry, stop - slip, stop_dist
            elif direction == "short" and bar["low"] <= orb["low"]:
                entry = min(orb["low"], bar["open"]) - slip
                stop = entry + stop_dist
                if bar["high"] >= stop:
                    return direction, entry, stop + slip, stop_dist
            continue
        if direction == "long":
            if bar["open"] <= stop:
                return direction, entry, bar["open"] - slip, stop_dist
            if bar["low"] <= stop:
                return direction, entry, stop - slip, stop_dist
        else:
            if bar["open"] >= stop:
                return direction, entry, bar["open"] + slip, stop_dist
            if bar["high"] >= stop:
                return direction, entry, stop + slip, stop_dist
    if entry is None:
        return None
    return direction, entry, float(day_bars["close"].iloc[-1]), stop_dist


def run_variant(selected, minute_bars, stop_frac=0.10, top=20, long_only=False, slip=SLIP):
    risk_dollars = EQUITY / SLOTS * RISK_PER_SLOT
    notional_cap = EQUITY / SLOTS * LEVERAGE
    trades = []
    sel = selected.groupby("date").head(top)
    for r in sel.itertuples():
        bars = minute_bars.get((r.symbol, r.date))
        if bars is None:
            continue
        res = simulate_trade(bars, {"open": r.open, "high": r.high, "low": r.low, "close": r.close},
                             r.atr14, stop_frac, long_only, slip)
        if res is None:
            continue
        direction, entry, exit_, stop_dist = res
        shares = int(min(risk_dollars / stop_dist, notional_cap / entry))
        if shares <= 0:
            continue
        pnl_ps = (exit_ - entry) if direction == "long" else (entry - exit_)
        pnl = shares * pnl_ps
        trades.append({"date": r.date, "symbol": r.symbol, "direction": direction, "entry": entry,
                       "exit": exit_, "shares": shares, "pnl": pnl, "r": pnl / risk_dollars,
                       "rel_volume": r.rel_volume})
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame, days):
    if t.empty:
        return None
    daily = t.groupby("date")["pnl"].sum().reindex(days, fill_value=0.0)
    curve = EQUITY + daily.cumsum()
    peak = curve.cummax().clip(lower=EQUITY)
    sharpe = (daily.mean() / daily.std() * (252 ** 0.5)) if daily.std() > 0 else 0.0
    mid = days[len(days) // 2]
    h1, h2 = t[t["date"] < mid], t[t["date"] >= mid]
    return {
        "trades": len(t), "win": (t["pnl"] > 0).mean(), "avg_r": t["r"].mean(),
        "pnl": t["pnl"].sum(), "dd": ((peak - curve) / peak).max(), "sharpe": sharpe,
        "r1": h1["r"].mean() if len(h1) else float("nan"),
        "r2": h2["r"].mean() if len(h2) else float("nan"),
        "p1": h1["pnl"].sum(), "p2": h2["pnl"].sum(),
    }


VARIANTS = [
    ("Paper rules (top 20, stop 10% ATR, both directions)", dict()),
    ("Long only", dict(long_only=True)),
    ("Top 10 only", dict(top=10)),
    ("Stop 5% ATR", dict(stop_frac=0.05)),
    ("Stop 20% ATR", dict(stop_frac=0.20)),
    ("Paper rules, double slippage ($0.02)", dict(slip=0.02)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="trading days to test")
    args = ap.parse_args()

    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY.")
    data = StockHistoricalDataClient(key, secret)
    trading = TradingClient(key, secret, paper=True)

    end = datetime.now(ET) - timedelta(minutes=20)  # free plan: SIP older than 15 min
    start = end - timedelta(days=int((args.days + LOOKBACK + 10) * 1.5))

    print("Loading universe...")
    symbols = universe_symbols(trading)
    print(f"  {len(symbols)} active US-listed symbols")

    print("Daily bars (price / volume / ATR filters)...")
    daily = daily_stats(data, symbols, start, end)
    all_days = sorted(daily["date"].unique())
    elig = daily[(daily["open"] > 5) & (daily["avgvol14"] >= 1_000_000) & (daily["atr14"] > 0.5)]
    # need LOOKBACK days of first-5-min history before the first tested day
    test_days = all_days[-args.days:]
    fetch_days = all_days[-(args.days + LOOKBACK):]
    elig = elig[elig["date"].isin(fetch_days)]
    # a symbol needs first-5-min history on prior days, so fetch every fetch day
    # for any symbol eligible on at least one test day
    syms_needed = set(elig[elig["date"].isin(test_days)]["symbol"])
    eligible_by_day = {d: sorted(syms_needed) for d in fetch_days}
    print(f"  {len(syms_needed)} symbols pass the filters on at least one test day")

    print("First 5-minute bars...")
    f5 = first5_bars(data, eligible_by_day)
    # Filters (price, avg volume, ATR) are applied per day before ranking.
    selected = select_in_play(f5, daily, elig)
    selected = selected[selected["date"].isin(test_days)]
    print(f"  {len(selected)} stock-days selected as in play over {selected['date'].nunique()} days")

    print("Minute bars for selected stocks...")
    minute_bars = {}
    for n, (day, g) in enumerate(selected.groupby("date")):
        d0 = pd.Timestamp(day).date()
        b = get_bars(data, g["symbol"].tolist(), TimeFrame.Minute,
                     datetime.combine(d0, dtime(9, 35), tzinfo=ET), datetime.combine(d0, dtime(16, 0), tzinfo=ET))
        if not b.empty:
            for sym, sb in b.groupby("symbol"):
                minute_bars[(sym, day)] = sb.set_index("timestamp").sort_index()
        if n % 20 == 0:
            print(f"  day {n + 1}/{selected['date'].nunique()}")

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    days = [d for d in test_days if d in set(selected["date"])] or test_days
    out("## Research: 5-minute ORB on Stocks in Play (Zarattini, Barbon & Aziz rules)")
    out()
    out(f"{len(days)} trading days ({days[0]} to {days[-1]}), full-market SIP data, universe of "
        f"{len(syms_needed)} liquid US stocks (price > $5, 14-day avg volume >= 1M, ATR > $0.50). "
        f"${EQUITY:,.0f} split into {SLOTS} slots, 1% of a slot risked per trade (${EQUITY / SLOTS * RISK_PER_SLOT:,.0f}), "
        f"max {LEVERAGE:.0f}x slot notional. Slippage ${SLIP:.2f}/share per stop fill.")
    out()
    out("| Variant | Trades | Win rate | Avg R | Total P&L | Return | Max DD | Sharpe | Avg R 1st half | Avg R 2nd half |")
    out("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    results = {}
    for label, kw in VARIANTS:
        t = run_variant(selected, minute_bars, **kw)
        results[label] = t
        s = stats(t, days)
        if s is None:
            out(f"| {label} | 0 | – | – | – | – | – | – | – | – |")
            continue
        out(f"| {label} | {s['trades']} | {s['win']:.1%} | {s['avg_r']:+.3f}R | ${s['pnl']:,.0f} | "
            f"{s['pnl'] / EQUITY:+.1%} | {s['dd']:.1%} | {s['sharpe']:.2f} | {s['r1']:+.3f}R | {s['r2']:+.3f}R |")
        t.to_csv(f"research_{label.split(' (')[0].lower().replace(' ', '_').replace(',', '').replace('%', 'pct').replace('$', '')}.csv", index=False)

    base = stats(results[VARIANTS[0][0]], days)
    dbl = stats(results["Paper rules, double slippage ($0.02)"], days)
    out()
    if base and base["avg_r"] > 0 and base["r1"] > 0 and base["r2"] > 0 and dbl and dbl["avg_r"] > 0 and base["trades"] >= 100:
        out(f"**Verdict:** the published strategy is profitable on recent data in both halves "
            f"({base['r1']:+.3f}R / {base['r2']:+.3f}R per trade, Sharpe {base['sharpe']:.2f}) and survives doubled "
            f"slippage ({dbl['avg_r']:+.3f}R). Candidate for paper trading.")
    else:
        out("**Verdict:** the published strategy did NOT hold up on recent data with these costs "
            "(negative or inconsistent expectancy, or not robust to doubled slippage). Do not trade it.")
    out()
    out("Caveats: survivorship bias (today's active symbols only), bar-level fills, no borrow checks for shorts, "
        "no market-impact model. Treat as evidence, not proof.")

    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
