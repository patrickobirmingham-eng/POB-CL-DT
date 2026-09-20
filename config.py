"""
All tunable parameters for the ORB day trading system live here.
Nothing else in the codebase should hardcode strategy parameters —
this is the single place to tune and re-run backtests from.
"""

# --- Watchlist ---------------------------------------------------------
# Keep this to liquid, high-average-volume names/ETFs. Illiquid stocks blow
# up slippage assumptions and make the backtest lie to you.
WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN",
]

# --- Opening range -------------------------------------------------------
OPENING_RANGE_MINUTES = 15          # length of the opening range (9:30-9:45 ET default)
MIN_OR_RANGE_PCT = 0.001            # skip symbols whose OR is < 0.1% of price (too tight/noisy)
MAX_OR_RANGE_PCT = 0.04             # skip symbols whose OR is > 4% of price (already exhausted the move)

# --- Entry ---------------------------------------------------------------
VOLUME_CONFIRMATION_MULT = 1.5      # breakout bar volume must be >= 1.5x the avg opening-range bar volume
ALLOW_SHORTS = False                # short breakdowns below OR low; requires shortable/marginable symbols
ENTRY_CUTOFF_TIME = "15:00"         # ET — no new entries after this time
BREAKOUT_BUFFER_PCT = 0.0005        # require price to clear the OR high/low by this % to avoid false breaks

# --- Risk management -------------------------------------------------------
RISK_PCT_PER_TRADE = 0.01           # risk 1% of account equity per trade (sized off stop distance)
REWARD_RISK_MULTIPLE = 2.0          # take-profit distance = this many multiples of the stop distance
MAX_TRADES_PER_DAY = 6              # circuit breaker: stop opening new trades after N trades in a day
MAX_DAILY_LOSS_PCT = 0.03           # circuit breaker: stop trading for the day after losing this % of equity
MAX_CONCURRENT_POSITIONS = 3        # don't hold more than N open positions at once
ONE_TRADE_PER_SYMBOL_PER_DAY = True # don't re-enter a symbol after it's been stopped out/closed today

# --- Time stop -------------------------------------------------------------
FLATTEN_TIME = "15:45"              # ET — close everything by this time, no exceptions
MARKET_CLOSE_TIME = "16:00"

# --- Execution / paper trading ----------------------------------------------
ORDER_TYPE = "market"               # entry order type placed once breakout confirms
USE_BRACKET_ORDERS = True           # attach stop-loss + take-profit as a bracket at entry time
POLL_INTERVAL_SECONDS = 15          # how often the live bot checks for breakouts during the OR-complete window
DATA_FEED = "iex"                   # "iex" works on free/paper accounts; use "sip" if you have that data plan

# --- Backtest defaults -------------------------------------------------------
BACKTEST_DEFAULT_DAYS = 60
BACKTEST_SLIPPAGE_PCT = 0.0005      # assumed slippage per fill (0.05%) — deliberately pessimistic
BACKTEST_COMMISSION_PER_TRADE = 0.0 # Alpaca is commission-free; kept for realism if you port this elsewhere

# --- Account / API -----------------------------------------------------------
PAPER_TRADING = True                # this codebase is paper-only; live_bot.py refuses to run if False
