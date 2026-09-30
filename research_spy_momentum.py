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

import momentum_strategy as ms

ET = ZoneInfo("America/New_York")
EQUITY = 100_000.0
LOOKBACK = 14
TARGET_VOL = 0.02
MAX_LEV = 4.0
COMMISSION = 0.0035
PUBLISHED = "2024-06-01"   # results from here on are out-of-sample for the paper


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
    """Per-session prepared frames (see momentum_strategy.prepare_day) plus
    open / close / previous close."""
    days = []
    for d, g in m.groupby(m.index.date):
        if len(g) < 300:     # skip half days / broken data
            continue
        bars, day_open = ms.prepare_day(g)
        days.append({"date": str(d), "open": day_open, "close": float(bars["close"].iloc[-1]), "bars": bars})
    for i, day in enumerate(days):
        day["prev_close"] = days[i - 1]["close"] if i else np.nan
        day["ret"] = day["close"] / day["prev_close"] - 1 if i else np.nan
    return days


def simulate(days, slip=0.01, long_only=False, target_vol=TARGET_VOL, max_lev=MAX_LEV, every=30):
    trades, daily = [], []
    for i in range(LOOKBACK + 1, len(days)):
        day = days[i]
        hist = days[i - LOOKBACK:i]
        sigma = ms.sigma_profile([h["bars"] for h in hist])
        vol = ms.daily_vol([h["close"] for h in days[i - LOOKBACK - 1:i]])
        if not np.isfinite(vol) or vol <= 0:
            continue
        b = day["bars"]
        o, pc = day["open"], day["prev_close"]
        upper, lower = ms.bands(o, pc, sigma)
        shares = ms.position_size(EQUITY, o, vol, target_vol, max_lev)
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

        for t in (ms.CHECK_TIMES if every == 30 else ms.make_check_times(every)):
            bar = ms.check_bar(t)
            px = b.at[bar, "close"]
            exit_now, new_side = ms.decide(pos, px, upper[bar], lower[bar], b.at[bar, "vwap"],
                                           allow_shorts=not long_only)
            if exit_now:
                close_pos(px)
            if new_side:
                open_pos(new_side, px)
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


def compare_intervals(client, sym, start, end, intervals, out):
    """Same rules, different decision frequency, on one symbol."""
    days = prepare_days(fetch_minutes(client, sym, start, end))
    out()
    out(f"### {sym}: decision interval comparison")
    out()
    out("| Check every | Trades | Days traded | All: return | All: Sharpe | All: max DD | Out-of-sample return | "
        "Out-of-sample Sharpe | OOS Sharpe, 2x slippage | Profitable years |")
    out("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    rows = {}
    for m in intervals:
        trades, daily = simulate(days, every=m)
        _, daily2 = simulate(days, every=m, slip=0.02)
        daily["year"] = daily["date"].str[:4]
        a = summarize(daily)
        o = summarize(daily[daily["date"] >= PUBLISHED])
        o2 = summarize(daily2[daily2["date"] >= PUBLISHED])
        yp = sum(1 for _, g in daily.groupby("year") if g["pnl"].sum() > 0)
        yn = daily["year"].nunique()
        rows[m] = (a, o, o2, yp, yn)
        out(f"| {m} min | {len(trades)} | {a['traded']} | {a['ret']:+.1%} | {a['sharpe']:.2f} | {a['dd']:.1%} | "
            f"{o['ret']:+.1%} | {o['sharpe']:.2f} | {o2['sharpe']:.2f} | {yp}/{yn} |")
        trades.to_csv(f"research_momentum_{sym.lower()}_{m}min.csv", index=False)
    base = rows.get(30)
    out()
    if base:
        better = [m for m, (a, o, o2, yp, yn) in rows.items() if m != 30
                  and o["sharpe"] >= base[1]["sharpe"] + 0.2 and a["sharpe"] >= base[0]["sharpe"] + 0.1
                  and o2["sharpe"] >= base[2]["sharpe"] and yp >= base[3]]
        if better:
            out(f"**Verdict:** {', '.join(f'{m} min' for m in better)} beat the 30-minute schedule clearly and "
                f"consistently (higher Sharpe overall and after publication, robust to doubled costs, at least as "
                f"many profitable years).")
        else:
            out("**Verdict:** no interval clearly beats the 30-minute schedule on all counts (overall Sharpe, "
                "after-publication Sharpe, doubled costs, profitable years). Keep 30 minutes.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--intervals", default="", help="e.g. 10,15,30,60: compare decision frequencies instead")
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

    if args.intervals:
        out("## Research: intraday momentum — how often should the bot decide?")
        out()
        out(f"Same rules, sizing and costs as the main study; only the decision interval changes "
            f"(10:00 to 15:30 ET). Out-of-sample = on/after {PUBLISHED}.")
        for sym in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
            compare_intervals(client, sym, start, end, [int(x) for x in args.intervals.split(",")], out)
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a") as f:
                f.write("\n".join(lines) + "\n")
        return

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
