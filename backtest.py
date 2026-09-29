"""
Backtest the ORB strategy against historical minute bars pulled from Alpaca's
market data API.

Usage:
    python backtest.py --days 60 --equity 25000
    python backtest.py --days 90 --equity 100000 --compare-shorts
"""
import argparse
import os
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

import config
import strategy

load_dotenv()

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    raise SystemExit("Install dependencies first: pip install -r requirements.txt")


def get_client():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY in your .env file first.")
    return StockHistoricalDataClient(key, secret)


def fetch_minute_bars(client, symbols, days):
    end = datetime.utcnow()
    start = end - timedelta(days=int(days * 1.6))  # pad for weekends/holidays
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=config.DATA_FEED,
    )
    bars = client.get_stock_bars(req).df
    if bars.empty:
        raise SystemExit("No bar data returned — check your API keys and symbols.")
    bars = bars.reset_index()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"]).dt.tz_convert("US/Eastern")
    return bars


def simulate(bars: pd.DataFrame, starting_equity: float, compound: bool = True):
    """compound=False sizes every trade off starting_equity instead of the running
    equity. Symbols are simulated one after another, so with compounding a
    symbol's position sizes depend on how earlier symbols did; fixed sizing
    removes that ordering effect, which matters when comparing two variants."""
    equity = starting_equity
    trades = []
    equity_curve = [(bars["timestamp"].min(), equity)]

    for symbol, sym_bars in bars.groupby("symbol"):
        sym_bars = sym_bars.set_index("timestamp").sort_index()
        sym_bars["date"] = sym_bars.index.date

        for session_date, day_bars in sym_bars.groupby("date"):
            day_bars = day_bars.between_time("09:30", "16:00")
            if day_bars.empty:
                continue

            orange = strategy.compute_opening_range(day_bars, symbol, str(session_date))
            if orange is None:
                continue

            or_end = day_bars.index[0] + pd.Timedelta(minutes=config.OPENING_RANGE_MINUTES)
            post_or = day_bars[day_bars.index >= or_end]

            traded_today = False
            open_trade = None

            for ts, bar in post_or.iterrows():
                if open_trade is None:
                    sig = strategy.check_breakout(orange, bar, ts, traded_today)
                    if sig is not None:
                        shares = strategy.position_size(equity if compound else starting_equity,
                                                        sig.risk_per_share)
                        # same hard per-trade notional cap the live bot applies
                        shares = min(shares, int(config.MAX_NOTIONAL_PER_TRADE // sig.entry_price))
                        if shares > 0:
                            fill_price = sig.entry_price * (
                                1 + config.BACKTEST_SLIPPAGE_PCT if sig.direction == "long"
                                else 1 - config.BACKTEST_SLIPPAGE_PCT
                            )
                            open_trade = {
                                "symbol": symbol, "date": str(session_date),
                                "direction": sig.direction, "entry_time": ts,
                                "entry_price": fill_price, "stop": sig.stop_price,
                                "target": sig.target_price, "shares": shares,
                            }
                            traded_today = True
                    continue

                # manage open trade: check stop/target/time-stop intrabar using high/low
                d = open_trade
                hit_stop = (bar["low"] <= d["stop"]) if d["direction"] == "long" else (bar["high"] >= d["stop"])
                hit_target = (bar["high"] >= d["target"]) if d["direction"] == "long" else (bar["low"] <= d["target"])
                time_stop = ts.time() >= strategy.flatten_time()

                exit_price = None
                exit_reason = None
                if hit_stop and hit_target:
                    # conservative assumption: stop hit first within the bar
                    exit_price, exit_reason = d["stop"], "stop"
                elif hit_stop:
                    exit_price, exit_reason = d["stop"], "stop"
                elif hit_target:
                    exit_price, exit_reason = d["target"], "target"
                elif time_stop:
                    exit_price, exit_reason = bar["close"], "time_stop"

                if exit_price is not None:
                    slip = config.BACKTEST_SLIPPAGE_PCT
                    fill = exit_price * (1 - slip if d["direction"] == "long" else 1 + slip)
                    pnl_per_share = (fill - d["entry_price"]) if d["direction"] == "long" else (d["entry_price"] - fill)
                    pnl = pnl_per_share * d["shares"] - config.BACKTEST_COMMISSION_PER_TRADE
                    risk_dollars = abs(d["entry_price"] - d["stop"]) * d["shares"]
                    r_multiple = pnl / risk_dollars if risk_dollars else 0.0

                    equity += pnl
                    trades.append({
                        **d, "exit_time": ts, "exit_price": fill,
                        "exit_reason": exit_reason, "pnl": pnl, "r_multiple": r_multiple,
                        "equity_after": equity,
                    })
                    equity_curve.append((ts, equity))
                    open_trade = None

            # force-close any trade still open at end of day data
            if open_trade is not None:
                d = open_trade
                last_bar = day_bars.iloc[-1]
                fill = last_bar["close"]
                pnl_per_share = (fill - d["entry_price"]) if d["direction"] == "long" else (d["entry_price"] - fill)
                pnl = pnl_per_share * d["shares"]
                risk_dollars = abs(d["entry_price"] - d["stop"]) * d["shares"]
                r_multiple = pnl / risk_dollars if risk_dollars else 0.0
                equity += pnl
                trades.append({
                    **d, "exit_time": day_bars.index[-1], "exit_price": fill,
                    "exit_reason": "eod_force_close", "pnl": pnl, "r_multiple": r_multiple,
                    "equity_after": equity,
                })
                equity_curve.append((day_bars.index[-1], equity))

    return pd.DataFrame(trades), pd.DataFrame(equity_curve, columns=["timestamp", "equity"])


def summarize(trades: pd.DataFrame, starting_equity: float):
    if trades.empty:
        print("No trades were generated by the strategy over this period.")
        return

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    total_pnl = trades["pnl"].sum()
    win_rate = len(wins) / len(trades)
    avg_r = trades["r_multiple"].mean()
    expectancy = avg_r  # in R terms

    equity_series = trades["equity_after"]
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_dd = drawdown.min()

    print("=" * 60)
    print(f"Trades:            {len(trades)}")
    print(f"Win rate:          {win_rate:.1%}")
    print(f"Avg win:           ${wins['pnl'].mean():.2f}" if len(wins) else "Avg win:           n/a")
    print(f"Avg loss:          ${losses['pnl'].mean():.2f}" if len(losses) else "Avg loss:          n/a")
    print(f"Avg R-multiple:    {avg_r:.2f}R   <- this is the number that matters most")
    print(f"Total P&L:         ${total_pnl:,.2f}")
    print(f"Return on equity:  {total_pnl / starting_equity:.1%}")
    print(f"Max drawdown:      {max_dd:.1%}")
    print("=" * 60)
    if avg_r <= 0:
        print("Average R-multiple is <= 0: this configuration has negative expectancy over")
        print("this period. Do not paper-trade it live as-is — adjust config.py and re-test,")
        print("or try a different watchlist/timeframe.")
    else:
        print("Positive expectancy over this backtest window. That's a necessary, not")
        print("sufficient, condition — test across multiple periods before trusting it.")


def _apply_daily_trade_cap(trades: pd.DataFrame) -> pd.DataFrame:
    """Keep only the first MAX_TRADES_PER_DAY entries per day across all symbols,
    as the live bot would (approximation: ignores the concurrent-position cap,
    the daily notional cap and the daily-loss breaker)."""
    if trades.empty:
        return trades
    t = trades.sort_values("entry_time")
    return t.groupby("date", group_keys=False).head(int(config.MAX_TRADES_PER_DAY)).reset_index(drop=True)


def _stats(t: pd.DataFrame, equity: float) -> dict:
    if t.empty:
        return {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "pnl": 0.0, "max_dd": 0.0,
                "worst_day": 0.0, "losing_days": 0, "days": 0}
    t = t.sort_values("exit_time")
    curve = equity + t["pnl"].cumsum()
    peak = curve.cummax().clip(lower=equity)
    daily = t.groupby("date")["pnl"].sum()
    return {
        "trades": len(t),
        "win_rate": (t["pnl"] > 0).mean(),
        "avg_r": t["r_multiple"].mean(),
        "pnl": t["pnl"].sum(),
        "max_dd": ((peak - curve) / peak).max(),
        "worst_day": daily.min(),
        "losing_days": int((daily < 0).sum()),
        "days": int(daily.size),
    }


def compare_shorts(bars: pd.DataFrame, equity: float, market_symbol: str = "QQQ"):
    """Runs the same data twice, long-only vs long+short, with fixed sizing and
    the daily trade cap, and reports the difference overall and on up vs down
    market days (by market_symbol's open-to-close move)."""
    market = bars[bars["symbol"] == market_symbol]
    trade_bars = bars[bars["symbol"] != market_symbol]
    mkt_dir = {}
    if not market.empty:
        m = market.set_index("timestamp").sort_index().between_time("09:30", "16:00")
        for d, g in m.groupby(m.index.date):
            mkt_dir[str(d)] = "up" if g["close"].iloc[-1] >= g["open"].iloc[0] else "down"

    original = config.ALLOW_SHORTS
    results = {}
    try:
        for label, allow in (("Long only", False), ("Long + short", True)):
            config.ALLOW_SHORTS = allow
            print(f"Simulating: {label}...")
            t, _ = simulate(trade_bars, equity, compound=False)
            results[label] = _apply_daily_trade_cap(t)
    finally:
        config.ALLOW_SHORTS = original

    lines = []
    def out(line=""):
        print(line)
        lines.append(line)

    days = sorted(mkt_dir) or sorted({d for t in results.values() if not t.empty for d in t["date"]})
    out(f"## ORB backtest: long-only vs long + short")
    out()
    out(f"{len(days)} trading days ({days[0] if days else '?'} to {days[-1] if days else '?'}), "
        f"{trade_bars['symbol'].nunique()} symbols, fixed risk of {config.RISK_PCT_PER_TRADE:.1%} "
        f"of ${equity:,.0f} per trade, max {config.MAX_TRADES_PER_DAY} trades/day, "
        f"${config.MAX_NOTIONAL_PER_TRADE:,.0f} per-trade cap, {config.BACKTEST_SLIPPAGE_PCT:.2%} slippage per fill. "
        f"Opening range {config.OPENING_RANGE_MINUTES} min, volume x{config.VOLUME_CONFIRMATION_MULT}, "
        f"target {config.REWARD_RISK_MULTIPLE}R. No AI filter.")
    out()
    out("| | Trades | Win rate | Avg R | Total P&L | Return | Max drawdown | Worst day | Losing days |")
    out("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    rows = [(k, v) for k, v in results.items()]
    both = results["Long + short"]
    if not both.empty:
        rows += [("  of which longs", both[both["direction"] == "long"]),
                 ("  of which shorts", both[both["direction"] == "short"])]
    for label, t in rows:
        s = _stats(t, equity)
        out(f"| {label} | {s['trades']} | {s['win_rate']:.1%} | {s['avg_r']:+.2f}R | ${s['pnl']:,.0f} | "
            f"{s['pnl'] / equity:+.1%} | {s['max_dd']:.1%} | ${s['worst_day']:,.0f} | {s['losing_days']}/{s['days']} |")

    if mkt_dir:
        out()
        n_up = sum(1 for v in mkt_dir.values() if v == "up")
        out(f"### By {market_symbol} day direction ({n_up} up days, {len(mkt_dir) - n_up} down days)")
        out()
        out("| | P&L on up days | P&L on down days | Avg R up | Avg R down |")
        out("|---|---:|---:|---:|---:|")
        for label, t in rows:
            if t.empty:
                out(f"| {label} | $0 | $0 | – | – |")
                continue
            dirs = t["date"].map(mkt_dir)
            up, dn = t[dirs == "up"], t[dirs == "down"]
            fmt_r = lambda x: f"{x['r_multiple'].mean():+.2f}R" if len(x) else "–"
            out(f"| {label} | ${up['pnl'].sum():,.0f} | ${dn['pnl'].sum():,.0f} | {fmt_r(up)} | {fmt_r(dn)} |")

    lo, ls = _stats(results["Long only"], equity), _stats(both, equity)
    out()
    if ls["pnl"] > lo["pnl"] and ls["avg_r"] >= lo["avg_r"] - 0.02 and ls["max_dd"] <= lo["max_dd"] * 1.25 + 0.005:
        out("**Verdict:** adding shorts improved total P&L without materially worse expectancy or drawdown "
            "over this window. Worth paper-trading, and re-testing on another window first.")
    else:
        out("**Verdict:** adding shorts did not clearly improve results over this window "
            "(lower P&L, lower expectancy, or a noticeably deeper drawdown). Keep shorts off for now.")
    out()
    out("Caveats: backtests use IEX minute bars and bar-level fills; they ignore whether a stock could be "
        "borrowed, the concurrent-position cap, the daily notional cap and the daily-loss breaker. "
        "Treat the difference between the two rows as the signal, not the absolute numbers.")

    for label, t in results.items():
        if not t.empty:
            t.to_csv(f"backtest_{'long_only' if label == 'Long only' else 'long_short'}.csv", index=False)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=config.BACKTEST_DEFAULT_DAYS)
    parser.add_argument("--equity", type=float, default=25000.0)
    parser.add_argument("--symbols", type=str, default=",".join(config.WATCHLIST))
    parser.add_argument("--compare-shorts", action="store_true",
                        help="Run long-only and long+short on the same data and compare them.")
    args = parser.parse_args()

    symbols = args.symbols.split(",")
    client = get_client()
    if args.compare_shorts:
        fetch = symbols + (["QQQ"] if "QQQ" not in symbols else [])
        print(f"Fetching {args.days} days of minute bars for {len(fetch)} symbols...")
        bars = fetch_minute_bars(client, fetch, args.days)
        compare_shorts(bars, args.equity)
        return

    print(f"Fetching {args.days} days of minute bars for {symbols}...")
    bars = fetch_minute_bars(client, symbols, args.days)

    print("Running simulation...")
    trades, equity_curve = simulate(bars, args.equity)

    if not trades.empty:
        trades.to_csv("backtest_trades.csv", index=False)
        print(f"Wrote {len(trades)} trades to backtest_trades.csv")

    summarize(trades, args.equity)


if __name__ == "__main__":
    main()
