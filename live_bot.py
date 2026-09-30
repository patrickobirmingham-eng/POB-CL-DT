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
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

import config
import strategy
import notify
import dashboard
import ai_filter
import momentum_strategy

load_dotenv()

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, TakeProfitRequest, StopLossRequest,
        GetOrdersRequest, ReplaceOrderRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus, OrderType
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


def get_recent_bars_bulk(data_client, symbols, since):
    """Fetches minute bars for many symbols in a single API call instead of one
    call per symbol. Needed once WATCHLIST is large (e.g. the Nasdaq-100) — at
    that size, looping get_recent_bars() per symbol on every poll would mean
    ~100 separate requests every POLL_INTERVAL_SECONDS, which is both slow and
    likely to hit Alpaca's data API rate limits. Returns {symbol: DataFrame}.
    """
    if not symbols:
        return {}
    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Minute,
        start=since,
        feed=config.DATA_FEED,
    )
    df = data_client.get_stock_bars(req).df
    if df.empty:
        return {}
    df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("US/Eastern")
    return {
        sym: group.set_index("timestamp").sort_index()
        for sym, group in df.groupby("symbol")
    }


_daily_cache = {}  # (date, symbol) -> dict of prior-day stats
_shortable_cache = {}  # (date, symbol) -> bool


def is_shortable(trading_client, symbol, today):
    """True only if Alpaca reports the stock as shortable AND easy to borrow.
    Checked once per symbol per day. If the lookup fails, answer False: better
    to skip a short than to send an order that will be rejected."""
    key = (today, symbol)
    if key not in _shortable_cache:
        try:
            asset = trading_client.get_asset(symbol)
            _shortable_cache[key] = bool(getattr(asset, "shortable", False)) and bool(getattr(asset, "easy_to_borrow", False))
        except Exception as e:
            log(f"{symbol}: could not check shortability ({e}); skipping short.")
            _shortable_cache[key] = False
        if not _shortable_cache[key]:
            log(f"{symbol}: not shortable / not easy to borrow today — short breakdowns skipped.")
    return _shortable_cache[key]


def get_prior_day_stats(data_client, symbols, today):
    """Prior completed session's high/low/close/volume per symbol, fetched once
    per symbol per day (daily bars) and cached. Used only to give the AI filter
    context on nearby levels; failures just mean less context."""
    missing = [s for s in symbols if (today, s) not in _daily_cache]
    if missing:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=missing,
                timeframe=TimeFrame.Day,
                start=datetime.combine(today - timedelta(days=10), dtime(0, 0), tzinfo=ET),
                end=datetime.combine(today, dtime(0, 0), tzinfo=ET),
                feed=config.DATA_FEED,
            )
            df = data_client.get_stock_bars(req).df
            if not df.empty:
                df = df.reset_index()
                for sym, g in df.groupby("symbol"):
                    last = g.sort_values("timestamp").iloc[-1]
                    _daily_cache[(today, sym)] = {
                        "prior_day_high": float(last["high"]), "prior_day_low": float(last["low"]),
                        "prior_day_close": float(last["close"]), "prior_day_volume": float(last["volume"]),
                    }
        except Exception as e:
            log(f"AI filter: prior-day stats unavailable ({e})")
        for sym in missing:
            _daily_cache.setdefault((today, sym), {})
    return {s: _daily_cache.get((today, s), {}) for s in symbols}


def build_ai_context(data_client, sig, orange, bars, now):
    """Everything the AI filter sees about one breakout signal."""
    r2 = lambda v: round(float(v), 4)
    entry = float(sig.entry_price)
    last = bars.iloc[-1]
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap = float((typical * bars["volume"]).sum() / bars["volume"].sum()) if bars["volume"].sum() else None
    minutes_open = max(1, int((now - datetime.combine(now.date(), dtime(9, 30), tzinfo=ET)).total_seconds() // 60))
    cum_vol = float(bars["volume"].sum())
    ctx = {
        "symbol": sig.symbol,
        "direction": sig.direction,
        "time_et": now.strftime("%H:%M"),
        "minutes_since_open": minutes_open,
        "minutes_until_forced_exit": int((datetime.combine(now.date(), strategy.flatten_time(), tzinfo=ET) - now).total_seconds() // 60),
        "entry_price": r2(entry),
        "stop_price": r2(sig.stop_price),
        "strategy_target_price": r2(sig.target_price),
        "risk_per_share": r2(sig.risk_per_share),
        "stop_distance_pct": round(sig.risk_per_share / entry * 100, 3) if entry else None,
        "max_allowed_stop_pct": round(float(config.AI_MAX_STOP_PCT) * 100, 2),
        "default_take_profit_r": float(config.REWARD_RISK_MULTIPLE),
        "opening_range_minutes": int(config.OPENING_RANGE_MINUTES),
        "opening_range_high": r2(orange.high),
        "opening_range_low": r2(orange.low),
        "opening_range_pct_of_price": round(orange.range_size / entry * 100, 3) if entry else None,
        "opening_range_avg_bar_volume": round(float(orange.avg_volume), 1),
        "breakout_bar": {"open": r2(last["open"]), "high": r2(last["high"]), "low": r2(last["low"]),
                         "close": r2(last["close"]), "volume": float(last["volume"])},
        "breakout_bar_relative_volume": round(float(last["volume"]) / orange.avg_volume, 2) if orange.avg_volume else None,
        "required_relative_volume": float(config.VOLUME_CONFIRMATION_MULT),
        "session_open": r2(bars["open"].iloc[0]),
        "session_high_so_far": r2(bars["high"].max()),
        "session_low_so_far": r2(bars["low"].min()),
        "vwap": r2(vwap) if vwap else None,
        "entry_vs_vwap_pct": round((entry - vwap) / vwap * 100, 3) if vwap else None,
        "cumulative_volume_today": cum_vol,
        "last_10_bars_close": [r2(x) for x in bars["close"].iloc[-10:].tolist()],
        "last_10_bars_volume": [float(x) for x in bars["volume"].iloc[-10:].tolist()],
        "volume_feed_note": f"{config.DATA_FEED} feed; volumes are partial, compare ratios",
    }
    stats = get_prior_day_stats(data_client, [sig.symbol, "QQQ"], now.date())
    pd_ = stats.get(sig.symbol) or {}
    if pd_:
        ctx.update({k: r2(v) for k, v in pd_.items()})
        pdc = pd_.get("prior_day_close")
        if pdc:
            ctx["gap_pct"] = round((ctx["session_open"] - pdc) / pdc * 100, 3)
            ctx["change_today_pct"] = round((entry - pdc) / pdc * 100, 3)
        if pd_.get("prior_day_volume"):
            ctx["cumulative_volume_vs_prior_day_pct"] = round(cum_vol / pd_["prior_day_volume"] * 100, 1)
            ctx["session_elapsed_pct"] = round(minutes_open / 390 * 100, 1)
    try:
        qqq = get_recent_bars(data_client, "QQQ", datetime.combine(now.date(), dtime(9, 30), tzinfo=ET))
        if not qqq.empty:
            q_last = float(qqq["close"].iloc[-1])
            ctx["qqq_change_since_open_pct"] = round((q_last - float(qqq["open"].iloc[0])) / float(qqq["open"].iloc[0]) * 100, 3)
            qpc = (stats.get("QQQ") or {}).get("prior_day_close")
            if qpc:
                ctx["qqq_change_today_pct"] = round((q_last - qpc) / qpc * 100, 3)
    except Exception as e:
        log(f"AI filter: QQQ context unavailable ({e})")
    return ctx


# ---------------------------------------------------------------------------
# Intraday momentum on QQQ (see momentum_strategy.py for the rules)
# ---------------------------------------------------------------------------
_momentum_day = {}  # per-day setup: date, sigma, prev_close, vol, open, shares


def momentum_setup(data_client, equity, now):
    """Once per day, after 09:46 ET: 14-session noise profile and daily vol from
    full-market (SIP) history, and today's official open from the SIP 09:30 bar
    (the free data plan serves SIP data older than 15 minutes). Returns the
    setup dict, or None if it can't be built yet."""
    today = now.date()
    if _momentum_day.get("date") == today:
        return _momentum_day
    sym = config.MOMENTUM_SYMBOL
    try:
        hist = StockBarsRequest(
            symbol_or_symbols=[sym], timeframe=TimeFrame.Minute, feed="sip",
            start=datetime.combine(today - timedelta(days=35), dtime(9, 30), tzinfo=ET),
            end=datetime.combine(today, dtime(0, 0), tzinfo=ET),
        )
        df = data_client.get_stock_bars(hist).df
        if df.empty:
            return None
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET)
        df = df.set_index("timestamp").sort_index().between_time("09:30", "15:59")
        days = [momentum_strategy.prepare_day(g)[0] for _, g in df.groupby(df.index.date) if len(g) >= 300]
        if len(days) < momentum_strategy.LOOKBACK + 1:
            log(f"Momentum: only {len(days)} prior sessions of {sym} history; need {momentum_strategy.LOOKBACK + 1}.")
            return None
        closes = [float(d["close"].iloc[-1]) for d in days]
        open_req = StockBarsRequest(
            symbol_or_symbols=[sym], timeframe=TimeFrame.Minute, feed="sip",
            start=datetime.combine(today, dtime(9, 30), tzinfo=ET),
            end=datetime.combine(today, dtime(9, 31), tzinfo=ET),
        )
        o = data_client.get_stock_bars(open_req).df
        if o.empty:
            return None
        day_open = float(o["open"].iloc[0])
    except Exception as e:
        log(f"Momentum setup failed (will retry): {e}")
        return None
    vol = momentum_strategy.daily_vol(closes)
    shares = momentum_strategy.position_size(equity, day_open, vol, float(config.MOMENTUM_TARGET_VOL),
                                             float(config.MOMENTUM_MAX_LEVERAGE))
    _momentum_day.clear()
    _momentum_day.update({
        "date": today, "sigma": momentum_strategy.sigma_profile(days), "prev_close": closes[-1],
        "vol": vol, "open": day_open, "shares": shares,
    })
    log(f"Momentum setup {sym}: open {day_open:.2f}, prev close {closes[-1]:.2f}, "
        f"daily vol {vol:.2%}, size {shares} shares.")
    return _momentum_day


def run_momentum(trading_client, data_client, day_state, positions, now, equity):
    """Acts once per 30-minute check slot (10:00-15:30 ET) within 10 minutes of
    it. An exit and a reversal in the same slot are split across two polls so the
    closing order fills before the opposite one is sent."""
    if not getattr(config, "MOMENTUM_ENABLED", False):
        return
    hhmm = now.strftime("%H:%M")
    slots = [t for t in momentum_strategy.CHECK_TIMES if t <= hhmm]
    if not slots:
        return
    slot = slots[-1]
    slot_dt = datetime.combine(now.date(), dtime(int(slot[:2]), int(slot[3:])), tzinfo=ET)
    if now - slot_dt > timedelta(minutes=10):
        return
    done = day_state.setdefault("momentum_done", set())
    if slot in done:
        return
    setup = momentum_setup(data_client, equity, now)
    if setup is None or setup["shares"] <= 0:
        return

    sym = config.MOMENTUM_SYMBOL
    bar_label = momentum_strategy.CHECK_BARS[slot]
    try:
        today_bars = get_recent_bars(data_client, sym, datetime.combine(now.date(), dtime(9, 30), tzinfo=ET))
    except Exception as e:
        log(f"Momentum: could not fetch {sym} bars ({e}); retrying next poll.")
        return
    if today_bars.empty or today_bars.index[-1].strftime("%H:%M") < bar_label:
        return  # the bar that closes at the check time isn't in yet
    bars, _ = momentum_strategy.prepare_day(today_bars)
    price, vwap = float(bars.at[bar_label, "close"]), float(bars.at[bar_label, "vwap"])
    sigma = float(setup["sigma"][bar_label])
    upper, lower = momentum_strategy.bands(setup["open"], setup["prev_close"], sigma)

    held = next((p for p in positions if p.symbol == sym), None)
    qty = float(held.qty) if held is not None else 0.0
    position = 1 if qty > 0 else (-1 if qty < 0 else 0)
    exit_now, new_side = momentum_strategy.decide(position, price, upper, lower, vwap,
                                                  bool(config.MOMENTUM_ALLOW_SHORTS))
    note = f"price {price:.2f}, band {lower:.2f}-{upper:.2f}, VWAP {vwap:.2f}"
    try:
        if exit_now:
            trading_client.close_position(sym)
            log(f"Momentum {slot}: exit {sym} {'long' if position > 0 else 'short'} ({note}).")
            notify.send(f"ORB Bot: Momentum exit {sym}", f"{slot} ET — {note}", tags="door")
            push_dashboard_update(trading_client, reason=f"momentum exit {sym}")
            if new_side:
                return  # re-evaluated next poll once the close has filled
        if new_side:
            if new_side < 0 and not is_shortable(trading_client, sym, now.date()):
                done.add(slot)
                return
            side = OrderSide.BUY if new_side > 0 else OrderSide.SELL
            trading_client.submit_order(MarketOrderRequest(
                symbol=sym, qty=setup["shares"], side=side, time_in_force=TimeInForce.DAY))
            label = "LONG" if new_side > 0 else "SHORT"
            log(f"Momentum {slot}: enter {sym} {label} {setup['shares']} shares ({note}).")
            notify.send(f"ORB Bot: Momentum {label} {sym}", f"{setup['shares']} shares at ~${price:.2f}\n{slot} ET — {note}",
                        tags="chart_with_upwards_trend" if new_side > 0 else "chart_with_downwards_trend")
            push_dashboard_update(trading_client, reason=f"momentum {label.lower()} {sym}")
        done.add(slot)
    except Exception as e:
        log(f"Momentum order failed at {slot}: {e}")
        notify.send(f"ORB Bot: Momentum order failed ({sym})", str(e), priority="high", tags="warning")
        done.add(slot)


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


def apply_breakeven_stops(trading_client, day_state, positions):
    """Once an open position has moved config.BREAKEVEN_TRIGGER_R multiples of
    its initial risk (entry-to-stop distance) in our favor, move that
    position's stop-loss order up to breakeven (the entry price).

    This never touches the take-profit leg and never caps the upside — the
    trade is still free to run all the way to its original target. All this
    does is guarantee a winner that gives back its gains exits flat instead
    of round-tripping into a full stop-loss loss. Best-effort: any failure
    here is logged and never interrupts trading, and each symbol is only
    moved to breakeven once per day (tracked via day_state["breakeven_moved"]).
    """
    if not config.BREAKEVEN_TRIGGER_R:
        return

    meta = day_state.setdefault("trade_meta", {})
    moved = day_state.setdefault("breakeven_moved", set())

    for p in positions:
        symbol = p.symbol
        if symbol in moved or symbol not in meta:
            continue

        info = meta[symbol]
        entry = info["entry_price"]
        stop = info["stop_price"]
        direction = info["direction"]
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0 or p.current_price is None:
            continue

        current_price = float(p.current_price)
        gain_per_share = (current_price - entry) if direction == "long" else (entry - current_price)
        if gain_per_share < config.BREAKEVEN_TRIGGER_R * risk_per_share:
            continue

        try:
            open_orders = trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            )
            stop_leg = next((o for o in open_orders if o.order_type == OrderType.STOP), None)
            if stop_leg is None:
                log(f"{symbol}: reached {config.BREAKEVEN_TRIGGER_R:.1f}R in profit but no open "
                    f"stop-loss order was found — skipping breakeven move this pass.")
                continue

            new_stop = round(entry, 2)
            trading_client.replace_order_by_id(
                order_id=stop_leg.id,
                order_data=ReplaceOrderRequest(stop_price=new_stop),
            )
            moved.add(symbol)
            log(f"{symbol}: reached {config.BREAKEVEN_TRIGGER_R:.1f}R in profit — moved stop-loss "
                f"to breakeven (${new_stop:.2f}). Target unchanged, trade still free to run.")
            notify.send(
                f"ORB Bot: {symbol} stop moved to breakeven",
                f"Price reached {config.BREAKEVEN_TRIGGER_R:.1f}x initial risk in profit; "
                f"stop-loss moved to entry (${new_stop:.2f}). Take-profit target is unchanged.",
                tags="lock",
            )
        except Exception as e:
            log(f"{symbol}: failed to move stop to breakeven: {e}")


def apply_closing_tighten(trading_client, day_state, positions, now):
    """Starting config.TIGHTEN_BEFORE_CLOSE_MINUTES before the flatten time,
    progressively pull each open position's take-profit limit down toward the
    live price instead of leaving it sitting at its original target.

    Without this, a position that popped nicely earlier in the day but has
    since faded back toward (or through) breakeven just rides that fade all
    the way to the 15:45 flatten — giving back a gain that could have been
    locked in, or riding a small loss deeper, for no reason other than the
    original target was never going to be hit today. This linearly
    interpolates the limit from the original target to the live price over
    the tighten window, so by the moment flatten fires the limit is
    effectively "sell here" rather than "sell at a target that's no longer
    realistic". It ratchets toward the live price only (never loosens back
    out) and never touches the stop-loss leg — apply_breakeven_stops() above
    already covers downside protection. Best-effort and throttled to at most
    once every config.TIGHTEN_STEP_SECONDS per symbol, so a killed/late poll
    or a transient API error here never interrupts trading.
    """
    if not config.TIGHTEN_BEFORE_CLOSE_MINUTES:
        return

    flatten_t = strategy.flatten_time()
    flatten_dt = datetime.combine(now.date(), flatten_t, tzinfo=ET)
    window_start_dt = flatten_dt - pd.Timedelta(minutes=config.TIGHTEN_BEFORE_CLOSE_MINUTES)
    if now < window_start_dt or now >= flatten_dt:
        return

    total_window = (flatten_dt - window_start_dt).total_seconds()
    remaining = (flatten_dt - now).total_seconds()
    fraction_remaining = max(0.0, min(1.0, remaining / total_window))

    meta = day_state.setdefault("trade_meta", {})
    last_tightened = day_state.setdefault("last_tightened_at", {})

    for p in positions:
        symbol = p.symbol
        info = meta.get(symbol)
        if info is None or info.get("target_price") is None or p.current_price is None:
            continue

        last_ts = last_tightened.get(symbol)
        if last_ts is not None:
            elapsed = (now - datetime.fromisoformat(last_ts)).total_seconds()
            if elapsed < config.TIGHTEN_STEP_SECONDS:
                continue

        current_price = float(p.current_price)
        original_target = info["target_price"]
        direction = info["direction"]
        new_limit = round(current_price + (original_target - current_price) * fraction_remaining, 2)

        try:
            open_orders = trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            )
            limit_leg = next((o for o in open_orders if o.order_type == OrderType.LIMIT), None)
            if limit_leg is None:
                continue

            current_limit = float(limit_leg.limit_price) if limit_leg.limit_price else None

            # Only ratchet toward the live price -- never loosen the limit
            # back out toward the original target if price bounces around.
            if current_limit is not None:
                if direction == "long" and new_limit >= current_limit:
                    last_tightened[symbol] = now.isoformat()
                    continue
                if direction == "short" and new_limit <= current_limit:
                    last_tightened[symbol] = now.isoformat()
                    continue
                if abs(current_limit - new_limit) < 0.01:
                    last_tightened[symbol] = now.isoformat()
                    continue

            trading_client.replace_order_by_id(
                order_id=limit_leg.id,
                order_data=ReplaceOrderRequest(limit_price=new_limit),
            )
            last_tightened[symbol] = now.isoformat()
            log(f"{symbol}: tightened take-profit limit to ${new_limit:.2f} "
                f"({fraction_remaining:.0%} of the {config.TIGHTEN_BEFORE_CLOSE_MINUTES}-min "
                f"closing window remaining).")
        except Exception as e:
            log(f"{symbol}: failed to tighten take-profit limit: {e}")


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
        if os.path.exists(ai_filter.DECISIONS_FILE):
            subprocess.run(["git", "add", ai_filter.DECISIONS_FILE], check=False)

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
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=False)
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
        "trade_meta": raw.get("trade_meta", {}),
        "breakeven_moved": set(raw.get("breakeven_moved", [])),
        "last_tightened_at": raw.get("last_tightened_at", {}),
        "ai_vetoed": set(raw.get("ai_vetoed", [])),
        "momentum_done": set(raw.get("momentum_done", [])),
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
        "trade_meta": day_state.get("trade_meta", {}),
        "breakeven_moved": sorted(day_state.get("breakeven_moved", set())),
        "last_tightened_at": day_state.get("last_tightened_at", {}),
        "ai_vetoed": sorted(day_state.get("ai_vetoed", set())),
        "momentum_done": sorted(day_state.get("momentum_done", set())),
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
            "trade_meta": {},       # symbol -> {direction, entry_price, stop_price, target_price}
            "breakeven_moved": set(),
            "last_tightened_at": {},  # symbol -> ISO timestamp of last take-profit tighten
            "ai_vetoed": set(),       # symbols the AI filter vetoed today (not re-asked)
            "momentum_done": set(),   # momentum check slots already acted on today
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
                "trade_meta": {}, "breakeven_moved": set(), "last_tightened_at": {},
                "ai_vetoed": set(), "momentum_done": set(),
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

        # Detect closed positions and refresh the dashboard FIRST, before any of
        # the early `continue`s below (daily-loss breaker, flatten time, trade
        # cap, etc.). A position can close on its own between polls — Alpaca
        # fills the bracket's stop-loss or take-profit leg server-side, with no
        # action from this script. Detect that by diffing against what we saw
        # open last pass, and push a dashboard update so the Daily P&L chart,
        # Income by Day and Closed Orders reflect it promptly. This used to sit
        # further down, so once the trade cap was hit (or after the daily-loss
        # breaker) closes were never noticed and the dashboard went stale until
        # the end-of-session rebuild.
        positions_list = trading_client.get_all_positions()
        open_now = {p.symbol for p in positions_list}
        previously_open = day_state.get("_last_seen_open_positions")
        if previously_open is not None and previously_open != open_now:
            closed = previously_open - open_now
            if closed:
                push_dashboard_update(trading_client, reason=f"position closed: {', '.join(sorted(closed))}")
        day_state["_last_seen_open_positions"] = open_now

        # Manage existing positions on EVERY poll, regardless of whether we're
        # free to open new trades. These used to sit below the trade-cap
        # `continue`, so once MAX_TRADES_PER_DAY was hit, winners never got
        # their breakeven stop and the closing-time take-profit tightening never
        # ran — the opposite of what they're for. Skipped only when the
        # daily-loss breaker has tripped or it's flatten time (positions are
        # being closed anyway, so don't fight that by replacing orders).
        if daily_pnl_pct > -config.MAX_DAILY_LOSS_PCT and now_t < flatten_t:
            # Breakeven: move a winner's stop to entry once it is up
            # BREAKEVEN_TRIGGER_R x initial risk. Never touches the target.
            apply_breakeven_stops(trading_client, day_state, positions_list)
            # Closing window before flatten_t: progressively pull each open
            # position's take-profit limit toward the live price so a fade
            # isn't ridden all the way to the forced close.
            apply_closing_tighten(trading_client, day_state, positions_list, now)
            # Second strategy: intraday momentum on QQQ, every 30 min 10:00-15:30.
            run_momentum(trading_client, data_client, day_state, positions_list, now, equity)

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

        # ORB entries can be switched off (ORB_ENABLED) without affecting the
        # momentum strategy, position management or the end-of-day flatten above.
        if not getattr(config, "ORB_ENABLED", True):
            save_state(args.state_file, day_state)
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        if now_t < or_complete_t:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        if day_state["trade_count"] >= config.MAX_TRADES_PER_DAY:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        open_positions = {p.symbol for p in positions_list}
        # The momentum strategy's position doesn't use one of ORB's slots.
        orb_open = open_positions - {getattr(config, "MOMENTUM_SYMBOL", "")}

        if len(orb_open) >= config.MAX_CONCURRENT_POSITIONS:
            time_module.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        # Fetch bars for every symbol we might act on this pass in ONE batched
        # API call rather than one call per symbol — essential once WATCHLIST
        # is large (e.g. the Nasdaq-100); see get_recent_bars_bulk().
        candidates = [
            symbol for symbol in config.WATCHLIST
            if symbol != getattr(config, "MOMENTUM_SYMBOL", "")
            if symbol not in open_positions
            and not (symbol in day_state["traded_today"] and config.ONE_TRADE_PER_SYMBOL_PER_DAY)
            # A veto stands for the rest of the day. Without this, the same
            # breakout would be re-sent to the AI on every 15 s poll.
            and symbol not in day_state.setdefault("ai_vetoed", set())
        ]
        since = datetime.combine(now.date(), market_open_t, tzinfo=ET)
        bars_by_symbol = get_recent_bars_bulk(data_client, candidates, since)

        for symbol in candidates:
            bars = bars_by_symbol.get(symbol)
            if bars is None or bars.empty:
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

            # Shorts need borrowable shares; skip quietly rather than send an
            # order Alpaca will reject (and alert on every poll).
            if sig.direction == "short" and not is_shortable(trading_client, symbol, now.date()):
                continue

            shares = strategy.position_size(equity, sig.risk_per_share)
            if shares <= 0:
                continue

            # --- AI filter: Claude confirms or vetoes the signal before any
            # order is sent. It can skip the trade, shrink it, or move the
            # target within 1R-3R; it can never widen the stop or add size.
            ai_mode = ai_filter.mode()
            ai_note = ""
            if ai_mode != "off":
                ctx = build_ai_context(data_client, sig, orange, bars, datetime.now(ET))
                decision = ai_filter.evaluate(ctx)
                log(f"AI {decision.action} {symbol} ({decision.source}, conf {decision.confidence_score:.2f}, "
                    f"size x{decision.position_size_multiplier:.2f}, target {decision.take_profit_r:.1f}R, "
                    f"{decision.latency_ms} ms): {decision.reasoning}")
                if ai_mode == "enforce":
                    if not decision.approved:
                        day_state["ai_vetoed"].add(symbol)
                        notify.send(
                            f"ORB Bot: AI vetoed {symbol}",
                            f"{sig.direction.upper()} breakout @ ${sig.entry_price:.2f} skipped "
                            f"(confidence {decision.confidence_score:.2f}).\n{decision.reasoning}",
                            priority="low", tags="no_entry",
                        )
                        continue
                    shares = int(shares * decision.position_size_multiplier)
                    if shares <= 0:
                        continue
                    r = decision.take_profit_r
                    if sig.direction == "long":
                        sig.target_price = sig.entry_price + r * sig.risk_per_share
                    else:
                        sig.target_price = sig.entry_price - r * sig.risk_per_share
                    ai_note = (f"\nAI: confidence {decision.confidence_score:.2f}, size x{decision.position_size_multiplier:.2f}, "
                               f"target {r:.1f}R. {decision.reasoning}")

            # Hard per-trade notional cap — no single trade may deploy more
            # than MAX_NOTIONAL_PER_TRADE, independent of risk-per-trade sizing
            # or the remaining daily budget checked below.
            max_shares_by_trade_cap = int(config.MAX_NOTIONAL_PER_TRADE // sig.entry_price)
            if max_shares_by_trade_cap < shares:
                log(f"{symbol}: trimming size from {shares} to {max_shares_by_trade_cap} shares "
                    f"to stay within per-trade notional cap (${config.MAX_NOTIONAL_PER_TRADE:,.0f}).")
                shares = max_shares_by_trade_cap
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
                day_state.setdefault("trade_meta", {})[symbol] = {
                    "direction": sig.direction,
                    "entry_price": sig.entry_price,
                    "stop_price": sig.stop_price,
                    "target_price": sig.target_price,
                }
                log_trade_row({
                    "timestamp": ts, "symbol": symbol, "direction": sig.direction,
                    "entry_price": sig.entry_price, "stop": sig.stop_price,
                    "target": sig.target_price, "shares": shares, "order_id": order.id,
                })
                notify.send(
                    f"ORB Bot: Entered {symbol} {sig.direction.upper()}",
                    f"{shares} shares @ ${sig.entry_price:.2f}\n"
                    f"Stop: ${sig.stop_price:.2f}  Target: ${sig.target_price:.2f}{ai_note}",
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
