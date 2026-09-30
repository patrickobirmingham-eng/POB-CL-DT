"""
Research backtest: intraday momentum on SPY ("noise area" breakout).

Replicates Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)" (2024) on Alpaca full-market (SIP)
minute bars, and reports results by year — including the period after the
paper was published (mid-2024), which is genuinely out-of-sample.

Rules:
  Noise area at minute m of day d:
    sigma(m) = average over the previous 14 days of |close(m) / open - 1|
    upper = max(open, prev close) * (1 + sigma(m))
    lower = min(open, prev close) * (1 - sigma(m))
  Decisions only at 10:00, 10:30, ..., 15:30 (using the price at that minute):
    flat:  price > upper -> long;  price < lower -> short
    long:  exit if price < max(upper, VWAP)   (then short if price < lower)
    short: exit if price > min(lower, VWAP)   (then long if price > upper)
  Everything is closed at the 16:00 close. No overnight positions.
  Size: equity * min(4, 2% / 14-day stdev of daily returns) / open price.

Costs: `--slip` $/share on every fill (default $0.01; SPY's spread is ~$0.01)
plus $0.0035/share commission-equivalent. Fixed $100k equity (no compounding)
so every year is sized the same.

Usage: python research_spy_momentum.py --start 2016-01-01 --symbols SPY,QQQ
Read-only: fetches market data, never places orders.
"""
import argparse
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment

ET = ZoneInfo("America/New_York")
EQUITY = 100_000.0
LOOKBACK = 14
TARGET_VOL = 0.02
MAX_LEV = 4.0
COMMISSION = 0.0035
PUBLISHED = "2024-06-01"   # results from here on are out-of-sample for the paper
# Price "at 10:00" = close of the 09:59 minute bar (it closes at 10:00:00), so
# each check reads the bar one minute earlier — no look-ahead.
CHECK_TIMES = [(datetime(2000, 1, 1, h, m) - timedelta(minutes=1)).strftime("%H:%M")
               for h in range(10, 16) for m in (0, 30)]


def fetch_minutes(client, symbol, start, end):
    frames = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=180), end)
        req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=cur, end=nxt,
                               feed="sip", adjustment=Adjustment.ALL)
        for i in range(6):
            try:
                df = client.get_stock_bars(req).df
                break
            except Exception as e:
                if i == 5:
                    raise
                time.sleep(2 ** i)
        if df is not None and not df.empty:
            frames.append(df.reset_index())
        print(f"  {symbol}: fetched through {nxt.date()}")
        cur = nxt
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET)
    df = df.set_index("timestamp").sort_index()
    return df.between_time("09:30", "15:59")


def prepare_days(m: pd.DataFrame):
    """Returns list of per-day DataFrames indexed by 'HH:MM' with close, vwap,
    move-from-open, plus per-day open/prev_close/daily return."""
    days = []
    grid = pd.date_range("09:30", "15:59", freq="1min").strftime("%H:%M")
    for d, g in m.groupby(m.index.date):
        if len(g) < 300:     # skip half days / broken data
            continue
        g = g.copy()
        g["hhmm"] = g.index.strftime("%H:%M")
        g = g.set_index("hhmm").reindex(grid)
        g["close"] = g["close"].ffill()
        g["volume"] = g["volume"].fillna(0)
        for c in ("open", "high", "low"):
            g[c] = g[c].fillna(g["close"])
        typical = (g["high"] + g["low"] + g["close"]) / 3
        g["vwap"] = (typical * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
        g["vwap"] = g["vwap"].ffill().fillna(g["close"])
        day_open = float(g["open"].iloc[0])
        g["move"] = (g["close"] / day_open - 1).abs()
        days.append({"date": str(d), "open": day_open, "close": float(g["close"].iloc[-1]), "bars": g})
    for i, day in enumerate(days):
        day["prev_close"] = days[i - 1]["close"] if i else np.nan
        day["ret"] = day["close"] / day["prev_close"] - 1 if i else np.nan
    return days


def simulate(days, slip=0.01, long_only=False):
    trades, daily = [], []
    for i in range(LOOKBACK + 1, len(days)):
        day = days[i]
        hist = days[i - LOOKBACK:i]
        sigma = pd.concat([h["bars"]["move"] for h in hist], axis=1).mean(axis=1)
        rets = pd.Series([h["ret"] for h in hist]).dropna()
        vol = rets.std()
        if not np.isfinite(vol) or vol <= 0:
            continue
        b = day["bars"]
        o, pc = day["open"], day["prev_close"]
        upper = max(o, pc) * (1 + sigma)
        lower = min(o, pc) * (1 - sigma)
        shares = int(EQUITY * min(MAX_LEV, TARGET_VOL / vol) / o)
        pos, entry, pnl_day, n_tr = 0, 0.0, 0.0, 0

        def close_pos(price):
            nonlocal pos, pnl_day
            fill = price - slip if pos > 0 else price + slip
            pnl = (fill - entry) * shares * pos - COMMISSION * shares
            pnl_day += pnl
            trades.append({"date": day["date"], "side": "long" if pos > 0 else "short", "entry": entry,
                           "exit": fill, "shares": shares, "pnl": pnl})
            pos = 0

        def open_pos(side, price):
            nonlocal pos, entry, pnl_day, n_tr
            pos = side
            entry = price + slip if side > 0 else price - slip
            pnl_day -= COMMISSION * shares
            n_tr += 1

        for t in CHECK_TIMES:
            px, ub, lb, vw = b.at[t, "close"], upper[t], lower[t], b.at[t, "vwap"]
            if pos > 0 and px < max(ub, vw):
                close_pos(px)
            elif pos < 0 and px > min(lb, vw):
                close_pos(px)
            if pos == 0:
                if px > ub:
                    open_pos(1, px)
                elif px < lb and not long_only:
                    open_pos(-1, px)
        if pos != 0:
            close_pos(b["close"].iloc[-1])
        daily.append({"date": day["date"], "pnl": pnl_day, "traded": n_tr > 0, "bh": day["ret"]})
    return pd.DataFrame(trades), pd.DataFrame(daily)


def summarize(daily: pd.DataFrame):
    r = daily["pnl"] / EQUITY
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0
    curve = EQUITY + daily["pnl"].cumsum()
    peak = curve.cummax().clip(lower=EQUITY)
    return {"ret": r.sum(), "ann": r.mean() * 252, "sharpe": sharpe, "dd": ((peak - curve) / peak).max(),
            "days": len(daily), "traded": int(daily["traded"].sum()), "bh": daily["bh"].sum()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--symbols", default="SPY,QQQ")
    args = ap.parse_args()
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY.")
    client = StockHistoricalDataClient(key, secret)
    start = datetime.fromisoformat(args.start).replace(tzinfo=ET)
    end = datetime.now(ET) - timedelta(minutes=20)

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("## Research: intraday momentum (noise-area breakout), Zarattini, Aziz & Barbon rules")
    out()
    out(f"Full-market SIP minute bars from {args.start}. Fixed ${EQUITY:,.0f} equity, size = min(4x, 2% target "
        f"daily vol), $0.0035/share commission + slippage per fill. Returns are simple sums of daily P&L / equity. "
        f"Out-of-sample = on/after {PUBLISHED} (after publication).")
    verdicts = []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        print(f"Fetching {sym}...")
        days = prepare_days(fetch_minutes(client, sym, start, end))
        for label, kw in ((f"{sym}", {}), (f"{sym}, double slippage", {"slip": 0.02}), (f"{sym}, long only", {"long_only": True})):
            trades, daily = simulate(days, **kw)
            if daily.empty:
                continue
            daily["year"] = daily["date"].str[:4]
            out()
            out(f"### {label}")
            out()
            out("| Period | Days | Days traded | Return | Annualized | Sharpe | Max DD | SPY/ETF buy & hold |")
            out("|---|---:|---:|---:|---:|---:|---:|---:|")
            groups = [(y, g) for y, g in daily.groupby("year")]
            groups += [("In-sample (before publication)", daily[daily["date"] < PUBLISHED]),
                       ("**Out-of-sample (after publication)**", daily[daily["date"] >= PUBLISHED]),
                       ("**All**", daily)]
            for name, g in groups:
                if g.empty:
                    continue
                s = summarize(g)
                out(f"| {name} | {s['days']} | {s['traded']} | {s['ret']:+.1%} | {s['ann']:+.1%} | {s['sharpe']:.2f} | "
                    f"{s['dd']:.1%} | {s['bh']:+.1%} |")
            if trades is not None and not trades.empty:
                trades.to_csv(f"research_momentum_{label.lower().replace(', ', '_').replace(' ', '_')}.csv", index=False)
            oos = summarize(daily[daily["date"] >= PUBLISHED]) if (daily["date"] >= PUBLISHED).any() else None
            years_pos = sum(1 for _, g in daily.groupby("year") if g["pnl"].sum() > 0)
            verdicts.append((label, oos, years_pos, daily["year"].nunique()))

    out()
    out("### Verdict")
    out()
    for label, oos, yp, yn in verdicts:
        if oos is None:
            continue
        good = oos["sharpe"] >= 0.75 and oos["ret"] > 0 and yp >= 0.7 * yn
        out(f"- **{label}:** out-of-sample Sharpe {oos['sharpe']:.2f}, return {oos['ret']:+.1%}, "
            f"max DD {oos['dd']:.1%}; profitable in {yp}/{yn} years -> "
            f"{'PASSES (candidate for paper trading)' if good else 'does not pass'}")
    out()
    out("Pass rule: out-of-sample Sharpe >= 0.75, positive out-of-sample return, profitable in >= 70% of years. "
        "Caveats: decisions use the close of the minute bar at each check time; fills at that price +/- slippage.")

    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
