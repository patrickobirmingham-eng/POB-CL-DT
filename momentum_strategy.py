"""
Intraday momentum ("noise area" breakout) — pure strategy logic, no I/O.

Rules from Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)" (2024). Backtested on QQQ from 2016
(research_spy_momentum.py), including 2+ years after the paper was published.

Shared by research_spy_momentum.py (backtest) and live_bot.py (paper trading)
so the two can never drift apart — the same idea as strategy.py for ORB.

  sigma(m)  = average over the previous 14 sessions of |close at minute m / open - 1|
  upper(m)  = max(open, prev_close) * (1 + sigma(m))
  lower(m)  = min(open, prev_close) * (1 - sigma(m))
  Decisions only at 10:00, 10:30, ..., 15:30, using the price at that time
  (the close of the minute bar that ends then):
    flat:  price > upper -> long;  price < lower -> short
    long:  exit if price < max(upper, VWAP), then short if price < lower
    short: exit if price > min(lower, VWAP), then long if price > upper
  Flat by the close. Shares = equity * min(max_leverage, target_vol / daily_vol) / open.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

LOOKBACK = 14

# "10:00" is decided on the close of the 09:59 minute bar (it closes at 10:00:00).
CHECK_TIMES = [f"{h:02d}:{m:02d}" for h in range(10, 16) for m in (0, 30)]
CHECK_BARS = {t: (datetime(2000, 1, 1, int(t[:2]), int(t[3:])) - timedelta(minutes=1)).strftime("%H:%M")
              for t in CHECK_TIMES}
MINUTE_GRID = pd.date_range("2000-01-01 09:30", "2000-01-01 15:59", freq="1min").strftime("%H:%M")


def prepare_day(g: pd.DataFrame):
    """g: one session's 1-minute bars (open/high/low/close/volume) indexed by
    tz-aware timestamp. Returns a frame indexed by 'HH:MM' on the full 09:30-15:59
    grid with close (forward-filled), running VWAP and move-from-open."""
    g = g.copy()
    g.index = g.index.strftime("%H:%M")
    g = g[~g.index.duplicated(keep="last")].reindex(MINUTE_GRID)
    g["close"] = g["close"].ffill().bfill()
    g["volume"] = g["volume"].fillna(0)
    for c in ("open", "high", "low"):
        g[c] = g[c].fillna(g["close"])
    typical = (g["high"] + g["low"] + g["close"]) / 3
    vwap = (typical * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
    g["vwap"] = vwap.ffill().fillna(g["close"])
    day_open = float(g["open"].iloc[0])
    g["move"] = (g["close"] / day_open - 1).abs()
    return g, day_open


def sigma_profile(prior_days):
    """prior_days: list of prepared frames (oldest first, at least LOOKBACK).
    Returns the average move-from-open per minute over the last LOOKBACK."""
    recent = prior_days[-LOOKBACK:]
    return pd.concat([d["move"] for d in recent], axis=1).mean(axis=1)


def daily_vol(prior_closes):
    """Stdev of daily close-to-close returns over the last LOOKBACK sessions."""
    closes = pd.Series(prior_closes[-(LOOKBACK + 1):], dtype=float)
    return float(closes.pct_change().dropna().std())


def bands(day_open, prev_close, sigma):
    upper = max(day_open, prev_close) * (1 + sigma)
    lower = min(day_open, prev_close) * (1 - sigma)
    return upper, lower


def position_size(equity, day_open, vol, target_vol, max_leverage):
    if not vol or not np.isfinite(vol) or vol <= 0 or day_open <= 0:
        return 0
    return int(equity * min(max_leverage, target_vol / vol) / day_open)


def decide(position: int, price: float, upper: float, lower: float, vwap: float,
           allow_shorts: bool = True):
    """position: +1 long, -1 short, 0 flat. Returns (exit_now, new_side) where
    new_side is +1/-1 to open after any exit, or 0 for no new position."""
    exit_now = False
    if position > 0 and price < max(upper, vwap):
        exit_now = True
        position = 0
    elif position < 0 and price > min(lower, vwap):
        exit_now = True
        position = 0
    new_side = 0
    if position == 0:
        if price > upper:
            new_side = 1
        elif price < lower and allow_shorts:
            new_side = -1
    return exit_now, new_side
