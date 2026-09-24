"""
All tunable parameters for the ORB day trading system live here.
Nothing else in the codebase should hardcode strategy parameters —
this is the single place to tune and re-run backtests from.
"""

# --- Watchlist ---------------------------------------------------------
# Keep this to liquid, high-average-volume names/ETFs. Illiquid stocks blow
# up slippage assumptions and make the backtest lie to you.
#
# Nasdaq-100 constituents as of Sep 2026 (includes both GOOG and GOOGL share
# classes, which is normal for this index). live_bot.py fetches bars for all
# candidate symbols in a single batched API call each poll (get_recent_bars_bulk),
# so a list this size doesn't multiply API requests per symbol the way the old
# per-symbol fetch would have.
WATCHLIST = [
    "ADBE", "AMD", "ABNB", "ALNY", "GOOGL", "GOOG", "AMZN", "AEP", "AMGN", "ADI",
    "AAPL", "AMAT", "APP", "ARM", "ASML", "ALAB", "ADSK", "ADP", "AXON", "BKR",
    "BKNG", "AVGO", "CDNS", "CTAS", "CSCO", "CCEP", "CMCSA", "CEG", "CPRT", "CRWV",
    "COST", "CRWD", "CSX", "DDOG", "DXCM", "FANG", "DASH", "EA", "EXC", "FAST",
    "FER", "FTNT", "GEHC", "GILD", "HON", "IDXX", "INTC", "INTU", "ISRG", "KDP",
    "KLAC", "KHC", "LRCX", "LIN", "LITE", "MAR", "MRVL", "MELI", "META", "MCHP",
    "MU", "MSFT", "MSTR", "MDLZ", "MPWR", "MNST", "NBIS", "NFLX", "NVDA", "NXPI",
    "ORLY", "ODFL", "PCAR", "PLTR", "PANW", "PAYX", "PYPL", "PDD", "PEP", "QCOM",
    "REGN", "RKLB", "ROP", "ROST", "SNDK", "STX", "SHOP", "SBUX", "SNPS", "TMUS",
    "TTWO", "TER", "TSLA", "TXN", "TRI", "VRTX", "WMT", "WBD", "WDC", "WDAY", "XEL",
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

# Hard cap on total NOTIONAL capital deployed across all trades in a single day
# (sum of entry_price * shares at the moment each position is opened), regardless
# of what the risk-per-trade math alone would size a position at. This is a
# separate control from RISK_PCT_PER_TRADE: risk sizing limits how much you can
# LOSE per trade based on stop distance; this limits how much capital gets
# deployed/exposed in the first place. A trade that would exceed the remaining
# daily budget is sized down to fit it (or skipped if the budget is exhausted).
MAX_NOTIONAL_PER_TRADE = 100_000        # never deploy more than $100k notional in a single trade
MAX_DAILY_NOTIONAL_TRADED = 500_000     # e.g. 500_000 = never deploy more than $500k/day total

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

# --- Push notifications (ntfy.sh) --------------------------------------------
# Free, no-account push notifications to your phone. Install the "ntfy" app
# (iOS App Store / Google Play), open it, tap "+", and subscribe to the exact
# topic name below (case-sensitive) — that's the entire setup. Topic names
# on ntfy.sh act like an unlisted channel: anyone who knows the exact string
# could subscribe too, so this one was randomly generated to keep it private.
# Change it any time in this file (then re-subscribe in the app to the new name).
NTFY_ENABLED = True
NTFY_TOPIC = "pob-orb-vkfmwlc1yr"
