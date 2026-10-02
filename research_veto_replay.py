"""
Replays the ORB trades the AI filter vetoed (or would have vetoed in shadow
mode) on real 1-minute bars, as if plain ORB had taken them — to judge whether
the AI's vetoes saved money or cost money.

For each VETO row in ai_decisions.csv on the given date:
  entry  = the logged entry price, at the logged time
  stop   = the logged stop price (moved to breakeven once price reaches +1R,
           like live_bot.apply_breakeven_stops)
  target = entry + REWARD_RISK_MULTIPLE x risk (plain ORB's bracket target)
  exit   = whichever is hit first on later minute bars (stop wins if both hit
           in the same bar — the conservative assumption), else the price at
           FLATTEN_TIME (or the latest bar, if the day isn't over yet).
  size   = strategy.position_size(equity, risk) — the live sizing rules.

Reports every trade plus totals, split into vetoes before the prior-day data
fix (10:09 ET on 2026-10-02) and after it, and the "realistic" subset plain
ORB could actually have taken (first MAX_TRADES_PER_DAY, one per symbol).

Read-only: fetches market data, never places orders.
Usage: python research_veto_replay.py --date 2026-10-02 [--equity 1020000]
"""
import argparse
import csv
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
import strategy

ET = ZoneInfo("America/New_York")
FIX_TIME = {"2026-10-02": "10:09:03"}   # when the corrected code started trading that day


def load_vetoes(path, day):
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["timestamp"].startswith(day) and r["action"] == "VETO"]
    for r in rows:
        r["ts"] = datetime.fromisoformat(r["timestamp"]).astimezone(ET)
    return sorted(rows, key=lambda r: r["ts"])


def fetch_bars(client, symbols, day):
    start = datetime.combine(day, dtime(9, 30), tzinfo=ET)
    end = min(datetime.combine(day, dtime(16, 0), tzinfo=ET), datetime.now(ET) - timedelta(minutes=16))
    out = {}
    for i in range(0, len(symbols), 50):
        req = StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Minute,
                               start=start, end=end, feed="sip")
        df = client.get_stock_bars(req).df
        if df is None or df.empty:
            continue
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET)
        for sym, g in df.groupby("symbol"):
            out[sym] = g.set_index("timestamp").sort_index()
    return out


def replay(row, bars, equity):
    entry, stop = float(row["entry_price"]), float(row["stop_price"])
    long = row["direction"] == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    rr = float(config.REWARD_RISK_MULTIPLE)
    target = entry + rr * risk if long else entry - rr * risk
    be_at = entry + float(getattr(config, "BREAKEVEN_TRIGGER_R", 1.0)) * risk * (1 if long else -1)
    shares = strategy.position_size(equity, risk)
    if shares <= 0:
        return None
    flatten = datetime.combine(row["ts"].date(), strategy.flatten_time(), tzinfo=ET)
    # Bars that start after the signal minute (the order fills at/around the signal price).
    later = bars[(bars.index > row["ts"].replace(second=0, microsecond=0)) & (bars.index < flatten)]
    exit_px, how, cur_stop, exit_ts = None, None, stop, None
    for ts, b in later.iterrows():
        hit_stop = b["low"] <= cur_stop if long else b["high"] >= cur_stop
        hit_tgt = b["high"] >= target if long else b["low"] <= target
        if hit_stop:
            exit_px, how, exit_ts = cur_stop, ("breakeven stop" if cur_stop == entry else "stop"), ts
            break
        if hit_tgt:
            exit_px, how, exit_ts = target, "target", ts
            break
        if cur_stop != entry and (b["high"] >= be_at if long else b["low"] <= be_at):
            cur_stop = entry   # takes effect from the next bar, like the live bot's polling
    if exit_px is None:
        if later.empty:
            return None
        exit_px, exit_ts = float(later["close"].iloc[-1]), later.index[-1]
        how = "3:45 close" if exit_ts >= flatten - timedelta(minutes=2) else f"still open ({exit_ts:%H:%M})"
    pl = (exit_px - entry) * shares * (1 if long else -1)
    return {"symbol": row["symbol"], "time": row["ts"].strftime("%H:%M"), "source": row["source"],
            "conf": row["confidence"], "entry": entry, "stop": stop, "target": round(target, 2),
            "shares": shares, "exit": round(exit_px, 2), "how": how, "pl": round(pl, 2),
            "r": round((exit_px - entry) / risk * (1 if long else -1), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--equity", type=float, default=1_020_000)
    args = ap.parse_args()
    day = datetime.fromisoformat(args.date).date()

    vetoes = load_vetoes("ai_decisions.csv", args.date)
    client = StockHistoricalDataClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"))
    bars = fetch_bars(client, sorted({r["symbol"] for r in vetoes}), day)
    fix = FIX_TIME.get(args.date, "00:00:00")

    results = []
    for r in vetoes:
        if r["symbol"] in bars:
            res = replay(r, bars[r["symbol"]], args.equity)
            if res:
                res["after_fix"] = r["ts"].strftime("%H:%M:%S") >= fix
                results.append(res)

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out(f"## Replay of AI-vetoed ORB trades — {args.date}")
    out()
    out(f"Each veto replayed on SIP 1-minute bars as plain ORB would have traded it: logged entry/stop, "
        f"{config.REWARD_RISK_MULTIPLE:g}R target, stop to breakeven at +1R, flat by {config.FLATTEN_TIME}; "
        f"stop assumed first if a bar hits both. Size = live rules on ${args.equity:,.0f} equity. No commissions/slippage.")

    def table(rows, title):
        out()
        out(f"### {title}")
        out()
        if not rows:
            out("_None._")
            return
        out("| Time | Symbol | Vetoed by | AI conf | Entry | Stop | Target | Shares | Exit | How | R | P&L |")
        out("|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|")
        for x in rows:
            out(f"| {x['time']} | {x['symbol']} | {x['source']} | {x['conf']} | {x['entry']:.2f} | {x['stop']:.2f} | "
                f"{x['target']:.2f} | {x['shares']:,} | {x['exit']:.2f} | {x['how']} | {x['r']:+.2f} | {x['pl']:+,.2f} |")
        wins = sum(1 for x in rows if x["pl"] > 0)
        losses = sum(1 for x in rows if x["pl"] < 0)
        out()
        out(f"**{len(rows)} trades: {wins} winners, {losses} losers, total {sum(x['pl'] for x in rows):+,.2f} "
            f"({sum(x['r'] for x in rows):+.2f}R).** A negative total means the vetoes saved money; positive means they cost money.")

    after = [x for x in results if x["after_fix"]]
    before = [x for x in results if not x["after_fix"]]
    table(after, "After the data fix (the vetoes that count)")
    # What plain ORB could actually have taken: one per symbol, first MAX_TRADES_PER_DAY of the day.
    seen, realistic = set(), []
    for x in sorted(results, key=lambda x: x["time"]):
        if x["symbol"] not in seen and len(realistic) < int(config.MAX_TRADES_PER_DAY):
            seen.add(x["symbol"])
            realistic.append(x)
    table(realistic, f"Realistic plain-ORB day (first {config.MAX_TRADES_PER_DAY} signals, one per symbol, whole day)")
    table(before, "Before the fix (made on wrong prior-day data — shown for completeness)")

    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    pd.DataFrame(results).to_csv(f"research_veto_replay_{args.date}.csv", index=False)


if __name__ == "__main__":
    main()
