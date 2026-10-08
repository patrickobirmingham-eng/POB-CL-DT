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


def simulate(days, slip=0.01, long_only=False, target_vol=TARGET_VOL, max_lev=MAX_LEV, every=30,
             take_profit=None, tp_reenter=False, stop_loss=None, lock=None):
    """take_profit: optional fraction (0.01 = +1%). A resting limit order sells
    (or covers) as soon as any minute bar reaches entry * (1 +/- take_profit),
    filled at the target price. tp_reenter: after a take-profit, keep taking
    new signals at later checks (False = done for the day).
    stop_loss: optional fraction. A resting catastrophe stop at entry * (1 -/+ stop_loss).
    lock: optional (activate, keep) fractions, e.g. (0.015, 0.5). Once the best
    move in our favour reaches `activate`, a resting stop locks in `keep` of that
    best move and ratchets up with each new high (down with each new low for shorts).
    Stops are checked on every minute bar and fill at the stop, or at the bar's
    open if it already gapped through. After a stop or lock exit, no new position
    in the same direction that day (opposite-side signals still trade)."""
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
        best = 0.0          # best move in our favour since entry, as a fraction
        blocked = set()     # sides not to re-enter today after a stop / lock exit

        def close_pos(price, reason="rule"):
            nonlocal pos, pnl_day
            fill = price - slip if pos > 0 else price + slip
            pnl = (fill - entry) * shares * pos - COMMISSION * shares
            pnl_day += pnl
            trades.append({"date": day["date"], "side": "long" if pos > 0 else "short", "entry": entry,
                           "exit": fill, "shares": shares, "pnl": pnl, "reason": reason})
            pos = 0

        def open_pos(side, price):
            nonlocal pos, entry, pnl_day, n_tr, best
            pos = side
            entry = price + slip if side > 0 else price - slip
            best = 0.0
            pnl_day -= COMMISSION * shares
            n_tr += 1

        def stop_level():
            """The resting stop price right now (None if there isn't one)."""
            levels = []
            if stop_loss is not None:
                levels.append(entry * (1 - pos * stop_loss))
            if lock is not None and best >= lock[0]:
                levels.append(entry * (1 + pos * lock[1] * best))
            if not levels:
                return None
            return max(levels) if pos > 0 else min(levels)

        check_bars = [ms.check_bar(t) for t in (ms.CHECK_TIMES if every == 30 else ms.make_check_times(every))]
        intrabar = take_profit is not None or stop_loss is not None or lock is not None
        if not intrabar:
            minutes = check_bars
        else:
            grid = list(b.index)
            minutes = grid[grid.index(check_bars[0]):]
            check_set = set(check_bars)
        tp_done = False
        for bar in minutes:
            if pos != 0 and (stop_loss is not None or lock is not None):
                # Resting stop, at the level set by bars before this one.
                stop = stop_level()
                if stop is not None:
                    bo = b.at[bar, "open"]
                    if pos > 0 and b.at[bar, "low"] <= stop:
                        blocked.add(1)
                        close_pos(min(bo, stop), "stop")
                    elif pos < 0 and b.at[bar, "high"] >= stop:
                        blocked.add(-1)
                        close_pos(max(bo, stop), "stop")
                if pos != 0:
                    fav = b.at[bar, "high"] / entry - 1 if pos > 0 else 1 - b.at[bar, "low"] / entry
                    best = max(best, fav)
            if take_profit is not None and pos != 0:
                # Resting take-profit limit, checked on every minute after entry.
                target = entry * (1 + take_profit) if pos > 0 else entry * (1 - take_profit)
                # A limit only surely fills if price trades THROUGH it, so require one cent beyond.
                hit = b.at[bar, "high"] >= target + 0.01 if pos > 0 else b.at[bar, "low"] <= target - 0.01
                if hit:
                    # Limit fill at exactly the target (close_pos subtracts slippage, so add it back).
                    close_pos(target + slip if pos > 0 else target - slip, "target")
                    tp_done = not tp_reenter
            if intrabar and bar not in check_set:
                continue
            px = b.at[bar, "close"]
            exit_now, new_side = ms.decide(pos, px, upper[bar], lower[bar], b.at[bar, "vwap"],
                                           allow_shorts=not long_only)
            if exit_now:
                close_pos(px)
            if new_side and not tp_done and new_side not in blocked:
                open_pos(new_side, px)
        if pos != 0:
            close_pos(b["close"].iloc[-1], "close")
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


def compare_take_profit(client, sym, start, end, levels, out):
    """Current rules vs the same rules plus a fixed take-profit target."""
    days = prepare_days(fetch_minutes(client, sym, start, end))
    out()
    out(f"### {sym}: take-profit targets vs the current trailing exit")
    out()
    out("| Exit rule | Trades | All: return | All: Sharpe | All: max DD | Avg winning day | Best day | "
        "Out-of-sample return | Out-of-sample Sharpe | OOS Sharpe, 2x slippage | Profitable years |")
    out("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    variants = [("Current (trailing line, no target)", None, False)]
    for lv in levels:
        variants.append((f"+{lv:g}% target, then done for the day", lv / 100, False))
        variants.append((f"+{lv:g}% target, may re-enter", lv / 100, True))
    rows = {}
    for label, tp, re_ in variants:
        trades, daily = simulate(days, take_profit=tp, tp_reenter=re_)
        _, daily2 = simulate(days, take_profit=tp, tp_reenter=re_, slip=0.02)
        daily["year"] = daily["date"].str[:4]
        a = summarize(daily)
        o = summarize(daily[daily["date"] >= PUBLISHED])
        o2 = summarize(daily2[daily2["date"] >= PUBLISHED])
        yp = sum(1 for _, g in daily.groupby("year") if g["pnl"].sum() > 0)
        yn = daily["year"].nunique()
        wins = daily[daily["pnl"] > 0]["pnl"] / EQUITY
        rows[label] = (a, o, o2, yp)
        out(f"| {label} | {len(trades)} | {a['ret']:+.1%} | {a['sharpe']:.2f} | {a['dd']:.1%} | "
            f"{wins.mean() if len(wins) else 0:+.2%} | {daily['pnl'].max() / EQUITY:+.2%} | "
            f"{o['ret']:+.1%} | {o['sharpe']:.2f} | {o2['sharpe']:.2f} | {yp}/{yn} |")
    base = rows[variants[0][0]]
    better = [l for l, (a, o, o2, yp) in rows.items() if l != variants[0][0]
              and o["sharpe"] >= base[1]["sharpe"] + 0.2 and a["sharpe"] >= base[0]["sharpe"] + 0.1
              and o2["sharpe"] >= base[2]["sharpe"] and yp >= base[3]]
    out()
    if better:
        out(f"**Verdict:** {'; '.join(better)} beat the current exit clearly and consistently "
            f"(higher Sharpe overall and after publication, robust to doubled costs, at least as many profitable years).")
    else:
        out("**Verdict:** no take-profit target clearly beats the current trailing exit on all counts "
            "(overall Sharpe, after-publication Sharpe, doubled costs, profitable years). Keep the current exit.")


# Exit variants for --compare-exits, fixed in advance (not tuned to the results):
# label, kind, simulate() kwargs.
EXIT_VARIANTS = [
    ("Current (trailing line, 30-min checks)", "base", {}),
    ("+ catastrophe stop 1.5%", "protect", {"stop_loss": 0.015}),
    ("+ catastrophe stop 2%", "protect", {"stop_loss": 0.02}),
    ("+ take-profit 1%, then done for the day", "target", {"take_profit": 0.01}),
    ("+ take-profit 2%, then done for the day", "target", {"take_profit": 0.02}),
    ("+ profit lock: from +1%, keep 50% of best", "protect", {"lock": (0.01, 0.5)}),
    ("+ profit lock: from +1.5%, keep 50% of best", "protect", {"lock": (0.015, 0.5)}),
]


def compare_exits(client, sym, start, end, out):
    """Current exit vs catastrophe stop, fixed take-profit and profit-lock stop."""
    days = prepare_days(fetch_minutes(client, sym, start, end))
    out()
    out(f"### {sym}: exit rules compared")
    out()
    out("| Exit rule | Trades | Stop/target exits | All: return | All: Sharpe | All: max DD | Worst day | "
        "Best day | Out-of-sample return | Out-of-sample Sharpe | OOS Sharpe, 2x slippage | Profitable years |")
    out("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    rows = {}
    for label, kind, kw in EXIT_VARIANTS:
        trades, daily = simulate(days, **kw)
        _, daily2 = simulate(days, slip=0.02, **kw)
        daily["year"] = daily["date"].str[:4]
        a = summarize(daily)
        o = summarize(daily[daily["date"] >= PUBLISHED])
        o2 = summarize(daily2[daily2["date"] >= PUBLISHED])
        yp = sum(1 for _, g in daily.groupby("year") if g["pnl"].sum() > 0)
        yn = daily["year"].nunique()
        worst, top = daily["pnl"].min() / EQUITY, daily["pnl"].max() / EQUITY
        hits = int(trades["reason"].isin(["stop", "target"]).sum()) if not trades.empty else 0
        rows[label] = (kind, a, o, o2, yp, worst)
        out(f"| {label} | {len(trades)} | {hits} | {a['ret']:+.1%} | {a['sharpe']:.2f} | {a['dd']:.1%} | "
            f"{worst:+.2%} | {top:+.2%} | {o['ret']:+.1%} | {o['sharpe']:.2f} | {o2['sharpe']:.2f} | {yp}/{yn} |")
        slug = label.lower().translate(str.maketrans({c: "_" for c in " +%,:()."}))
        trades.to_csv(f"research_momentum_{sym.lower()}_exit_{'_'.join(x for x in slug.split('_') if x)}.csv",
                      index=False)
    _, ba, bo, bo2, byp, bworst = rows[EXIT_VARIANTS[0][0]]
    out()
    for label, (kind, a, o, o2, yp, worst) in rows.items():
        if kind == "base":
            continue
        if kind == "protect":
            # Insurance: must not cost meaningful edge, and must actually cut the bad tail.
            ok = (a["sharpe"] >= ba["sharpe"] - 0.1 and o["sharpe"] >= bo["sharpe"] - 0.1
                  and o2["sharpe"] >= bo2["sharpe"] - 0.1 and yp >= byp
                  and (worst > bworst + 0.001 or a["dd"] < ba["dd"] - 0.01))
            why = ("costs little edge (Sharpe within 0.1 overall, after publication and with doubled costs, "
                   "as many profitable years) and cuts the worst day or max drawdown" if ok else
                   "either costs too much edge or doesn't reduce the worst day / max drawdown")
        else:
            # A target changes the payoff, so it has to clearly beat the current exit.
            ok = (o["sharpe"] >= bo["sharpe"] + 0.2 and a["sharpe"] >= ba["sharpe"] + 0.1
                  and o2["sharpe"] >= bo2["sharpe"] and yp >= byp)
            why = ("clearly beats the current exit" if ok else "doesn't clearly beat the current exit")
        out(f"- **{label}:** {'PASSES' if ok else 'does not pass'} — {why}.")
    out()
    out("Protective rules (catastrophe stop, profit lock) pass if they keep Sharpe within 0.1 of the current exit "
        "(overall, after publication, doubled costs), keep at least as many profitable years, and improve the worst "
        "day by 0.1%+ or max drawdown by 1%+. Take-profit targets must beat it clearly (Sharpe +0.2 after "
        "publication, +0.1 overall, no worse with doubled costs).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--intervals", default="", help="e.g. 10,15,30,60: compare decision frequencies instead")
    ap.add_argument("--take-profit", default="", help="e.g. 0.5,1,1.5,2 (percent): compare take-profit targets instead")
    ap.add_argument("--compare-exits", action="store_true",
                    help="compare the current exit with catastrophe stops, take-profits and a profit-lock stop")
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

    if args.compare_exits:
        out("## Research: intraday momentum — catastrophe stop, take-profit or profit lock?")
        out()
        out(f"Same rules, sizing and costs as the main study, plus resting orders checked on every minute bar: "
            f"stops fill at the stop (or the bar's open if it gapped through), targets only if price trades a cent "
            f"beyond. After a stop or lock exit there is no same-direction re-entry that day. Variants were fixed "
            f"before running. Out-of-sample = on/after {PUBLISHED}.")
        for sym in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
            compare_exits(client, sym, start, end, out)
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a") as f:
                f.write("\n".join(lines) + "\n")
        return

    if args.take_profit:
        out("## Research: intraday momentum — does a fixed take-profit target help?")
        out()
        out(f"Same rules, sizing and costs as the main study, plus a resting limit order at entry +/- the target "
            f"(filled at the target only if price trades a cent beyond it). Out-of-sample = on/after {PUBLISHED}.")
        for sym in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
            compare_take_profit(client, sym, start, end, [float(x) for x in args.take_profit.split(",")], out)
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a") as f:
                f.write("\n".join(lines) + "\n")
        return

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
