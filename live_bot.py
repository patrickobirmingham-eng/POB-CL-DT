"""
Live (paper) execution of the ORB strategy against Alpaca.

This refuses to run unless config.PAPER_TRADING is True and the API base URL
resolves to paper-api.alpaca.markets — it will not place live orders, full stop.

Designed to run either as a long-lived local process (default: runs 9:30-15:45 ET
in one go) OR as a short-lived job triggered on a schedule (e.g. GitHub Actions),
where a single run only covers part of the trading day and hands off state to the
next run via state.json. Use --session-end / --session-start (ET, "HH:MM") to bound
a single invocation, and --state-file to point at a persisted state file.

Usage:
    python live_bot.py
    python live_bot.py --session-start 09:30 --session-end 13:00 --state-file state.json
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time as time_module
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

import config
import strategy
import notify
import dashboard

load_dotenv()

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    raise SystemExit("Install dependencies first: pip install -r requirements.txt")

ET = ZoneInfo("America/New_York")
LOG_FILE = "trade_log.csv"


def log(msg: str):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_trade_row(row: dict):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def get_clients():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY in your .env file first.")
    if not config.PAPER_TRADING:
        raise SystemExit("config.PAPER_TRADING is False. This script only runs in paper mode. Aborting.")

    trading = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)
    return trading, data


def get_recent_bars(data_client, symbol, since):
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=since,
        feed=config.DATA_FEED,
    )
    df = data_client.get_stock_bars(req).df
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("US/Eastern")
    return df.set_index("timestamp").sort_index()


def submit_bracket_order(trading_client, symbol, direction, shares, stop_price, target_price):
    side = OrderSide.BUY if direction == "long" else OrderSide.SELL
    order = MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
    )
    return trading_client.submit_order(order)


def push_dashboard_update(trading_client, reason: str):
    """Regenerates docs/index.html from live account state and commits + pushes
    it immediately, so the dashboard reflects each trade as it happens rather
    than only at the end of the session. Best-effort: a failure here (network
    blip, transient push rejection) is logged but never stops the bot — trading
    logic and risk controls are unaffected either way.
    """
    try:
        dashboard.generate(trading_client)

        # Only in a git checkout (e.g. running under GitHub Actions) is there
        # anything to commit/push. A local ad-hoc run has no repo to push to.
        if not os.path.isdir(".git"):
            return

        subprocess.run(["git", "config", "user.name", "orb-trading-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "docs/index.html", "docs/.nojekyll"], check=False)

        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return  # nothing changed, nothing to commit

        subprocess.run(["git", "commit", "-m", f"Live dashboard update: {reason} [skip ci]"], check=False)

        for attempt in range(1, 6):
            push = subprocess.run(["git", "push"])
            if push.returncode == 0:
                log(f"Dashboard pushed live ({reason}).")
                return
            log(f"Dashboard push rejected (attempt {attempt}) — pulling latest and retrying...")
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        log("Dashboard push still failing after 5 attempts — will retry after the next trade/flatten.")
    except Exception as e:
        log(f"Live dashboard update failed (non-fatal): {e}")


def flatten_all(trading_client, reason: str = "Flatten time reached"):
    log(f"{reason} — closing all open positions.")
    try:
        trading_client.close_all_positions(cancel_orders=True)
    except Exception as e:
        log(f"close_all_positions() raised an error: {e}")
        # Fall through to the verify/retry loop below rather than trusting this
        # exception to mean "nothing closed" — some failures are per-symbol and
        # don't stop others from having gone through.

    # close_all_positions() can report success (or raise nothing) while still
    # leaving positions open — this happened for real on 2026-09-21, where the
    # bracket orders' stop/target legs got canceled but the actual closing sell
    # orders were never placed, silently carrying ~$3M of notional overnight.
    # Don't trust the call succeeded — verify against get_all_positions() and,
    # for anything still open, submit an explicit closing market order per
    # position directly. Retry a few times since a just-submitted close order
    # takes a moment to fill.
    still_open = []
    for attempt in range(1, 6):
        time_module.sleep(2)
        still_open = trading_client.get_all_positions()
        if not still_open:
            break
        for p in still_open:
            qty = abs(float(p.qty))
            side = OrderSide.SELL if float(p.qty) > 0 else OrderSide.BUY
            try:
                log(f"Flatten verify (attempt {attempt}): {p.symbol} still open "
                    f"({p.qty} shares) — submitting explicit closing {side.value} order.")
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                ))
            except Exception as e:
                log(f"Explicit close order for {p.symbol} failed: {e}")

    if still_open:
        symbols = ", ".join(p.symbol for p in still_open)
        log(f"WARNING: still holding positions after flatten attempts: {symbols}")
        notify.send(
            "ORB Bot: FLATTEN INCOMPLETE",
            f"{reason}. Could not confirm these positions closed: {symbols}. "
            f"Check the account directly.",
            priority="high",
            tags="rotating_light",
        )
    else:
        notify.send(
            "ORB Bot: Flattened",
            f"{reason}. All open positions confirmed closed.",
            tags="stop_sign",
        )

    push_dashboard_update(trading_client, reason="flatten")


def wait_for_market_open(trading_client, bounded: bool):
    clock = trading_client.get_clock()
    if clock.is_open:
        return True
    if bounded:
        # In a scheduled/bounded run (e.g. GitHub Actions) we never sit and wait —
        # that burns the job's runtime budget. If the market's closed (holiday,
        # weekend misfire) just log it and let main() exit cleanly.
        log(f"Market is closed (holiday or off-hours). Next open: {clock.next_open}. Exiting this run.")
        return False
    log(f"Market closed. Next open: {clock.next_open}. Sleeping...")
    while True:
        time_module.sleep(30)
        clock = trading_client.get_clock()
        if clock.is_open:
            log("Market is open.")
            return True


def _parse_hhmm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def load_state(path: str):
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    if raw.get("date") != today_str:
        # stale state from a previous day — ignore it, start fresh
        return None

    opening_ranges = {}
    for sym, d in raw.get("opening_ranges", {}).items():
        opening_ranges[sym] = strategy.OpeningRange(
            symbol=d["symbol"], date=d["date"], high=d["high"],
            low=d["low"], avg_volume=d["avg_volume"],
        )

    return {
        "date": raw["date"],
        "opening_ranges": opening_ranges,
        "traded_today": set(raw.get("traded_today", [])),
        "trade_count": raw.get("trade_count", 0),
        "starting_equity": raw.get("starting_equity"),
        "flattened": raw.get("flattened", False),
        "notional_deployed_today": raw.get("notional_deployed_today", 0.0),
    }


def save_state(path: str, day_state: dict):
    if not path:
        return
    serializable = {
        "date": day_state["date"],
        "opening_ranges": {
            sym: {"symbol": orange.symbol, "date": orange.date,
                  "high": orange.high, "low": orange.low, "avg_volume": orange.avg_volume}
            for sym, orange in day_state["opening_ranges"].items()
        },
        "traded_today": sorted(day_state["traded_today"]),
        "trade_count": day_state["trade_count"],
        "starting_equity": day_state["starting_equity"],
        "flattened": day_state["flattened"],
        "notional_deployed_today": day_state["notional_deployed_today"],
    }
    with open(path, "w") as f:
        json.dump(serializable, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-start", type=str, default=None,
                         help="ET HH:MM — don't do anything before this time in this run")
    parser.add_argument("--session-end", type=str, default=None,
                         help="ET HH:MM — exit this run at/after this time (state is saved so "
                              "the next scheduled run can pick up where this one left off)")
    parser.add_argument("--state-file", type=str, default=None,
                         help="Path to a JSON file used to persist day_state across runs. "
                              "Required for split/scheduled (e.g. GitHub Actions) operation.")
    args = parser.parse_args()

    bounded = args.session_end is not None
    session_start_t = _parse_hhmm(args.session_start) if args.session_start else None
    session_end_t = _parse_hhmm(args.session_end) if args.session_end else None

    trading_client, data_client = get_clients()
    account = trading_client.get_account()
    log(f"Connected to Alpaca PAPER account. Equity: ${float(account.equity):,.2f}")

    restored = load_state(args.state_file) if args.state_file else None
    if restored:
        log(f"Restored state from {args.state_file}: "
            f"{len(restored['traded_today'])} symbols traded, {restored['trade_count']} trades so far today.")
        day_state = restored
    else:
        day_state = {
            "date": None,
            "opening_ranges": {},   # symbol -> OpeningRange
            "traded_today": set(),
            "trade_count": 0,
            "starting_equity": None,
            "flattened": False,
            "notional_deployed_today": 0.0,
        }

    flatten_t = strategy.flatten_time()

    while True:
        if session_start_t is not None:
            now_check = datetime.now(ET)
            if now_check.time() < session_start_t:
                log(f"Before session start ({args.session_start} ET). Nothing to do yet, exiting run.")
                save_state(args.state_file, day_state)
                return

        if not wait_for_market_open(trading_client, bounded):
            save_state(args.state_file, day_state)
            return

        now = datetime.now(ET)
        today_str = now.strftime("%Y-%m-%d")

        if day_state["date"] != today_str:
            log(f"New trading day: {today_str}. Resetting state.")
            day_state.update({
                "date": today_str, "opening_ranges": {}, "traded_today": set(),
                "trade_count": 0, "starting_equity": float(trading_client.get_account().equity),
                "flattened": False, "notional_deployed_today": 0.0,
            })

        now_t = now.time()

        if session_end_t is not None and now_t >= session_end_t:
            log(f"Session end reached ({args.session_end} ET). Saving state and exiting this run.")
            save_state(args.state_file, day_state)
            return
        market_open_t = dtime(9, 30)
        or_complete_t = (
            datetime.combine(now.date(), market_open_t)
            + pd.Timedelta(minutes=config.OPENING_RANGE_MINUTES)
        ).time()

        account = trading_client.get_account()
        equity = float(account.equity)
        daily_pnl_pct = (equity - day_state["starting_equity"]) / day_state["starting_equity"]

        # Circuit breakers
        if daily_pnl_pct <= -config.MAX_DAILY_LOSS_PCT:
            if not day_state["flattened"]:
                flatten_all(trading_client, reason=f"Daily loss circuit breaker hit ({daily_pnl_pct:.1%})")
                day_state["flattened"] = True
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        if now_t >= flatten_t and not day_state["flattened"]:
            flatten_all(trading_client)
            day_state["flattened"] = True

        if now_t >= flatten_t:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        if now_t < or_complete_t:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        if day_state["trade_count"] >= config.MAX_TRADES_PER_DAY:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        open_positions = {p.symbol for p in trading_client.get_all_positions()}

        # A position can close on its own between polls — Alpaca fills the bracket's
        # stop-loss or take-profit leg server-side, with no action from this script.
        # Detect that by diffing against what we saw open last pass, and push a
        # dashboard update so a stop/target hit shows up promptly too, not just entries.
        previously_open = day_state.get("_last_seen_open_positions")
        if previously_open is not None and previously_open != open_positions:
            closed = previously_open - open_positions
            if closed:
                push_dashboard_update(trading_client, reason=f"position closed: {', '.join(sorted(closed))}")
        day_state["_last_seen_open_positions"] = open_positions

        if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        for symbol in config.WATCHLIST:
            if symbol in open_positions:
                continue
            if symbol in day_state["traded_today"] and config.ONE_TRADE_PER_SYMBOL_PER_DAY:
                continue

            since = datetime.combine(now.date(), market_open_t, tzinfo=ET)
            bars = get_recent_bars(data_client, symbol, since)
            if bars.empty:
                continue

            if symbol not in day_state["opening_ranges"]:
                orange = strategy.compute_opening_range(bars, symbol, today_str)
                if orange is None:
                    continue
                day_state["opening_ranges"][symbol] = orange
                log(f"{symbol}: opening range set. High={orange.high:.2f} Low={orange.low:.2f}")

            orange = day_state["opening_ranges"][symbol]
            last_bar = bars.iloc[-1]
            ts = bars.index[-1]

            sig = strategy.check_breakout(orange, last_bar, ts, symbol in day_state["traded_today"])
            if sig is None:
                continue

            shares = strategy.position_size(equity, sig.risk_per_share)
            if shares <= 0:
                continue

            # Hard daily notional cap — trims (or skips) the risk-sized position so
            # cumulative capital deployed today never exceeds MAX_DAILY_NOTIONAL_TRADED,
            # independent of what the risk-per-trade math alone would size it at.
            remaining_budget = config.MAX_DAILY_NOTIONAL_TRADED - day_state["notional_deployed_today"]
            if remaining_budget <= 0:
                log(f"Daily notional cap (${config.MAX_DAILY_NOTIONAL_TRADED:,.0f}) reached — "
                    f"skipping {symbol} breakout.")
                continue
            max_shares_by_budget = int(remaining_budget // sig.entry_price)
            if max_shares_by_budget < shares:
                log(f"{symbol}: trimming size from {shares} to {max_shares_by_budget} shares "
                    f"to stay within daily notional cap (${remaining_budget:,.0f} remaining).")
                shares = max_shares_by_budget
            if shares <= 0:
                continue

            log(f"BREAKOUT {symbol} {sig.direction} @ {sig.entry_price:.2f} "
                f"stop={sig.stop_price:.2f} target={sig.target_price:.2f} shares={shares}")

            try:
                order = submit_bracket_order(
                    trading_client, symbol, sig.direction, shares, sig.stop_price, sig.target_price
                )
                day_state["traded_today"].add(symbol)
                day_state["trade_count"] += 1
                day_state["notional_deployed_today"] += shares * sig.entry_price
                log_trade_row({
                    "timestamp": ts, "symbol": symbol, "direction": sig.direction,
                    "entry_price": sig.entry_price, "stop": sig.stop_price,
                    "target": sig.target_price, "shares": shares, "order_id": order.id,
                })
                notify.send(
                    f"ORB Bot: Entered {symbol} {sig.direction.upper()}",
                    f"{shares} shares @ ${sig.entry_price:.2f}\n"
                    f"Stop: ${sig.stop_price:.2f}  Target: ${sig.target_price:.2f}",
                    tags="chart_with_upwards_trend" if sig.direction == "long" else "chart_with_downwards_trend",
                )
                push_dashboard_update(trading_client, reason=f"entered {symbol}")
            except Exception as e:
                log(f"Order submission failed for {symbol}: {e}")
                notify.send(
                    f"ORB Bot: Order failed ({symbol})",
                    str(e),
                    priority="high",
                    tags="warning",
                )

        # Persist state after every pass so a killed/crashed job (or a GitHub Actions
        # job hitting its time limit) doesn't lose today's progress.
        save_state(args.state_file, day_state)

        time_module.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user. Exiting. NOTE: open positions are NOT auto-closed on exit — "
            "check your Alpaca paper account. Bracket orders' stops/targets remain active on "
            "Alpaca's side even after this script stops.")
        sys.exit(0)
