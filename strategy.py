"""
Pure strategy logic for the Opening Range Breakout system.

No network/broker calls live here on purpose — this module is shared by both
backtest.py (simulated fills) and live_bot.py (real paper-trading fills), so the
signal logic is guaranteed identical between backtest and live. If they diverge,
your backtest is lying to you about how the live bot will behave.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
import pandas as pd

import config


@dataclass
class OpeningRange:
    symbol: str
    date: str
    high: float
    low: float
    avg_volume: float

    @property
    def range_size(self) -> float:
        return self.high - self.low

    def range_pct(self, ref_price: float) -> float:
        return self.range_size / ref_price if ref_price else 0.0


@dataclass
class Signal:
    symbol: str
    direction: str          # "long" or "short"
    entry_price: float
    stop_price: float
    target_price: float
    timestamp: pd.Timestamp
    risk_per_share: float


def compute_opening_range(bars: pd.DataFrame, symbol: str, session_date: str) -> Optional[OpeningRange]:
    """
    bars: minute-bar DataFrame for a single symbol, single day, indexed by tz-aware
    timestamp (US/Eastern), with columns open/high/low/close/volume. Must start at
    the market open (9:30 ET).
    """
    if bars.empty:
        return None

    or_end = bars.index[0] + pd.Timedelta(minutes=config.OPENING_RANGE_MINUTES)
    or_bars = bars[bars.index < or_end]
    if or_bars.empty:
        return None

    return OpeningRange(
        symbol=symbol,
        date=session_date,
        high=or_bars["high"].max(),
        low=or_bars["low"].min(),
        avg_volume=or_bars["volume"].mean(),
    )


def _within_range_filters(orange: OpeningRange, ref_price: float) -> bool:
    pct = orange.range_pct(ref_price)
    return config.MIN_OR_RANGE_PCT <= pct <= config.MAX_OR_RANGE_PCT


def _entry_cutoff() -> time:
    h, m = map(int, config.ENTRY_CUTOFF_TIME.split(":"))
    return time(h, m)


def check_breakout(orange: OpeningRange, bar: pd.Series, timestamp: pd.Timestamp,
                    already_traded_today: bool) -> Optional[Signal]:
    """
    Given the opening range and the current post-OR bar, decide whether a breakout
    signal fires. `bar` must have open/high/low/close/volume.
    """
    if already_traded_today and config.ONE_TRADE_PER_SYMBOL_PER_DAY:
        return None

    if timestamp.time() > _entry_cutoff():
        return None

    ref_price = bar["close"]
    if not _within_range_filters(orange, ref_price):
        return None

    volume_ok = bar["volume"] >= config.VOLUME_CONFIRMATION_MULT * orange.avg_volume
    if not volume_ok:
        return None

    long_trigger = orange.high * (1 + config.BREAKOUT_BUFFER_PCT)
    short_trigger = orange.low * (1 - config.BREAKOUT_BUFFER_PCT)

    if bar["close"] >= long_trigger:
        entry = bar["close"]
        stop = orange.low
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + config.REWARD_RISK_MULTIPLE * risk
        return Signal(orange.symbol, "long", entry, stop, target, timestamp, risk)

    if config.ALLOW_SHORTS and bar["close"] <= short_trigger:
        entry = bar["close"]
        stop = orange.high
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - config.REWARD_RISK_MULTIPLE * risk
        return Signal(orange.symbol, "short", entry, stop, target, timestamp, risk)

    return None


def position_size(equity: float, risk_per_share: float) -> int:
    """Shares to trade so that a stop-out costs RISK_PCT_PER_TRADE of equity."""
    if risk_per_share <= 0:
        return 0
    dollar_risk = equity * config.RISK_PCT_PER_TRADE
    shares = int(dollar_risk / risk_per_share)
    return max(shares, 0)


def flatten_time() -> time:
    h, m = map(int, config.FLATTEN_TIME.split(":"))
    return time(h, m)
