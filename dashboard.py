"""
Generates a static HTML dashboard (docs/index.html) summarizing the paper
trading account: current equity, an equity curve, open positions, and recent
order history. Meant to be run as a step in the GitHub Actions workflow after
each bot session, with the output committed and served via GitHub Pages.

This is read-only — it never places or modifies orders.
"""
import csv
import json
import math
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

import config as bot_config

ET = ZoneInfo("America/New_York")
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")

# Optional "Refresh" button on the dashboard: pulls live account/positions/
# orders on demand via a small read-only PHP proxy (holds the Alpaca keys
# server-side, e.g. hosted on Bluehost) rather than only showing the snapshot
# from whenever the bot last pushed. Both come from GitHub Actions repo
# variables (Settings -> Secrets and variables -> Actions -> Variables) —
# not secrets, since the token ends up embedded in this public page anyway;
# it only deters casual random hits, it's not a real access boundary. Leave
# DASHBOARD_REFRESH_URL unset to hide the button entirely (default today).
REFRESH_ENDPOINT = os.getenv("DASHBOARD_REFRESH_URL", "")
REFRESH_TOKEN = os.getenv("DASHBOARD_REFRESH_TOKEN", "")

# Settings panel (gear icon): lets you change the tunable parameters in
# config.py from the dashboard, in any browser, and have them saved to
# settings.json in the repo — not to this browser's local storage — so
# every browser sees the same values and live_bot.py/strategy.py pick them
# up on their next run (config.py applies settings.json overrides at import
# time). Reading the current values is done straight from the public repo
# (no proxy needed); saving requires a passcode you type each time, checked
# server-side by a small PHP proxy that commits settings.json via the GitHub
# API — the passcode is never embedded in this page, unlike REFRESH_TOKEN
# above, because this one can change live trading parameters, not just read
# data. Leave DASHBOARD_SETTINGS_SAVE_URL unset to hide the gear icon.
SETTINGS_SAVE_ENDPOINT = os.getenv("DASHBOARD_SETTINGS_SAVE_URL", "")
SETTINGS_SOURCE_URL = (
    "https://raw.githubusercontent.com/patrickobirmingham-eng/POB-CL-DT/main/settings.json"
)

# Ordered, grouped list of the config.py parameters exposed in the Settings
# panel. Deliberately excludes PAPER_TRADING (a hardcoded safety rail, never
# meant to be a runtime toggle) and ORDER_TYPE / USE_BRACKET_ORDERS (neither
# is actually read anywhere in live_bot.py, so exposing them as "changeable"
# would silently do nothing).
SETTINGS_FIELDS = [
    {"key": "OPENING_RANGE_MINUTES", "label": "Opening range length", "group": "Entry & Opening Range", "type": "number", "step": 1, "min": 1, "suffix": "min",
     "tooltip": "Length of the opening-range window used to compute breakout levels (e.g. 15 = the first 15 minutes after the open, 9:30–9:45 ET)."},
    {"key": "MIN_OR_RANGE_PCT", "label": "Min opening-range size", "group": "Entry & Opening Range", "type": "percent", "step": 0.01,
     "tooltip": "Skip a symbol for the day if its opening range is smaller than this % of price — a range this tight is usually noise, not a real range."},
    {"key": "MAX_OR_RANGE_PCT", "label": "Max opening-range size", "group": "Entry & Opening Range", "type": "percent", "step": 0.01,
     "tooltip": "Skip a symbol for the day if its opening range is larger than this % of price — a range this wide usually means the move already happened."},
    {"key": "VOLUME_CONFIRMATION_MULT", "label": "Volume confirmation multiple", "group": "Entry & Opening Range", "type": "number", "step": 0.1, "min": 0, "suffix": "x avg OR volume",
     "tooltip": "The breakout bar's volume must be at least this many times the average opening-range bar volume to count as a valid breakout."},
    {"key": "BREAKOUT_BUFFER_PCT", "label": "Breakout buffer", "group": "Entry & Opening Range", "type": "percent", "step": 0.01,
     "tooltip": "Price must clear the opening-range high/low by this % before a breakout triggers, to filter out marginal false breaks."},
    {"key": "ENTRY_CUTOFF_TIME", "label": "Entry cutoff time (ET)", "group": "Entry & Opening Range", "type": "time",
     "tooltip": "No new trades are opened after this time (ET), even if a valid breakout occurs."},
    {"key": "ALLOW_SHORTS", "label": "Allow short breakdowns", "group": "Entry & Opening Range", "type": "bool",
     "tooltip": "If enabled, the bot will also short breakdowns below the opening-range low, not just long breakouts above the high. Requires your account to support shorting."},

    {"key": "RISK_PCT_PER_TRADE", "label": "Risk per trade", "group": "Risk Management", "type": "percent", "step": 0.1, "hint": "% of account equity, sized off stop distance",
     "tooltip": "The dollar amount risked per trade — sized as this % of account equity — based on the distance from entry to stop-loss."},
    {"key": "REWARD_RISK_MULTIPLE", "label": "Reward:risk multiple", "group": "Risk Management", "type": "number", "step": 0.1, "min": 0.1, "suffix": "x stop distance",
     "tooltip": "Take-profit target distance, expressed as a multiple of the stop-loss distance (e.g. 2 = the target is twice as far away as the stop)."},
    {"key": "MAX_TRADES_PER_DAY", "label": "Max trades per day", "group": "Risk Management", "type": "number", "step": 1, "min": 1,
     "tooltip": "Circuit breaker: the bot stops opening new trades once it has opened this many in a single day."},
    {"key": "MAX_DAILY_LOSS_PCT", "label": "Daily loss circuit breaker", "group": "Risk Management", "type": "percent", "step": 0.1, "hint": "stop trading for the day after losing this % of equity",
     "tooltip": "Circuit breaker: if the account's daily P&L drops below −this % of starting equity, the bot flattens all positions and stops trading for the rest of the day."},
    {"key": "MAX_CONCURRENT_POSITIONS", "label": "Max concurrent positions", "group": "Risk Management", "type": "number", "step": 1, "min": 1,
     "tooltip": "The bot won't open a new position if this many positions are already open at once."},
    {"key": "ONE_TRADE_PER_SYMBOL_PER_DAY", "label": "One trade per symbol per day", "group": "Risk Management", "type": "bool",
     "tooltip": "If enabled, a symbol that's already been traded (entered, stopped out, or closed) today won't be re-entered later the same day."},
    {"key": "MAX_NOTIONAL_PER_TRADE", "label": "Max notional per trade", "group": "Risk Management", "type": "number", "step": 1000, "min": 0, "prefix": "$",
     "tooltip": "Hard cap on the dollar amount (entry price × shares) any single trade can deploy, regardless of what risk-per-trade sizing alone would produce."},
    {"key": "MAX_DAILY_NOTIONAL_TRADED", "label": "Max notional per day", "group": "Risk Management", "type": "number", "step": 1000, "min": 0, "prefix": "$",
     "tooltip": "Hard cap on the total dollar amount deployed across all trades combined in a single day — trades are sized down (or skipped) once this is reached."},

    {"key": "BREAKEVEN_TRIGGER_R", "label": "Breakeven trigger", "group": "Breakeven Stop", "type": "nullable_number", "step": 0.1, "min": 0, "suffix": "x initial risk (R)", "hint": "leave blank to disable moving the stop to breakeven",
     "tooltip": "Once a position has moved this many multiples of its initial risk (R) in your favor, its stop-loss moves up to breakeven (entry price) so a winner can't turn into a full loss. Blank disables this."},

    {"key": "TIGHTEN_BEFORE_CLOSE_MINUTES", "label": "Tighten window before close", "group": "Closing-Time Tightening", "type": "nullable_number", "step": 1, "min": 0, "suffix": "min before flatten", "hint": "leave blank to disable tightening take-profit near the close",
     "tooltip": "Starting this many minutes before the flatten-all time, the take-profit limit on each open position is progressively pulled down toward the live price, so a position that popped earlier but has since faded doesn't ride that fade all the way to the forced close. Blank disables this."},
    {"key": "TIGHTEN_STEP_SECONDS", "label": "Tighten update interval", "group": "Closing-Time Tightening", "type": "number", "step": 15, "min": 15, "suffix": "sec",
     "tooltip": "Minimum time between take-profit limit adjustments for the same symbol during the tighten window — keeps this from replacing the order on every single poll."},

    {"key": "FLATTEN_TIME", "label": "Flatten-all time (ET)", "group": "Time / Session", "type": "time",
     "tooltip": "All open positions are closed by this time (ET), no exceptions — the bot's hard end-of-day exit."},
    {"key": "MARKET_CLOSE_TIME", "label": "Market close time (ET)", "group": "Time / Session", "type": "time",
     "tooltip": "The market's official closing time (ET) — a reference point used elsewhere in the bot, separate from the earlier flatten time above."},
    {"key": "POLL_INTERVAL_SECONDS", "label": "Poll interval", "group": "Time / Session", "type": "number", "step": 1, "min": 1, "suffix": "sec",
     "tooltip": "How often (in seconds) the live bot checks prices for breakouts and manages open positions during the trading session."},
    {"key": "DATA_FEED", "label": "Market data feed", "group": "Time / Session", "type": "select", "options": ["iex", "sip"],
     "tooltip": "Which Alpaca market data feed to use. “iex” works on free/paper accounts; “sip” requires a paid data plan but gives fuller market coverage."},

    {"key": "BACKTEST_DEFAULT_DAYS", "label": "Default backtest window", "group": "Backtest Defaults", "type": "number", "step": 1, "min": 1, "suffix": "days",
     "tooltip": "Default number of trading days to look back over when running a backtest, if you don't specify a window."},
    {"key": "BACKTEST_SLIPPAGE_PCT", "label": "Assumed slippage", "group": "Backtest Defaults", "type": "percent", "step": 0.01,
     "tooltip": "Assumed slippage per fill in a backtest, as a % of price — deliberately pessimistic so backtest results don't overstate real-world performance."},
    {"key": "BACKTEST_COMMISSION_PER_TRADE", "label": "Commission per trade", "group": "Backtest Defaults", "type": "number", "step": 0.01, "min": 0, "prefix": "$",
     "tooltip": "Assumed commission per trade in a backtest. Alpaca is commission-free, so this mainly exists for realism if you ever port the strategy elsewhere."},

    {"key": "NTFY_ENABLED", "label": "Push notifications enabled", "group": "Notifications", "type": "bool",
     "tooltip": "Turns push notifications to your phone (via the ntfy.sh app) on or off."},
    {"key": "NTFY_TOPIC", "label": "ntfy.sh topic", "group": "Notifications", "type": "text", "hint": "treat like a password — anyone who knows it can subscribe",
     "tooltip": "The ntfy.sh topic name notifications are sent to — acts like an unlisted channel. Treat it like a password: anyone who knows it can subscribe to your notifications."},

    {"key": "AI_FILTER_MODE", "label": "AI trade filter", "group": "AI Trade Filter", "type": "select", "options": ["enforce", "shadow", "off"],
     "tooltip": "enforce: Claude confirms or vetoes every breakout before it is traded, and can shrink the position or adjust the target (1R-3R). shadow: Claude's decisions are only logged; every signal trades as plain ORB. off: the filter is not called."},
    {"key": "AI_MIN_CONFIDENCE", "label": "Minimum AI confidence", "group": "AI Trade Filter", "type": "number", "step": 0.05, "min": 0, "hint": "0-1; a CONFIRM below this is treated as a veto",
     "tooltip": "Claude reports the probability the trade reaches +1R before its stop. Confirmations below this threshold are treated as vetoes."},
    {"key": "AI_MAX_STOP_PCT", "label": "Max stop distance", "group": "AI Trade Filter", "type": "percent", "step": 0.1, "hint": "hard limit, enforced in code",
     "tooltip": "Any breakout whose stop is further than this % from entry is skipped, regardless of what the AI says."},

    {"key": "MOMENTUM_ENABLED", "label": "QQQ momentum strategy on", "group": "QQQ Momentum Strategy", "type": "bool",
     "tooltip": "Second strategy run alongside ORB: every 30 minutes from 10:00 to 15:30 ET, go long QQQ above its intraday 'noise band' or short below it, with a trailing stop at the band/VWAP, flat by the close. Backtested 2016-2026."},
    {"key": "MOMENTUM_TARGET_VOL", "label": "Target daily volatility", "group": "QQQ Momentum Strategy", "type": "percent", "step": 0.1, "hint": "published strategy uses 2%; started at 1% (half size)",
     "tooltip": "Position size is chosen so the position's expected daily move is this % of equity, based on QQQ's recent daily volatility."},
    {"key": "MOMENTUM_MAX_LEVERAGE", "label": "Max leverage", "group": "QQQ Momentum Strategy", "type": "number", "step": 0.5, "min": 0, "suffix": "x equity",
     "tooltip": "Cap on the QQQ position's notional as a multiple of account equity (published strategy: 4x; started at 2x)."},
    {"key": "MOMENTUM_ALLOW_SHORTS", "label": "Allow shorting QQQ", "group": "QQQ Momentum Strategy", "type": "bool",
     "tooltip": "If off, the momentum strategy only takes long positions (lower returns but smaller drawdowns in the backtest)."},

    {"key": "WATCHLIST", "label": "Watchlist (comma-separated symbols)", "group": "Watchlist (Advanced)", "type": "watchlist",
     "tooltip": "The list of symbols the bot scans for opening-range breakouts each trading day."},
]

CURRENT_SETTINGS = {
    field["key"]: getattr(bot_config, field["key"], None) for field in SETTINGS_FIELDS
}


def get_client():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY first.")
    return TradingClient(key, secret, paper=True)


def build_daily_pl_chart(points, width=900, height=240, pad_left=72, pad_right=16, pad_top=16, pad_bottom=36):
    """points: list of (date_str, float dollar P&L), oldest first, one entry per
    trading day over the lookback window. Returns an inline SVG bar chart with a
    dollar Y axis and a date X axis (green bars for up days, red for down days).
    """
    if not points:
        return "<p class='muted'>Not enough history yet for a chart.</p>"

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    lo = min(lo, 0.0)
    hi = max(hi, 0.0)
    span = (hi - lo) or 1.0
    n = len(points)

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    gap = plot_w / n
    bar_w = max(gap * 0.7, 1.0)

    def y(v):
        return pad_top + (hi - v) / span * plot_h

    zero_y = y(0.0)

    bars = ""
    for i, (d, v) in enumerate(points):
        x = pad_left + i * gap + (gap - bar_w) / 2
        top = y(max(v, 0.0))
        bottom = y(min(v, 0.0))
        h = max(bottom - top, 1.0)
        cls_name = "up" if v >= 0 else "down"
        # data-date/data-pl feed the JS hover tooltip below; the <title> is a
        # native-tooltip fallback for anyone viewing the raw SVG.
        bars += (
            f'<rect class="pl-bar {cls_name}" x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2" '
            f'data-date="{d}" data-pl="{fmt_money(v)}"><title>{d}: {fmt_money(v)}</title></rect>'
        )

    # Sample ~8 evenly-spaced date labels across the X axis rather than one per
    # bar — a year of trading days is far too many to label individually.
    n_labels = min(8, n)
    label_idxs = sorted({round(i * (n - 1) / (n_labels - 1)) for i in range(n_labels)}) if n_labels > 1 else [0]
    x_labels = ""
    for i in label_idxs:
        d, _ = points[i]
        lx = pad_left + i * gap + gap / 2
        x_labels += (
            f'<text class="pl-axis" x="{lx:.1f}" y="{height - pad_bottom + 18}" '
            f'text-anchor="middle">{d}</text>'
        )

    zero_line = (
        f'<line class="pl-zero" x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_right}" y2="{zero_y:.1f}" />'
    )
    # Gridlines at "nice" round dollar values (1/2/5 x 10^k) inside [lo, hi].
    raw_step = span / 5
    mag = 10 ** math.floor(math.log10(raw_step))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw_step)
    grid_lines = ""
    gv = math.ceil(lo / step) * step
    while gv <= hi + 1e-9:
        gy = y(gv)
        grid_lines += f'<line class="pl-grid" x1="{pad_left}" y1="{gy:.1f}" x2="{width - pad_right}" y2="{gy:.1f}" />'
        grid_lines += (
            f'<text class="pl-axis" x="{pad_left - 8}" y="{gy + 3:.1f}" text-anchor="end">'
            f'{fmt_money(gv).replace(".00", "")}</text>'
        )
        gv += step

    return f"""
    <svg viewBox="0 0 {width} {height}" class="pl-chart">
      {grid_lines}
      {zero_line}
      {bars}
      {x_labels}
    </svg>
    """


def fmt_money(v):
    try:
        n = float(v)
        return f"-${abs(n):,.2f}" if n < 0 else f"${n:,.2f}"
    except (TypeError, ValueError):
        return "—"


def raw_num(v):
    """Numeric value for a td's data-value attribute (sortable), or '' if unavailable."""
    try:
        return f"{float(v):.6f}"
    except (TypeError, ValueError):
        return ""


def fetch_all_orders(client, page_size=500, max_orders=5000):
    """Pages back through the account's ENTIRE order history (all statuses),
    newest first, instead of the single most-recent-50 call this used to make.

    Alpaca's get_orders caps each response at `page_size` (max 500) and only
    ever returns the newest slice unless you walk it backwards yourself: each
    subsequent call passes `until` set to the oldest `submitted_at` seen so
    far, so the next page picks up right before it. Without this, once the
    account has placed more than `page_size` orders total, the oldest ones
    (e.g. the very first trading day) silently fall out of every table that's
    built from the order list — Closed Orders, Income by Day, the P&L chart —
    even though they're still sitting in Alpaca's own history.

    `max_orders` is just a safety cap so a runaway loop can't page forever;
    5000 orders is far more than this bot will place in any realistic
    lookback window.
    """
    all_orders = []
    until = None
    while len(all_orders) < max_orders:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=page_size, until=until)
        batch = client.get_orders(req)
        if not batch:
            break
        all_orders.extend(batch)
        if len(batch) < page_size:
            break
        until = min(o.submitted_at for o in batch)
    return sorted(all_orders, key=lambda o: o.submitted_at, reverse=True)


def build_closed_trades(orders):
    """FIFO-matches filled buy orders against filled sell orders (per symbol, in
    chronological order) to produce a list of closed round-trip trades, each
    with its own P&L. A single buy can be closed by multiple partial sells (or
    vice versa) — each matched chunk becomes its own closed-trade row, sized to
    whichever side had fewer remaining shares.

    Only orders present in the `orders` list are considered. Called with the
    full paginated order history (see `fetch_all_orders`), not just the most
    recent 50, so a buy from the very first trading day still gets matched.
    """
    filled = [o for o in orders if o.status and o.status.value == "filled" and o.side and o.filled_avg_price]
    filled.sort(key=lambda o: o.filled_at or o.submitted_at)

    buy_queues = defaultdict(deque)  # symbol -> deque[[qty, price, filled_at]]
    closed = []

    for o in filled:
        qty = float(o.qty)
        price = float(o.filled_avg_price)
        filled_at = o.filled_at or o.submitted_at
        side = o.side.value

        if side == "buy":
            buy_queues[o.symbol].append([qty, price, filled_at])
            continue

        if side != "sell":
            continue

        remaining = qty
        queue = buy_queues[o.symbol]
        while remaining > 1e-9 and queue:
            lot = queue[0]
            matched = min(remaining, lot[0])
            pl = matched * (price - lot[1])
            gain_pct = (price - lot[1]) / lot[1] * 100 if lot[1] else 0.0
            closed.append({
                "symbol": o.symbol,
                "shares": matched,
                "buy_price": lot[1],
                "sell_price": price,
                "pl": pl,
                "gain_pct": gain_pct,
                "transaction_date": filled_at,
            })
            lot[0] -= matched
            remaining -= matched
            if lot[0] <= 1e-9:
                queue.popleft()
        # A sell with no matching buy in this order window is skipped rather
        # than shown with a fabricated entry price.

    closed.sort(key=lambda t: t["transaction_date"], reverse=True)
    return closed


def generate(client=None):
    """Regenerates docs/index.html from the current Alpaca account state.

    Pass an already-connected TradingClient to reuse (e.g. from live_bot.py,
    so this doesn't open a second connection); omit it to create one, which
    is what happens when dashboard.py is run standalone.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Tell GitHub Pages not to run this folder through Jekyll (its default site
    # generator) — without this, Jekyll can fail to build a plain hand-written
    # HTML file and the page never deploys. Recreated every run so it's never
    # accidentally missing.
    open(os.path.join(OUTPUT_DIR, ".nojekyll"), "w").close()

    if client is None:
        client = get_client()

    account = client.get_account()
    positions = client.get_all_positions()

    # Full order history (paginated, all statuses) — used for Closed Orders /
    # Income by Day / the P&L chart, so early trading days never fall out of
    # those totals as the account accumulates more than 50 orders. The
    # "Recent Orders (Last 50)" table and the open-orders list below still
    # only need the newest slice of this, which `all_orders` is already
    # sorted for.
    all_orders = fetch_all_orders(client)
    orders = all_orders[:50]


    generated_at = datetime.now(ET).strftime("%Y-%m-%d %I:%M %p ET")

    # Company names aren't on the Position/Order objects — look each one up via
    # the assets endpoint. Cached per-symbol since the same symbol often shows
    # up in both the positions table and many rows of the orders table.
    _name_cache = {}

    def company_name(symbol):
        if symbol not in _name_cache:
            try:
                _name_cache[symbol] = client.get_asset(symbol).name
            except Exception:
                _name_cache[symbol] = symbol
        return _name_cache[symbol]

    # Current/previous-close price snapshots for every symbol that appears in
    # the orders table, so "Last Price" / "Daily Change" etc. can be shown for
    # order rows the same way they're shown for open positions (positions get
    # this data directly from the Position object; orders don't carry it).
    order_symbols = sorted({o.symbol for o in orders}) if orders else []
    snapshots = {}
    if order_symbols:
        try:
            data_client = StockHistoricalDataClient(
                os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
            )
            snapshots = data_client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=order_symbols, feed="iex")
            )
        except Exception:
            snapshots = {}

    def snapshot_prices(symbol):
        """Returns (current_price, lastday_price) or (None, None) if unavailable."""
        snap = snapshots.get(symbol)
        if not snap:
            return None, None
        current = None
        if getattr(snap, "latest_trade", None) is not None:
            current = float(snap.latest_trade.price)
        elif getattr(snap, "daily_bar", None) is not None:
            current = float(snap.daily_bar.close)
        lastday = None
        if getattr(snap, "previous_daily_bar", None) is not None:
            lastday = float(snap.previous_daily_bar.close)
        return current, lastday

    positions_rows = ""
    total_cost_basis = total_mkt_value = total_pl = total_todays_change = 0.0
    if positions:
        for p in positions:
            qty = float(p.qty)
            current_price = float(p.current_price)
            avg_entry = float(p.avg_entry_price)
            cost_basis = float(p.cost_basis)
            mkt_value = float(p.market_value)
            pl = float(p.unrealized_pl)
            gain_pct = float(p.unrealized_plpc) * 100

            lastday_price = float(p.lastday_price) if p.lastday_price is not None else current_price
            daily_price_change = current_price - lastday_price
            daily_pct_change = (
                float(p.change_today) * 100 if p.change_today is not None
                else (daily_price_change / lastday_price * 100 if lastday_price else 0.0)
            )
            todays_change = qty * daily_price_change  # today's $ P&L on the position, separate from total unrealized P&L

            total_cost_basis += cost_basis
            total_mkt_value += mkt_value
            total_pl += pl
            total_todays_change += todays_change

            pl_class = "pos" if pl >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            daily_class = "pos" if daily_price_change >= 0 else "neg"
            todays_class = "pos" if todays_change >= 0 else "neg"

            positions_rows += f"""
            <tr>
              <td data-value="{p.symbol}">{p.symbol}</td>
              <td data-value="{company_name(p.symbol)}">{company_name(p.symbol)}</td>
              <td class="num" data-value="{raw_num(qty)}">{p.qty}</td>
              <td class="num" data-value="{raw_num(current_price)}">{fmt_money(current_price)}</td>
              <td class="num" data-value="{raw_num(avg_entry)}">{fmt_money(avg_entry)}</td>
              <td class="num" data-value="{raw_num(cost_basis)}">{fmt_money(cost_basis)}</td>
              <td class="num" data-value="{raw_num(mkt_value)}">{fmt_money(mkt_value)}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl)}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_price_change)}">{fmt_money(daily_price_change)}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_pct_change)}">{daily_pct_change:+.2f}%</td>
              <td class="num {todays_class}" data-value="{raw_num(todays_change)}">{fmt_money(todays_change)}</td>
            </tr>"""

        total_pl_class = "pos" if total_pl >= 0 else "neg"
        total_todays_class = "pos" if total_todays_change >= 0 else "neg"
        positions_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num">{fmt_money(total_cost_basis)}</td>
              <td class="num">{fmt_money(total_mkt_value)}</td>
              <td class="num {total_pl_class}">{fmt_money(total_pl)}</td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num {total_todays_class}">{fmt_money(total_todays_change)}</td>
            </tr>"""
    else:
        positions_rows = "<tr><td colspan='12' class='muted'>No open positions</td></tr>"

    # Current Open Orders — pending (not yet filled/canceled/etc.) orders. The
    # bot places bracket orders (OrderClass.BRACKET) for every position: a
    # take-profit limit-sell leg (status "new", carries limit_price) and a
    # stop-loss stop-sell leg (status "held" while the take-profit leg is
    # live, carries stop_price instead) — so each open position shows up as
    # two rows here, one per exit leg, not two separate attempts to sell the
    # same shares. "Purchase Price" is that position's avg entry price
    # (looked up by symbol from the positions we already fetched above);
    # projected P&L / projected stop-loss compare each leg's own trigger
    # price against that entry price. An order with no matching open
    # position (e.g. a fresh entry order that hasn't filled yet), no limit
    # price, or no stop price shows "—" in the columns that don't apply,
    # since there's no basis to project from.
    TERMINAL_ORDER_STATUSES = {
        "filled", "canceled", "expired", "rejected", "done_for_day", "replaced",
    }
    positions_by_symbol = {p.symbol: p for p in positions} if positions else {}
    open_orders = [
        o for o in orders
        if (o.status.value if o.status else "") not in TERMINAL_ORDER_STATUSES
    ]
    open_orders_rows = ""
    total_projected_pl = total_projected_cost_basis = 0.0
    total_stop_pl = total_stop_cost_basis = 0.0
    total_current_mkt_value = 0.0
    if open_orders:
        for o in open_orders:
            submitted_dt = o.submitted_at.astimezone(ET)
            submitted = submitted_dt.strftime("%Y-%m-%d %I:%M %p")
            side = o.side.value if o.side else "—"
            status = o.status.value if o.status else "—"
            side_class = "pos" if side == "buy" else "neg"
            qty = float(o.qty) if o.qty else 0.0
            limit_price = float(o.limit_price) if o.limit_price else None
            stop_price = float(o.stop_price) if getattr(o, "stop_price", None) else None
            current_price, _ = snapshot_prices(o.symbol)

            pos = positions_by_symbol.get(o.symbol)
            purchase_price = float(pos.avg_entry_price) if pos is not None else None
            delta_price = (
                current_price - purchase_price
                if (purchase_price is not None and current_price is not None) else None
            )
            current_mkt_value = qty * delta_price if delta_price is not None else None
            if current_mkt_value is not None:
                total_current_mkt_value += current_mkt_value

            limit_minus_purchase = (
                limit_price - purchase_price
                if (limit_price is not None and purchase_price is not None) else None
            )

            if limit_price is not None and purchase_price is not None:
                per_share = (limit_price - purchase_price) if side == "sell" else (purchase_price - limit_price)
                projected_pl = qty * per_share
                projected_pct = (per_share / purchase_price * 100) if purchase_price else None
                total_projected_pl += projected_pl
                total_projected_cost_basis += qty * purchase_price
            else:
                projected_pl = None
                projected_pct = None

            if stop_price is not None and purchase_price is not None:
                stop_per_share = (stop_price - purchase_price) if side == "sell" else (purchase_price - stop_price)
                projected_stop_pl = qty * stop_per_share
                projected_stop_pct = (stop_per_share / purchase_price * 100) if purchase_price else None
                total_stop_pl += projected_stop_pl
                total_stop_cost_basis += qty * purchase_price
            else:
                projected_stop_pl = None
                projected_stop_pct = None

            delta_class = "pos" if (delta_price is not None and delta_price >= 0) else ("neg" if delta_price is not None else "")
            limit_minus_purchase_class = "pos" if (limit_minus_purchase is not None and limit_minus_purchase >= 0) else ("neg" if limit_minus_purchase is not None else "")
            pl_class = "pos" if (projected_pl is not None and projected_pl >= 0) else ("neg" if projected_pl is not None else "")
            pct_class = "pos" if (projected_pct is not None and projected_pct >= 0) else ("neg" if projected_pct is not None else "")
            stop_pl_class = "pos" if (projected_stop_pl is not None and projected_stop_pl >= 0) else ("neg" if projected_stop_pl is not None else "")
            stop_pct_class = "pos" if (projected_stop_pct is not None and projected_stop_pct >= 0) else ("neg" if projected_stop_pct is not None else "")

            open_orders_rows += f"""
            <tr>
              <td data-value="{submitted_dt.isoformat()}">{submitted}</td>
              <td data-value="{o.symbol}">{o.symbol}</td>
              <td data-value="{company_name(o.symbol)}">{company_name(o.symbol)}</td>
              <td class="{side_class}" data-value="{side}">{side.upper()}</td>
              <td class="num" data-value="{raw_num(qty)}">{o.qty}</td>
              <td data-value="{status}">{status}</td>
              <td class="num" data-value="{raw_num(current_price)}">{fmt_money(current_price) if current_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(current_mkt_value)}">{fmt_money(current_mkt_value) if current_mkt_value is not None else "—"}</td>
              <td class="num" data-value="{raw_num(purchase_price)}">{fmt_money(purchase_price) if purchase_price is not None else "—"}</td>
              <td class="num {delta_class}" data-value="{raw_num(delta_price)}">{fmt_money(delta_price) if delta_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(limit_price)}">{fmt_money(limit_price) if limit_price is not None else "—"}</td>
              <td class="num {limit_minus_purchase_class}" data-value="{raw_num(limit_minus_purchase)}">{fmt_money(limit_minus_purchase) if limit_minus_purchase is not None else "—"}</td>
              <td class="num {pl_class}" data-value="{raw_num(projected_pl)}">{fmt_money(projected_pl) if projected_pl is not None else "—"}</td>
              <td class="num {pct_class}" data-value="{raw_num(projected_pct)}">{f"{projected_pct:+.2f}%" if projected_pct is not None else "—"}</td>
              <td class="num" data-value="{raw_num(stop_price)}">{fmt_money(stop_price) if stop_price is not None else "—"}</td>
              <td class="num {stop_pl_class}" data-value="{raw_num(projected_stop_pl)}">{fmt_money(projected_stop_pl) if projected_stop_pl is not None else "—"}</td>
              <td class="num {stop_pct_class}" data-value="{raw_num(projected_stop_pct)}">{f"{projected_stop_pct:+.2f}%" if projected_stop_pct is not None else "—"}</td>
            </tr>"""

        total_projected_pct = (total_projected_pl / total_projected_cost_basis * 100) if total_projected_cost_basis else None
        total_stop_pct = (total_stop_pl / total_stop_cost_basis * 100) if total_stop_cost_basis else None
        total_pl_class = "pos" if total_projected_pl >= 0 else "neg"
        total_pct_class = "pos" if (total_projected_pct is not None and total_projected_pct >= 0) else ""
        total_stop_pl_class = "pos" if total_stop_pl >= 0 else "neg"
        total_stop_pct_class = "pos" if (total_stop_pct is not None and total_stop_pct >= 0) else ""
        total_current_mkt_value_class = "pos" if total_current_mkt_value >= 0 else "neg"
        open_orders_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num {total_current_mkt_value_class}">{fmt_money(total_current_mkt_value)}</td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num {total_pl_class}">{fmt_money(total_projected_pl)}</td>
              <td class="num {total_pct_class}">{f"{total_projected_pct:+.2f}%" if total_projected_pct is not None else "—"}</td>
              <td></td>
              <td class="num {total_stop_pl_class}">{fmt_money(total_stop_pl)}</td>
              <td class="num {total_stop_pct_class}">{f"{total_stop_pct:+.2f}%" if total_stop_pct is not None else "—"}</td>
            </tr>"""
    else:
        open_orders_rows = "<tr><td colspan='17' class='muted'>No open orders</td></tr>"

    orders_rows = ""
    order_statuses_seen = set()
    if orders:
        for o in orders:
            submitted_dt = o.submitted_at.astimezone(ET)
            submitted = submitted_dt.strftime("%Y-%m-%d %I:%M %p")
            side = o.side.value if o.side else "—"
            status = o.status.value if o.status else "—"
            order_statuses_seen.add(status)
            filled_price = float(o.filled_avg_price) if o.filled_avg_price else None
            qty = float(o.qty) if o.qty else 0.0
            side_class = "pos" if side == "buy" else "neg"

            current_price, lastday_price = snapshot_prices(o.symbol)

            # "Purchase Price" / "Cost Basis" mirror the open-positions table: the
            # price this order actually filled at, and qty * that price. Only
            # meaningful for filled orders — unfilled/canceled orders show "—".
            cost_basis = qty * filled_price if filled_price is not None else None
            mkt_value = qty * current_price if current_price is not None else None
            pl = (mkt_value - cost_basis) if (mkt_value is not None and cost_basis is not None) else None
            gain_pct = (pl / cost_basis * 100) if (pl is not None and cost_basis not in (None, 0)) else None

            daily_price_change = (
                current_price - lastday_price
                if (current_price is not None and lastday_price is not None)
                else None
            )
            daily_pct_change = (
                daily_price_change / lastday_price * 100
                if (daily_price_change is not None and lastday_price)
                else None
            )
            todays_change = qty * daily_price_change if daily_price_change is not None else None

            pl_class = "pos" if (pl is not None and pl >= 0) else ("neg" if pl is not None else "")
            gain_class = "pos" if (gain_pct is not None and gain_pct >= 0) else ("neg" if gain_pct is not None else "")
            daily_class = "pos" if (daily_price_change is not None and daily_price_change >= 0) else ("neg" if daily_price_change is not None else "")
            todays_class = "pos" if (todays_change is not None and todays_change >= 0) else ("neg" if todays_change is not None else "")

            orders_rows += f"""
            <tr data-status="{status}">
              <td data-value="{submitted_dt.isoformat()}">{submitted}</td>
              <td data-value="{o.symbol}">{o.symbol}</td>
              <td data-value="{company_name(o.symbol)}">{company_name(o.symbol)}</td>
              <td class="{side_class}" data-value="{side}">{side.upper()}</td>
              <td class="num" data-value="{raw_num(qty)}">{o.qty}</td>
              <td data-value="{status}">{status}</td>
              <td class="num" data-value="{raw_num(current_price)}">{fmt_money(current_price) if current_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(filled_price)}">{fmt_money(filled_price) if filled_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(cost_basis)}">{fmt_money(cost_basis) if cost_basis is not None else "—"}</td>
              <td class="num" data-value="{raw_num(mkt_value)}">{fmt_money(mkt_value) if mkt_value is not None else "—"}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl) if pl is not None else "—"}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{f"{gain_pct:+.2f}%" if gain_pct is not None else "—"}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_price_change)}">{fmt_money(daily_price_change) if daily_price_change is not None else "—"}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_pct_change)}">{f"{daily_pct_change:+.2f}%" if daily_pct_change is not None else "—"}</td>
              <td class="num {todays_class}" data-value="{raw_num(todays_change)}">{fmt_money(todays_change) if todays_change is not None else "—"}</td>
            </tr>"""
    else:
        orders_rows = "<tr><td colspan='15' class='muted'>No orders yet</td></tr>"

    closed_trades = build_closed_trades(all_orders)
    closed_rows = ""
    total_closed_pl = 0.0
    total_purchase_cost = 0.0  # sum of shares * buy_price, across all closed trades
    total_sell_proceeds = 0.0  # sum of shares * sell_price, across all closed trades
    if closed_trades:
        for t in closed_trades:
            tx_dt = t["transaction_date"].astimezone(ET)
            pl = t["pl"]
            gain_pct = t["gain_pct"]
            total_closed_pl += pl
            total_purchase_cost += t["shares"] * t["buy_price"]
            total_sell_proceeds += t["shares"] * t["sell_price"]
            pl_class = "pos" if pl >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            closed_rows += f"""
            <tr>
              <td data-value="{tx_dt.isoformat()}">{tx_dt.strftime("%Y-%m-%d %I:%M %p")}</td>
              <td data-value="{t['symbol']}">{t['symbol']}</td>
              <td data-value="{company_name(t['symbol'])}">{company_name(t['symbol'])}</td>
              <td class="num" data-value="{raw_num(t['shares'])}">{t['shares']:g}</td>
              <td class="num" data-value="{raw_num(t['buy_price'])}">{fmt_money(t['buy_price'])}</td>
              <td class="num" data-value="{raw_num(t['sell_price'])}">{fmt_money(t['sell_price'])}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl)}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
            </tr>"""
        total_class = "pos" if total_closed_pl >= 0 else "neg"
        # Overall % gain/loss for the totals row: total P&L over total capital put
        # in (sum of purchase costs) — the same "$ out vs $ back" basis used for
        # each individual row, just aggregated, rather than an average of the
        # per-trade percentages (which would let a tiny trade's swing skew the
        # total as much as a big one).
        total_gain_pct = (total_closed_pl / total_purchase_cost * 100) if total_purchase_cost else 0.0
        total_gain_class = "pos" if total_gain_pct >= 0 else "neg"
        closed_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num">{fmt_money(total_purchase_cost)}</td>
              <td class="num">{fmt_money(total_sell_proceeds)}</td>
              <td class="num {total_class}">{fmt_money(total_closed_pl)}</td>
              <td class="num {total_gain_class}">{total_gain_pct:+.2f}%</td>
            </tr>"""
    else:
        closed_rows = "<tr><td colspan='8' class='muted'>No closed trades yet</td></tr>"

    # Income by Day — closed_trades rolled up per calendar date (ET), drawn
    # from the full paginated order history, so every trading day since the
    # account started shows up here, not just the most recent ones.
    daily = defaultdict(lambda: {"trades": 0, "cost_basis": 0.0, "sold_cost": 0.0, "pl": 0.0})
    for t in closed_trades:
        day = t["transaction_date"].astimezone(ET).strftime("%Y-%m-%d")
        d = daily[day]
        d["trades"] += 1
        d["cost_basis"] += t["shares"] * t["buy_price"]
        d["sold_cost"] += t["shares"] * t["sell_price"]
        d["pl"] += t["pl"]

    daily_rows = ""
    if daily:
        for day in sorted(daily.keys(), reverse=True):
            d = daily[day]
            gain_pct = (d["pl"] / d["cost_basis"] * 100) if d["cost_basis"] else 0.0
            pl_class = "pos" if d["pl"] >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            daily_rows += f"""
            <tr>
              <td data-value="{day}">{day}</td>
              <td class="num" data-value="{raw_num(d['trades'])}">{d['trades']}</td>
              <td class="num" data-value="{raw_num(d['cost_basis'])}">{fmt_money(d['cost_basis'])}</td>
              <td class="num" data-value="{raw_num(d['sold_cost'])}">{fmt_money(d['sold_cost'])}</td>
              <td class="num {pl_class}" data-value="{raw_num(d['pl'])}">{fmt_money(d['pl'])}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
            </tr>"""
        total_daily_trades = sum(d["trades"] for d in daily.values())
        total_daily_cost_basis = sum(d["cost_basis"] for d in daily.values())
        total_daily_sold_cost = sum(d["sold_cost"] for d in daily.values())
        total_daily_pl = sum(d["pl"] for d in daily.values())
        total_daily_gain_pct = (total_daily_pl / total_daily_cost_basis * 100) if total_daily_cost_basis else 0.0
        total_daily_pl_class = "pos" if total_daily_pl >= 0 else "neg"
        total_daily_gain_class = "pos" if total_daily_gain_pct >= 0 else "neg"
        daily_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td class="num">{total_daily_trades}</td>
              <td class="num">{fmt_money(total_daily_cost_basis)}</td>
              <td class="num">{fmt_money(total_daily_sold_cost)}</td>
              <td class="num {total_daily_pl_class}">{fmt_money(total_daily_pl)}</td>
              <td class="num {total_daily_gain_class}">{total_daily_gain_pct:+.2f}%</td>
            </tr>"""
    else:
        daily_rows = "<tr><td colspan='6' class='muted'>No closed trades yet</td></tr>"

    # Status filter checkboxes — built from whatever statuses actually showed up
    # in the last 50 orders, so the filter row never shows a status with zero
    # matching rows. "Filled" starts checked by default; every other status
    # (canceled, etc.) starts unchecked so the table opens focused on filled
    # orders, and the user can opt back in to the rest.
    status_filters_html = ""
    for status in sorted(order_statuses_seen):
        label = status.replace("_", " ").title()
        checked_attr = "checked " if status == "filled" else ""
        status_filters_html += (
            f'<label class="filter-chip">'
            f'<input type="checkbox" class="status-filter" value="{status}" {checked_attr}'
            f'onchange="filterOrders()"> {label}</label>'
        )
    if not status_filters_html:
        status_filters_html = '<span class="muted">No orders yet</span>'

    # Chart data mirrors the Income by Day table exactly (same `daily` dict,
    # same per-day P&L), just sorted oldest-first for left-to-right plotting.
    daily_pl_points = [(day, daily[day]["pl"]) for day in sorted(daily.keys())]
    daily_pl_chart_html = build_daily_pl_chart(daily_pl_points)

    # AI filter decisions (ai_decisions.csv, written by ai_filter.py), newest
    # first. Confirmed trades are matched to that symbol's realized P&L for the
    # same day, so the AI's calls can be judged against real outcomes.
    pl_by_symbol_day = defaultdict(float)
    for t in closed_trades:
        pl_by_symbol_day[(t["symbol"], t["transaction_date"].astimezone(ET).strftime("%Y-%m-%d"))] += t["pl"]
    ai_rows_data = []
    try:
        with open("ai_decisions.csv", newline="") as f:
            ai_rows_data = list(csv.DictReader(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"dashboard: could not read ai_decisions.csv ({e})")
    ai_rows_data = ai_rows_data[::-1][:100]
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    ai_today = [r for r in ai_rows_data if r.get("timestamp", "").startswith(today_str)]
    ai_confirmed_today = sum(1 for r in ai_today if r.get("action") == "CONFIRM")
    ai_vetoed_today = sum(1 for r in ai_today if r.get("action") == "VETO")
    ai_mode_now = str(getattr(bot_config, "AI_FILTER_MODE", "enforce"))
    ai_rows = ""
    for r in ai_rows_data:
        ts = r.get("timestamp", "")
        try:
            ts_dt = datetime.fromisoformat(ts)
            ts_disp = ts_dt.strftime("%Y-%m-%d %I:%M %p")
            day = ts_dt.strftime("%Y-%m-%d")
        except ValueError:
            ts_disp, day = ts, ""
        sym = r.get("symbol", "")
        action = r.get("action", "")
        act_class = "pos" if action == "CONFIRM" else "neg"
        source = r.get("source", "")
        source_label = {"ai": "AI", "fail_open": "Fallback (no AI)", "hard_limit": "Hard limit"}.get(source, source)
        if r.get("mode") == "shadow":
            source_label += " · shadow"
        outcome = pl_by_symbol_day.get((sym, day)) if action == "CONFIRM" else None
        out_class = "" if outcome is None else ("pos" if outcome >= 0 else "neg")
        reasoning = (r.get("reasoning", "") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ai_rows += f"""
            <tr>
              <td data-value="{ts}">{ts_disp}</td>
              <td data-value="{sym}">{sym}</td>
              <td data-value="{r.get('direction', '')}">{(r.get('direction', '') or '').upper()}</td>
              <td class="num" data-value="{raw_num(r.get('entry_price'))}">{fmt_money(r.get('entry_price'))}</td>
              <td class="{act_class}" data-value="{action}"><strong>{action}</strong></td>
              <td class="num" data-value="{raw_num(r.get('confidence')) if source == 'ai' else ''}">{r.get('confidence', '') if source == 'ai' else '—'}</td>
              <td class="num" data-value="{raw_num(r.get('size_mult'))}">x{r.get('size_mult', '')}</td>
              <td class="num" data-value="{raw_num(r.get('take_profit_r'))}">{r.get('take_profit_r', '')}R</td>
              <td data-value="{source_label}">{source_label}</td>
              <td class="num {out_class}" data-value="{raw_num(outcome)}">{fmt_money(outcome) if outcome is not None else "—"}</td>
              <td class="reason">{reasoning}</td>
            </tr>"""
    if not ai_rows:
        ai_rows = "<tr><td colspan='11' class='muted'>No AI decisions yet — they appear here as breakouts are evaluated.</td></tr>"

    wins = sum(1 for t in closed_trades if t["pl"] > 0)
    win_rate_text = f"{wins / len(closed_trades) * 100:.0f}%" if closed_trades else "—"

    refresh_button_html = (
        '<button id="refreshBtn" onclick="refreshDashboard()">&#8635; Refresh</button>'
        if REFRESH_ENDPOINT else ""
    )

    settings_button_html = (
        '<button id="settingsBtn" onclick="openSettingsModal()" '
        'aria-label="Trading bot settings">&#9881; Settings</button>'
        if SETTINGS_SAVE_ENDPOINT else ""
    )

    settings_fields_json = json.dumps(SETTINGS_FIELDS)
    current_settings_json = json.dumps(CURRENT_SETTINGS)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude.AI Paper Day Trading</title>
<style>
  :root {{
    --bg: #0a0d14; --bg-2: #0d1119; --panel: #111621; --panel-2: #151b28; --border: #1f2637; --border-strong: #2b344a;
    --text: #e8ebf2; --muted: #8791a7; --faint: #5d667b;
    --pos: #22c55e; --neg: #f0504e; --accent: #4f8cff; --accent-soft: rgba(79, 140, 255, 0.14);
    --row-hover: rgba(255, 255, 255, 0.03); --shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
    --grid: rgba(255, 255, 255, 0.06);
  }}
  :root[data-theme="light"] {{
    --bg: #f3f5f9; --bg-2: #eaeef5; --panel: #ffffff; --panel-2: #f8f9fc; --border: #e3e8f0; --border-strong: #cfd6e3;
    --text: #131a29; --muted: #5a6479; --faint: #8a93a6;
    --pos: #12833f; --neg: #c62828; --accent: #1f5fe0; --accent-soft: rgba(31, 95, 224, 0.10);
    --row-hover: rgba(15, 30, 60, 0.035); --shadow: 0 1px 2px rgba(20, 30, 60, 0.06);
    --grid: rgba(15, 30, 60, 0.08);
  }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 14px; line-height: 1.45; -webkit-font-smoothing: antialiased;
    font-feature-settings: "tnum" 1, "cv11" 1;
    transition: background 0.15s ease, color 0.15s ease;
  }}
  .wrap {{ max-width: 1800px; width: 100%; margin: 0 auto; padding: 28px 32px 40px 32px; }}

  .topbar {{
    position: sticky; top: 0; z-index: 100;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
  }}
  .topbar-inner {{
    max-width: 1800px; margin: 0 auto; padding: 14px 32px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }}
  .brand {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
  .brand-mark {{
    width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--accent), #7c5cff);
    display: flex; align-items: center; justify-content: center; color: #fff; font-size: 16px; font-weight: 700;
  }}
  .brand h1 {{ font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .brand .sub {{ color: var(--muted); font-size: 12px; margin-top: 1px; }}
  .badge {{
    display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 9px; border-radius: 999px;
    color: var(--accent); background: var(--accent-soft); border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
  }}
  .badge::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }}
  #topActions {{ display: flex; gap: 8px; align-items: center; }}
  #topActions button, #refreshBtn {{
    background: var(--panel); color: var(--text); border: 1px solid var(--border-strong);
    border-radius: 8px; padding: 7px 13px; font-size: 13px; font-weight: 500; font-family: inherit; cursor: pointer;
    transition: border-color 0.12s ease, background 0.12s ease;
  }}
  #topActions button:hover, #refreshBtn:hover {{ border-color: var(--accent); background: var(--panel-2); }}

  .updated {{
    color: var(--muted); font-size: 12.5px; margin-bottom: 22px;
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  }}
  .updated #lastUpdated {{ color: var(--text); font-weight: 500; }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .card {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px;
    box-shadow: var(--shadow); position: relative; overflow: hidden;
  }}
  .card::before {{ content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--border-strong); }}
  .card.primary::before {{ background: var(--accent); }}
  .card.pos-card::before {{ background: var(--pos); }}
  .card.neg-card::before {{ background: var(--neg); }}
  .card .label {{ color: var(--muted); font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 8px; }}
  .card .value {{ font-size: 26px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.1; }}

  .panel {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 22px 22px 22px; margin-bottom: 20px; box-shadow: var(--shadow);
  }}
  .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }}
  .panel-head h2 {{ margin: 0; }}
  .symbol-search {{
    width: 320px; max-width: 100%; margin-left: auto;
    background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); border-radius: 8px;
    padding: 7px 12px; font-size: 13px; font-family: inherit;
    transition: border-color 0.12s ease, box-shadow 0.12s ease;
  }}
  .symbol-search::placeholder {{ color: var(--faint); }}
  .symbol-search:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }}
  tr.no-match td {{ color: var(--muted); padding: 18px 8px; }}
  td.reason {{ text-align: left; color: var(--muted); font-size: 12.5px; min-width: 260px; }}
  .ai-summary {{ font-size: 12.5px; }}
  .panel h2 {{
    font-size: 13px; font-weight: 650; margin: 0 0 16px 0; color: var(--text);
    text-transform: uppercase; letter-spacing: 0.07em; display: flex; align-items: center; gap: 10px;
  }}
  .panel h2::before {{ content: ""; width: 3px; height: 14px; border-radius: 2px; background: var(--accent); }}
  .table-scroll {{ overflow-x: auto; margin: 0 -4px; padding: 0 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; table-layout: auto; font-variant-numeric: tabular-nums; }}
  th {{
    text-align: center; color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 10px 8px; border-bottom: 1px solid var(--border-strong); background: var(--panel-2);
    white-space: normal; word-wrap: break-word; overflow-wrap: break-word; line-height: 1.3; vertical-align: bottom;
  }}
  th:first-child {{ border-top-left-radius: 8px; }}
  th:last-child {{ border-top-right-radius: 8px; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ color: var(--text); }}
  th.sortable::after {{ content: "⇅"; color: var(--faint); margin-left: 4px; font-size: 10px; }}
  th.sortable[data-dir="asc"]::after {{ content: "▲"; color: var(--accent); }}
  th.sortable[data-dir="desc"]::after {{ content: "▼"; color: var(--accent); }}
  td {{ padding: 10px 8px; border-bottom: 1px solid var(--border); white-space: normal; word-wrap: break-word; overflow-wrap: break-word; text-align: center; }}
  tbody tr:not(.totals-row):hover td {{ background: var(--row-hover); }}
  tbody tr:last-child td {{ border-bottom: none; }}
  .totals-row td {{ font-weight: 650; background: var(--panel-2); border-top: 1px solid var(--border-strong); border-bottom: none; }}
  .pos {{ color: var(--pos); }}
  .neg {{ color: var(--neg); }}
  .muted {{ color: var(--muted); }}
  .pl-chart {{ width: 100%; height: auto; max-height: 320px; display: block; }}
  .chart-wrap {{ position: relative; }}
  .pl-bar {{ cursor: pointer; transition: opacity 0.1s ease; }}
  .pl-bar.up {{ fill: var(--pos); }}
  .pl-bar.down {{ fill: var(--neg); }}
  .pl-bar:hover {{ opacity: 0.7; }}
  .pl-grid {{ stroke: var(--grid); stroke-width: 1; }}
  .pl-zero {{ stroke: var(--border-strong); stroke-width: 1.2; }}
  .pl-axis {{ fill: var(--muted); font-size: 10.5px; }}
  .pl-tooltip {{
    position: absolute; display: none; pointer-events: none;
    background: var(--panel-2); border: 1px solid var(--border-strong); border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3);
    padding: 7px 11px; font-size: 12px; font-weight: 500; color: var(--text); white-space: nowrap;
    transform: translate(-50%, -100%); margin-top: -10px; z-index: 10;
  }}
  .disclaimer {{ color: var(--faint); font-size: 12px; margin-top: 28px; line-height: 1.6; text-align: center; border-top: 1px solid var(--border); padding-top: 18px; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
  .filter-chip {{
    display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); cursor: pointer;
    padding: 5px 11px; border: 1px solid var(--border-strong); border-radius: 999px; background: var(--panel-2);
    transition: border-color 0.12s ease, color 0.12s ease;
  }}
  .filter-chip:hover {{ border-color: var(--accent); color: var(--text); }}
  .filter-chip input {{ accent-color: var(--accent); cursor: pointer; margin: 0; }}

  .modal-overlay {{
    position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); z-index: 200;
    display: flex; align-items: flex-start; justify-content: center;
    padding: 60px 16px 0 16px; overflow-y: auto;
  }}
  .modal {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    max-width: 640px; width: 100%; padding: 20px 20px 0 20px; margin-bottom: 40px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4);
  }}
  .modal-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }}
  .modal-header h2 {{ font-size: 17px; margin: 0; }}
  .modal-close {{
    background: none; border: none; color: var(--muted); font-size: 22px; line-height: 1;
    cursor: pointer; padding: 4px 8px;
  }}
  .modal-close:hover {{ color: var(--text); }}
  .settings-subtitle {{ color: var(--muted); font-size: 12px; margin: 0 0 16px 0; line-height: 1.5; }}
  .settings-status {{ font-size: 13px; font-weight: 500; padding: 9px 12px; border-radius: 8px; display: none; }}
  .settings-status.ok, .settings-status.err, .settings-status.info {{ display: block; }}
  .settings-status.info {{ color: var(--muted); background: var(--panel-2); border: 1px solid var(--border); }}
  .settings-status.ok {{ color: var(--pos); background: color-mix(in srgb, var(--pos) 12%, transparent); border: 1px solid color-mix(in srgb, var(--pos) 40%, transparent); }}
  .settings-status.err {{ color: var(--neg); background: color-mix(in srgb, var(--neg) 12%, transparent); border: 1px solid color-mix(in srgb, var(--neg) 40%, transparent); }}
  .settings-group {{ margin-bottom: 18px; }}
  .settings-group h3 {{
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
    margin: 0 0 8px 0; border-bottom: 1px solid var(--border); padding-bottom: 6px;
  }}
  .settings-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 7px 0;
  }}
  .settings-row label {{ font-size: 13px; }}
  .settings-row .settings-hint {{ display: block; color: var(--muted); font-size: 11px; margin-top: 2px; }}
  .settings-info {{ display: inline-block; margin-left: 5px; cursor: help; color: var(--muted); font-size: 13px; }}
  .settings-row .settings-control {{ display: flex; align-items: center; gap: 6px; flex-shrink: 0; }}
  .settings-row input[type="text"], .settings-row input[type="number"], .settings-row input[type="time"], .settings-row select {{
    background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
    padding: 5px 8px; font-size: 13px; width: 120px;
  }}
  .settings-row input[type="checkbox"] {{ accent-color: var(--accent); width: 16px; height: 16px; cursor: pointer; }}
  .settings-row .suffix {{ color: var(--muted); font-size: 12px; }}
  #settingsWatchlist {{
    width: 100%; min-height: 90px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-size: 12px;
    font-family: inherit; resize: vertical;
  }}
  .modal-footer {{
    display: flex; flex-direction: column; gap: 10px; margin-top: 18px; padding: 14px 0 20px 0;
    border-top: 1px solid var(--border);
    position: sticky; bottom: 0; background: var(--panel); z-index: 5;
  }}
  .modal-footer-row {{ display: flex; align-items: center; gap: 10px; }}
  #settingsToken {{
    flex: 1; background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 7px 10px; font-size: 13px;
  }}
  #settingsSaveBtn {{
    background: var(--accent); color: #fff; border: none; border-radius: 6px;
    padding: 8px 16px; font-size: 13px; cursor: pointer; white-space: nowrap;
  }}
  #settingsSaveBtn:hover {{ opacity: 0.9; }}
  #settingsSaveBtn:disabled {{ opacity: 0.5; cursor: default; }}
  .settings-loading {{ color: var(--muted); font-size: 13px; padding: 20px 0; text-align: center; }}

  #refreshBtn:disabled {{ opacity: 0.5; cursor: default; }}

  @media (max-width: 720px) {{
    .wrap {{ padding: 16px 12px 28px 12px; }}
    .topbar-inner {{ padding: 10px 12px; }}
    .brand .sub, #topActions .badge {{ display: none; }}
    .cards .card:last-child:nth-child(odd) {{ grid-column: span 2; }}
    .brand-mark {{ width: 28px; height: 28px; font-size: 14px; }}
    .brand h1 {{ font-size: 14px; }}
    #topActions button {{ padding: 6px 10px; font-size: 12px; }}
    .modal-overlay {{ padding: 20px 10px 0 10px; }}
    .settings-row {{ flex-wrap: wrap; }}
    .cards {{ grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 16px; }}
    .card {{ padding: 13px 14px; }}
    .card .value {{ font-size: 19px; }}
    .panel {{ padding: 14px 12px; margin-bottom: 14px; }}
    .symbol-search {{ width: 100%; margin-left: 0; }}
    table {{ font-size: 12px; }}
    th, td {{ padding: 8px 7px; }}
    .updated {{ gap: 6px; }}
  }}
</style>
</head>
<body>
  <header class="topbar"><div class="topbar-inner">
    <div class="brand">
      <div class="brand-mark">&#9650;</div>
      <div>
        <h1>Claude.AI Paper Day Trading</h1>
        <div class="sub">Opening Range Breakout &middot; Alpaca paper account</div>
      </div>
    </div>
    <div id="topActions">
      <span class="badge">Paper</span>
      {settings_button_html}
      <button id="themeToggleBtn" onclick="toggleTheme()" aria-label="Toggle dark/light theme">&#9728; Light</button>
    </div>
  </div></header>

  <div id="settingsModal" class="modal-overlay" style="display:none;" onclick="if (event.target === this) closeSettingsModal();">
    <div class="modal">
      <div class="modal-header">
        <h2>Trading Bot Settings</h2>
        <button class="modal-close" onclick="closeSettingsModal()" aria-label="Close">&times;</button>
      </div>
      <p class="settings-subtitle">
        Changes save to settings.json in the repo (not this browser), so every browser sees the
        same values. live_bot.py and strategy.py pick them up on their next run/poll — usually within
        a few minutes, not immediately mid-session.
      </p>
      <div class="modal-body" id="settingsBody">
        <div class="settings-loading">Loading current settings…</div>
      </div>
      <div class="modal-footer">
        <div id="settingsStatus" class="settings-status" role="status" aria-live="polite"></div>
        <div class="modal-footer-row">
          <input type="password" id="settingsToken" placeholder="Passcode to save" autocomplete="off">
          <button id="settingsSaveBtn" onclick="saveSettings()">Save Changes</button>
        </div>
      </div>
    </div>
  </div>

  <div class="wrap">
    <div class="updated">
      <span>Last updated</span> <span id="lastUpdated">{generated_at}</span>
      {refresh_button_html}
      <span id="refreshStatus" class="muted"></span>
    </div>

    <div class="cards">
      <div class="card primary"><div class="label">Equity</div><div class="value">{fmt_money(account.equity)}</div></div>
      <div class="card"><div class="label">Cash</div><div class="value">{fmt_money(account.cash)}</div></div>
      <div class="card"><div class="label">Buying Power</div><div class="value">{fmt_money(account.buying_power)}</div></div>
      <div class="card"><div class="label">Open Positions</div><div class="value">{len(positions)}</div></div>
      <div class="card {'pos-card' if total_closed_pl >= 0 else 'neg-card'}"><div class="label">Realized P&amp;L</div><div class="value {'pos' if total_closed_pl >= 0 else 'neg'}">{fmt_money(total_closed_pl)}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value">{win_rate_text}</div></div>
      <div class="card"><div class="label">Closed Trades</div><div class="value">{len(closed_trades)}</div></div>
    </div>

    <div class="panel">
      <h2>Daily Profit / (Loss)</h2>
      <div class="chart-wrap">
        {daily_pl_chart_html}
        <div id="plTooltip" class="pl-tooltip"></div>
      </div>
    </div>

    <div class="panel">
      <h2>Income by Day</h2>
      <div class="table-scroll">
      <table id="dailyTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('dailyTable',0,'text')">Transaction Date</th>
          <th class="sortable num" onclick="sortTable('dailyTable',1,'num')">Total # of Trades</th>
          <th class="sortable num" onclick="sortTable('dailyTable',2,'num')">Total Cost Basis</th>
          <th class="sortable num" onclick="sortTable('dailyTable',3,'num')">Total Sold Cost</th>
          <th class="sortable num" onclick="sortTable('dailyTable',4,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('dailyTable',5,'num')">Gain %</th>
        </tr></thead>
        <tbody>{daily_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Current Open Orders</h2>
      <div class="table-scroll">
      <table id="openOrdersTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('openOrdersTable',0,'text')">Submitted</th>
          <th class="sortable" onclick="sortTable('openOrdersTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('openOrdersTable',2,'text')">Company Name</th>
          <th class="sortable" onclick="sortTable('openOrdersTable',3,'text')">Side</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',4,'num')"># of Shares</th>
          <th class="sortable" onclick="sortTable('openOrdersTable',5,'text')">Status</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',6,'num')">Current Stock Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',7,'num')">Current Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',8,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',9,'num')">Current - Purchase Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',10,'num')">Limit Order Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',11,'num')">Limit - Purchase Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',12,'num')">Projected Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',13,'num')">Projected % Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',14,'num')">Stop Sell Price</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',15,'num')">Projected Stop Loss</th>
          <th class="sortable num" onclick="sortTable('openOrdersTable',16,'num')">Projected % Stop Loss</th>
        </tr></thead>
        <tbody>{open_orders_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Open Positions</h2>
      <div class="table-scroll">
      <table id="positionsTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('positionsTable',0,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('positionsTable',1,'text')">Company Name</th>
          <th class="sortable num" onclick="sortTable('positionsTable',2,'num')"># of Shares</th>
          <th class="sortable num" onclick="sortTable('positionsTable',3,'num')">Last Price</th>
          <th class="sortable num" onclick="sortTable('positionsTable',4,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('positionsTable',5,'num')">Cost Basis</th>
          <th class="sortable num" onclick="sortTable('positionsTable',6,'num')">Mkt Value</th>
          <th class="sortable num" onclick="sortTable('positionsTable',7,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('positionsTable',8,'num')">Gain %</th>
          <th class="sortable num" onclick="sortTable('positionsTable',9,'num')">Daily Price Change</th>
          <th class="sortable num" onclick="sortTable('positionsTable',10,'num')">Daily % Change</th>
          <th class="sortable num" onclick="sortTable('positionsTable',11,'num')">Today's Change</th>
        </tr></thead>
        <tbody>{positions_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>Recent Orders (last 50)</h2>
        <input type="search" id="ordersSearch" class="symbol-search" placeholder="Search symbols or dates, e.g. AAPL, 2026-09-28"
               aria-label="Filter recent orders by stock symbol or submitted date" autocomplete="off" spellcheck="false" oninput="filterOrders()">
      </div>
      <div class="filters">{status_filters_html}</div>
      <div class="table-scroll">
      <table id="ordersTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('ordersTable',0,'text')">Submitted</th>
          <th class="sortable" onclick="sortTable('ordersTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('ordersTable',2,'text')">Company Name</th>
          <th class="sortable" onclick="sortTable('ordersTable',3,'text')">Side</th>
          <th class="sortable num" onclick="sortTable('ordersTable',4,'num')"># of Shares</th>
          <th class="sortable" onclick="sortTable('ordersTable',5,'text')">Status</th>
          <th class="sortable num" onclick="sortTable('ordersTable',6,'num')">Last Price</th>
          <th class="sortable num" onclick="sortTable('ordersTable',7,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('ordersTable',8,'num')">Cost Basis</th>
          <th class="sortable num" onclick="sortTable('ordersTable',9,'num')">Mkt Value</th>
          <th class="sortable num" onclick="sortTable('ordersTable',10,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('ordersTable',11,'num')">Gain %</th>
          <th class="sortable num" onclick="sortTable('ordersTable',12,'num')">Daily Price Change</th>
          <th class="sortable num" onclick="sortTable('ordersTable',13,'num')">Daily % Change</th>
          <th class="sortable num" onclick="sortTable('ordersTable',14,'num')">Today's Change</th>
        </tr></thead>
        <tbody>{orders_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>Closed Orders</h2>
        <input type="search" id="closedSearch" class="symbol-search" placeholder="Search symbols or dates, e.g. AAPL, 2026-09-28"
               aria-label="Filter closed orders by stock symbol or transaction date" autocomplete="off" spellcheck="false" oninput="filterClosed()">
      </div>
      <div class="table-scroll">
      <table id="closedTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('closedTable',0,'text')">Transaction Date</th>
          <th class="sortable" onclick="sortTable('closedTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('closedTable',2,'text')">Company Name</th>
          <th class="sortable num" onclick="sortTable('closedTable',3,'num')"># of Shares</th>
          <th class="sortable num" onclick="sortTable('closedTable',4,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('closedTable',5,'num')">Sell Price</th>
          <th class="sortable num" onclick="sortTable('closedTable',6,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('closedTable',7,'num')">% Gain / Loss</th>
        </tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>AI Trade Filter Decisions</h2>
        <span class="muted ai-summary">Mode: <strong>{ai_mode_now}</strong> &middot; Today: {ai_confirmed_today} confirmed, {ai_vetoed_today} vetoed</span>
      </div>
      <div class="table-scroll">
      <table id="aiTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('aiTable',0,'text')">Time</th>
          <th class="sortable" onclick="sortTable('aiTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('aiTable',2,'text')">Side</th>
          <th class="sortable num" onclick="sortTable('aiTable',3,'num')">Entry</th>
          <th class="sortable" onclick="sortTable('aiTable',4,'text')">Decision</th>
          <th class="sortable num" onclick="sortTable('aiTable',5,'num')">Confidence</th>
          <th class="sortable num" onclick="sortTable('aiTable',6,'num')">Size</th>
          <th class="sortable num" onclick="sortTable('aiTable',7,'num')">Target</th>
          <th class="sortable" onclick="sortTable('aiTable',8,'text')">Source</th>
          <th class="sortable num" onclick="sortTable('aiTable',9,'num')">Realized P&amp;L (day)</th>
          <th>Reasoning</th>
        </tr></thead>
        <tbody>{ai_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="disclaimer">
      This is a PAPER TRADING account — no real money is involved. This dashboard
      is regenerated automatically after each trading session and reflects data
      from Alpaca's paper trading API only.
    </div>
  </div>

  <script>
    function sortTable(tableId, colIdx, type) {{
      const table = document.getElementById(tableId);
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.querySelectorAll('tr:not(.totals-row):not(.no-match)'));
      if (rows.length < 2) return;
      const headerRow = table.tHead.rows[0];
      const th = headerRow.cells[colIdx];
      const asc = th.getAttribute('data-dir') !== 'asc';
      Array.from(headerRow.cells).forEach(c => c.removeAttribute('data-dir'));
      th.setAttribute('data-dir', asc ? 'asc' : 'desc');

      rows.sort((a, b) => {{
        const aCell = a.cells[colIdx], bCell = b.cells[colIdx];
        const av = aCell.getAttribute('data-value') ?? aCell.textContent.trim();
        const bv = bCell.getAttribute('data-value') ?? bCell.textContent.trim();
        let cmp;
        if (type === 'num') {{
          const an = av === '' ? -Infinity : parseFloat(av);
          const bn = bv === '' ? -Infinity : parseFloat(bv);
          cmp = an - bn;
        }} else {{
          cmp = av.localeCompare(bv);
        }}
        return asc ? cmp : -cmp;
      }});

      rows.forEach(r => tbody.appendChild(r));
      const noMatchRow = tbody.querySelector('tr.no-match');
      if (noMatchRow) tbody.appendChild(noMatchRow);
      // keep the totals row (if any) pinned at the bottom
      const totalsRow = tbody.querySelector('tr.totals-row');
      if (totalsRow) tbody.appendChild(totalsRow);
    }}

    // Search boxes take a comma/space separated list of stock symbols and/or
    // dates, case-insensitive. Symbols match the Symbol column exactly; dates
    // match the first column (Submitted / Transaction Date) in Eastern time.
    // Accepted date forms: 2026-09-28, 2026-09 (whole month), 2026 (whole
    // year), 09-28 or 9/28 (that day, any year), 9/28/2026. A row must match
    // one of the symbols (if any were typed) AND one of the dates (if any).
    function parseSearch(inputId) {{
      const el = document.getElementById(inputId);
      const out = {{ symbols: [], dates: [] }};
      if (!el) return out;
      el.value.split(/[\\s,]+/).map(x => x.trim()).filter(Boolean).forEach(tok => {{
        let m;
        if ((m = tok.match(/^(\\d{{4}})[-\\/](\\d{{1,2}})(?:[-\\/](\\d{{1,2}}))?$/))) {{
          out.dates.push({{ y: +m[1], m: +m[2], d: m[3] ? +m[3] : null }});
        }} else if ((m = tok.match(/^(\\d{{1,2}})[-\\/](\\d{{1,2}})[-\\/](\\d{{4}})$/))) {{
          out.dates.push({{ y: +m[3], m: +m[1], d: +m[2] }});
        }} else if ((m = tok.match(/^(\\d{{1,2}})[-\\/](\\d{{1,2}})$/))) {{
          out.dates.push({{ y: null, m: +m[1], d: +m[2] }});
        }} else if (/^\\d{{4}}$/.test(tok)) {{
          out.dates.push({{ y: +tok, m: null, d: null }});
        }} else {{
          out.symbols.push(tok.toUpperCase());
        }}
      }});
      return out;
    }}
    function rowSymbol(row) {{
      const cell = row.cells[1];
      return cell ? (cell.getAttribute('data-value') || cell.textContent.trim()).toUpperCase() : '';
    }}
    // Row's date (Eastern time) as [y, m, d]. data-value is an ISO timestamp:
    // with an ET offset when generated by the bot, in UTC after a live Refresh.
    function rowDate(row) {{
      const cell = row.cells[0];
      const raw = cell ? cell.getAttribute('data-value') : null;
      if (!raw) return null;
      const dt = new Date(raw);
      if (isNaN(dt)) return null;
      const iso = dt.toLocaleDateString('en-CA', {{ timeZone: 'America/New_York' }});  // YYYY-MM-DD
      return iso.split('-').map(Number);
    }}
    function rowMatches(row, q) {{
      if (q.symbols.length && !q.symbols.includes(rowSymbol(row))) return false;
      if (q.dates.length) {{
        const rd = rowDate(row);
        if (!rd) return false;
        const hit = q.dates.some(t => (t.y === null || t.y === rd[0]) && (t.m === null || t.m === rd[1]) && (t.d === null || t.d === rd[2]));
        if (!hit) return false;
      }}
      return true;
    }}
    function describeQuery(q) {{
      return q.symbols.concat(q.dates.length ? ['the entered date(s)'] : []).join(', ');
    }}
    function setNoMatch(tableId, show, message) {{
      const table = document.getElementById(tableId);
      const tbody = table.tBodies[0];
      const existing = tbody.querySelector('tr.no-match');
      if (existing) existing.remove();
      if (!show) return;
      const tr = document.createElement('tr');
      tr.className = 'no-match';
      const td = document.createElement('td');
      td.colSpan = table.tHead.rows[0].cells.length;
      td.textContent = message;
      tr.appendChild(td);
      const totals = tbody.querySelector('tr.totals-row');
      tbody.insertBefore(tr, totals);
    }}

    // Recent Orders: status checkboxes AND symbol/date search must all match.
    // A Total row under Profit / (Loss) (column index 10) sums the rows shown.
    function filterOrders() {{
      const checked = Array.from(document.querySelectorAll('.status-filter:checked')).map(c => c.value);
      const q = parseSearch('ordersSearch');
      const table = document.getElementById('ordersTable');
      const tbody = table.tBodies[0];
      const nCols = table.tHead.rows[0].cells.length;
      const plCol = 10;
      let total = 0, shown = 0, plSum = 0, plCount = 0;
      tbody.querySelectorAll('tr[data-status]').forEach(row => {{
        total++;
        const ok = checked.includes(row.getAttribute('data-status')) && rowMatches(row, q);
        row.style.display = ok ? '' : 'none';
        if (!ok) return;
        shown++;
        const v = parseFloat(row.cells[plCol].getAttribute('data-value'));
        if (!isNaN(v)) {{ plSum += v; plCount++; }}
      }});
      const hasQuery = q.symbols.length > 0 || q.dates.length > 0;
      setNoMatch('ordersTable', total > 0 && shown === 0,
        hasQuery ? 'No orders for ' + describeQuery(q) + ' with the selected statuses.' : 'No orders match the selected statuses.');

      let totals = tbody.querySelector('tr.totals-row');
      if (total === 0 || shown === 0) {{ if (totals) totals.remove(); return; }}
      if (!totals) {{
        totals = document.createElement('tr');
        totals.className = 'totals-row';
        for (let i = 0; i < nCols; i++) totals.appendChild(document.createElement('td'));
      }}
      totals.cells[0].textContent = 'Total';
      const plCell = totals.cells[plCol];
      plCell.textContent = plCount ? fmtMoneyJS(plSum) : '—';
      plCell.className = 'num ' + (plCount ? (plSum >= 0 ? 'pos' : 'neg') : '');
      tbody.appendChild(totals);
    }}

    // Closed Orders: symbol/date search; the Total row (incl. Profit / (Loss))
    // is recomputed for whatever is visible and restored when cleared.
    let closedTotalsOriginal = null;
    function filterClosed() {{
      const table = document.getElementById('closedTable');
      const tbody = table.tBodies[0];
      const totals = tbody.querySelector('tr.totals-row');
      if (totals && closedTotalsOriginal === null) closedTotalsOriginal = totals.innerHTML;
      const q = parseSearch('closedSearch');
      const hasQuery = q.symbols.length > 0 || q.dates.length > 0;
      const rows = Array.from(tbody.querySelectorAll('tr:not(.totals-row):not(.no-match)')).filter(r => r.cells.length > 2);
      let shown = 0, cost = 0, proceeds = 0, pl = 0;
      rows.forEach(row => {{
        const ok = rowMatches(row, q);
        row.style.display = ok ? '' : 'none';
        if (!ok) return;
        shown++;
        const shares = parseFloat(row.cells[3].getAttribute('data-value'));
        cost += shares * parseFloat(row.cells[4].getAttribute('data-value'));
        proceeds += shares * parseFloat(row.cells[5].getAttribute('data-value'));
        pl += parseFloat(row.cells[6].getAttribute('data-value'));
      }});
      setNoMatch('closedTable', rows.length > 0 && shown === 0, 'No closed orders for ' + describeQuery(q) + '.');
      if (!totals) return;
      if (!hasQuery) {{
        totals.style.display = '';
        if (closedTotalsOriginal !== null) totals.innerHTML = closedTotalsOriginal;
      }} else if (shown === 0) {{
        totals.style.display = 'none';
      }} else {{
        totals.style.display = '';
        const gain = cost ? pl / cost * 100 : 0;
        totals.cells[4].textContent = fmtMoneyJS(cost);
        totals.cells[5].textContent = fmtMoneyJS(proceeds);
        totals.cells[6].textContent = fmtMoneyJS(pl);
        totals.cells[6].className = 'num ' + (pl >= 0 ? 'pos' : 'neg');
        totals.cells[7].textContent = pctJS(gain);
        totals.cells[7].className = 'num ' + (gain >= 0 ? 'pos' : 'neg');
      }}
    }}

    (function() {{
      const tooltip = document.getElementById('plTooltip');
      const chartWrap = document.querySelector('.chart-wrap');
      if (!tooltip || !chartWrap) return;
      chartWrap.querySelectorAll('.pl-bar').forEach(bar => {{
        bar.addEventListener('mousemove', e => {{
          const wrapRect = chartWrap.getBoundingClientRect();
          tooltip.textContent = `${{bar.getAttribute('data-date')}}: ${{bar.getAttribute('data-pl')}}`;
          tooltip.style.left = (e.clientX - wrapRect.left) + 'px';
          tooltip.style.top = (e.clientY - wrapRect.top) + 'px';
          tooltip.style.display = 'block';
        }});
        bar.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
    }})();

    // --- Live refresh: pulls fresh account/positions/orders from a small
    // read-only proxy (holds the Alpaca keys server-side) and re-renders the
    // Open Positions / Recent Orders tables + header cards in place. Only
    // active if a proxy URL was configured when this page was generated;
    // Closed Orders / Income by Day / the P&L chart are NOT updated here —
    // those only change once a position actually closes, which requires a
    // real bot session, so they still only refresh via the normal pipeline.
    const REFRESH_ENDPOINT = {REFRESH_ENDPOINT!r};
    const REFRESH_TOKEN = {REFRESH_TOKEN!r};

    // --- Settings panel: view/edit config.py's tunable parameters and save
    // them to settings.json in the repo (via a PHP proxy that commits
    // through the GitHub API), not to this browser — so every browser sees
    // the same values. Reading is done straight from the public repo (no
    // proxy needed); INITIAL_SETTINGS is the snapshot as of when this page
    // was last generated, used as an instant fallback if the live re-fetch
    // on open fails.
    const SETTINGS_SAVE_ENDPOINT = {SETTINGS_SAVE_ENDPOINT!r};
    const SETTINGS_SOURCE_URL = {SETTINGS_SOURCE_URL!r};
    const SETTINGS_FIELDS = {settings_fields_json};
    const INITIAL_SETTINGS = {current_settings_json};

    const SETTINGS_API_URL = 'https://api.github.com/repos/patrickobirmingham-eng/POB-CL-DT/contents/settings.json';
    const SAVED_KEY = 'orb-dashboard-last-saved-settings';
    function rememberSavedSettings(values) {{
      try {{ localStorage.setItem(SAVED_KEY, JSON.stringify({{ at: Date.now(), values: values }})); }} catch (e) {{ /* ignore */ }}
    }}
    function recentlySavedSettings() {{
      try {{
        const rec = JSON.parse(localStorage.getItem(SAVED_KEY) || 'null');
        if (rec && Date.now() - rec.at < 10 * 60 * 1000) return rec.values || {{}};
      }} catch (e) {{ /* ignore */ }}
      return {{}};
    }}

    function formatFieldValue(field, raw) {{
      if (field.type === 'percent') return raw == null ? '' : (Number(raw) * 100);
      if (field.type === 'watchlist') return Array.isArray(raw) ? raw.join(', ') : (raw || '');
      if (field.type === 'nullable_number') return raw == null ? '' : raw;
      return raw;
    }}

    function parseFieldValue(field, el) {{
      if (field.type === 'bool') return el.checked;
      if (field.type === 'watchlist') {{
        return el.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
      }}
      if (field.type === 'text' || field.type === 'time' || field.type === 'select') return el.value;
      if (field.type === 'nullable_number') {{
        const t = el.value.trim();
        return t === '' ? null : Number(t);
      }}
      if (field.type === 'percent') {{
        const t = el.value.trim();
        return t === '' ? null : Number(t) / 100;
      }}
      return Number(el.value);
    }}

    function renderSettingsForm(values) {{
      const order = [];
      const byGroup = {{}};
      SETTINGS_FIELDS.forEach(f => {{
        if (!byGroup[f.group]) {{ byGroup[f.group] = []; order.push(f.group); }}
        byGroup[f.group].push(f);
      }});
      let html = '';
      order.forEach(g => {{
        html += '<div class="settings-group"><h3>' + g + '</h3>';
        byGroup[g].forEach(f => {{
          const val = formatFieldValue(f, values[f.key]);
          const hint = f.hint ? ('<span class="settings-hint">' + f.hint + '</span>') : '';
          const tipText = f.tooltip ? String(f.tooltip).replace(/"/g, '&quot;') : '';
          const info = tipText ? ('<span class="settings-info" title="' + tipText + '">ⓘ</span>') : '';
          const id = 'set_' + f.key;
          let control;
          if (f.type === 'bool') {{
            control = '<input type="checkbox" id="' + id + '" ' + (val ? 'checked' : '') + '>';
          }} else if (f.type === 'select') {{
            const opts = (f.options || []).map(o =>
              '<option value="' + o + '" ' + (o === val ? 'selected' : '') + '>' + o + '</option>'
            ).join('');
            control = '<select id="' + id + '">' + opts + '</select>';
          }} else if (f.type === 'time') {{
            control = '<input type="time" id="' + id + '" value="' + (val || '') + '">';
          }} else if (f.type === 'watchlist') {{
            control = '<textarea id="' + id + '">' + (val || '') + '</textarea>';
          }} else if (f.type === 'text') {{
            const safe = (val ?? '').toString().replace(/"/g, '&quot;');
            control = '<input type="text" id="' + id + '" value="' + safe + '">';
          }} else {{
            const step = f.step != null ? f.step : 'any';
            const min = f.min != null ? (' min="' + f.min + '"') : '';
            control = '<input type="number" step="' + step + '"' + min + ' id="' + id + '" value="' + (val ?? '') + '">';
          }}
          const prefix = f.prefix ? ('<span class="suffix">' + f.prefix + '</span>') : '';
          const suffix = f.suffix ? ('<span class="suffix">' + f.suffix + '</span>') : '';
          if (f.type === 'watchlist') {{
            html += '<div class="settings-row" style="flex-direction:column; align-items:stretch;">'
              + '<label for="' + id + '">' + f.label + info + hint + '</label>' + control + '</div>';
          }} else {{
            html += '<div class="settings-row">'
              + '<label for="' + id + '">' + f.label + info + hint + '</label>'
              + '<div class="settings-control">' + prefix + control + suffix + '</div></div>';
          }}
        }});
        html += '</div>';
      }});
      document.getElementById('settingsBody').innerHTML = html;
    }}

    function collectSettingsValues() {{
      const out = {{}};
      SETTINGS_FIELDS.forEach(f => {{
        const el = document.getElementById('set_' + f.key);
        if (!el) return;
        out[f.key] = parseFieldValue(f, el);
      }});
      return out;
    }}

    async function openSettingsModal() {{
      document.getElementById('settingsModal').style.display = 'flex';
      const statusEl = document.getElementById('settingsStatus');
      statusEl.textContent = '';
      statusEl.className = 'settings-status';
      document.getElementById('settingsBody').innerHTML = '<div class="settings-loading">Loading current settings…</div>';
      let values = INITIAL_SETTINGS;
      // raw.githubusercontent.com is CDN-cached for a few minutes, so right
      // after a save it can still return the OLD file. The GitHub contents
      // API is much fresher; try it first, then the raw URL as a fallback.
      let fresh = null;
      for (const [url, headers] of [
        [SETTINGS_API_URL + '?ref=main&_=' + Date.now(), {{ 'Accept': 'application/vnd.github.raw+json' }}],
        [SETTINGS_SOURCE_URL + '?_=' + Date.now(), {{}}],
      ]) {{
        try {{
          const resp = await fetch(url, {{ headers: headers, cache: 'no-store' }});
          if (resp.ok) {{ fresh = await resp.json(); break; }}
        }} catch (e) {{ /* try next source */ }}
      }}
      if (fresh) values = Object.assign({{}}, INITIAL_SETTINGS, fresh);
      // If this browser saved within the last 10 minutes, those values win
      // over anything fetched (which may still be a stale cached copy).
      values = Object.assign({{}}, values, recentlySavedSettings());
      renderSettingsForm(values);
    }}

    function closeSettingsModal() {{
      document.getElementById('settingsModal').style.display = 'none';
    }}

    async function saveSettings() {{
      const statusEl = document.getElementById('settingsStatus');
      const btn = document.getElementById('settingsSaveBtn');
      const tokenEl = document.getElementById('settingsToken');
      const token = tokenEl.value;
      if (!token) {{
        statusEl.textContent = 'Enter the passcode to save.';
        statusEl.className = 'settings-status err';
        return;
      }}
      const values = collectSettingsValues();
      btn.disabled = true;
      statusEl.textContent = 'Saving…';
      statusEl.className = 'settings-status info';
      try {{
        const resp = await fetch(SETTINGS_SAVE_ENDPOINT, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ token: token, settings: values }}),
        }});
        const data = await resp.json().catch(() => ({{}}));
        if (!resp.ok || !data.ok) throw new Error(data.error || ('HTTP ' + resp.status));
        rememberSavedSettings(values);
        statusEl.textContent = 'Saved. The bot will pick this up on its next run/poll.';
        statusEl.className = 'settings-status ok';
        tokenEl.value = '';
      }} catch (e) {{
        statusEl.textContent = 'Save failed: ' + e.message;
        statusEl.className = 'settings-status err';
      }} finally {{
        btn.disabled = false;
      }}
    }}

    function fmtMoneyJS(v) {{
      if (v === null || v === undefined || isNaN(v)) return '—';
      const n = Number(v);
      const sign = n < 0 ? '-' : '';
      return sign + '$' + Math.abs(n).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}
    function pctJS(v) {{
      if (v === null || v === undefined || isNaN(v)) return '—';
      return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
    }}
    function cls(v) {{ return (v === null || v === undefined || isNaN(v)) ? '' : (v >= 0 ? 'pos' : 'neg'); }}

    function snapshotPrices(snapshots, symbol) {{
      const snap = snapshots ? snapshots[symbol] : null;
      if (!snap) return [null, null];
      let current = null;
      if (snap.latestTrade && snap.latestTrade.p != null) current = Number(snap.latestTrade.p);
      else if (snap.dailyBar && snap.dailyBar.c != null) current = Number(snap.dailyBar.c);
      let lastday = null;
      if (snap.prevDailyBar && snap.prevDailyBar.c != null) lastday = Number(snap.prevDailyBar.c);
      return [current, lastday];
    }}

    function buildPositionsRows(positions, names) {{
      if (!positions || positions.length === 0) {{
        return "<tr><td colspan='12' class='muted'>No open positions</td></tr>";
      }}
      let rows = '';
      let totalCost = 0, totalMkt = 0, totalPl = 0, totalToday = 0;
      positions.forEach(p => {{
        const qty = Number(p.qty);
        const current = Number(p.current_price);
        const avgEntry = Number(p.avg_entry_price);
        const costBasis = Number(p.cost_basis);
        const mktValue = Number(p.market_value);
        const pl = Number(p.unrealized_pl);
        const gainPct = Number(p.unrealized_plpc) * 100;
        const lastday = p.lastday_price != null ? Number(p.lastday_price) : current;
        const dailyChange = current - lastday;
        const dailyPct = p.change_today != null ? Number(p.change_today) * 100
          : (lastday ? (dailyChange / lastday * 100) : 0);
        const todaysChange = qty * dailyChange;
        totalCost += costBasis; totalMkt += mktValue; totalPl += pl; totalToday += todaysChange;
        const name = (names && names[p.symbol]) || p.symbol;
        rows += `<tr>
          <td data-value="${{p.symbol}}">${{p.symbol}}</td>
          <td data-value="${{name}}">${{name}}</td>
          <td class="num" data-value="${{qty}}">${{p.qty}}</td>
          <td class="num" data-value="${{current}}">${{fmtMoneyJS(current)}}</td>
          <td class="num" data-value="${{avgEntry}}">${{fmtMoneyJS(avgEntry)}}</td>
          <td class="num" data-value="${{costBasis}}">${{fmtMoneyJS(costBasis)}}</td>
          <td class="num" data-value="${{mktValue}}">${{fmtMoneyJS(mktValue)}}</td>
          <td class="num ${{cls(pl)}}" data-value="${{pl}}">${{fmtMoneyJS(pl)}}</td>
          <td class="num ${{cls(gainPct)}}" data-value="${{gainPct}}">${{pctJS(gainPct)}}</td>
          <td class="num ${{cls(dailyChange)}}" data-value="${{dailyChange}}">${{fmtMoneyJS(dailyChange)}}</td>
          <td class="num ${{cls(dailyPct)}}" data-value="${{dailyPct}}">${{pctJS(dailyPct)}}</td>
          <td class="num ${{cls(todaysChange)}}" data-value="${{todaysChange}}">${{fmtMoneyJS(todaysChange)}}</td>
        </tr>`;
      }});
      rows += `<tr class="totals-row">
        <td>Total</td><td></td><td></td><td></td><td></td>
        <td class="num">${{fmtMoneyJS(totalCost)}}</td>
        <td class="num">${{fmtMoneyJS(totalMkt)}}</td>
        <td class="num ${{cls(totalPl)}}">${{fmtMoneyJS(totalPl)}}</td>
        <td></td><td></td><td></td>
        <td class="num ${{cls(totalToday)}}">${{fmtMoneyJS(totalToday)}}</td>
      </tr>`;
      return rows;
    }}

    const TERMINAL_ORDER_STATUSES = new Set(['filled', 'canceled', 'expired', 'rejected', 'done_for_day', 'replaced']);

    function buildOpenOrdersRows(orders, names, positions, snapshots) {{
      const openOrders = (orders || []).filter(o => !TERMINAL_ORDER_STATUSES.has(o.status));
      if (openOrders.length === 0) {{
        return "<tr><td colspan='17' class='muted'>No open orders</td></tr>";
      }}
      const positionsBySymbol = {{}};
      (positions || []).forEach(p => {{ positionsBySymbol[p.symbol] = p; }});
      let rows = '';
      let totalPl = 0, totalCostBasis = 0, totalStopPl = 0, totalStopCostBasis = 0, totalCurrentMktValue = 0;
      openOrders.forEach(o => {{
        const submittedDt = new Date(o.submitted_at);
        const submitted = submittedDt.toLocaleString('en-US', {{
          timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        }});
        const side = o.side || '—';
        const status = o.status || '—';
        const qty = o.qty != null ? Number(o.qty) : 0;
        const limitPrice = o.limit_price != null ? Number(o.limit_price) : null;
        const stopPrice = o.stop_price != null ? Number(o.stop_price) : null;
        const [currentPrice] = snapshotPrices(snapshots, o.symbol);
        const pos = positionsBySymbol[o.symbol];
        const purchasePrice = pos ? Number(pos.avg_entry_price) : null;
        const deltaPrice = (purchasePrice != null && currentPrice != null) ? (currentPrice - purchasePrice) : null;
        const currentMktValue = deltaPrice != null ? (qty * deltaPrice) : null;
        if (currentMktValue != null) {{ totalCurrentMktValue += currentMktValue; }}
        const limitMinusPurchase = (limitPrice != null && purchasePrice != null) ? (limitPrice - purchasePrice) : null;

        let projectedPl = null, projectedPct = null;
        if (limitPrice != null && purchasePrice != null) {{
          const perShare = side === 'sell' ? (limitPrice - purchasePrice) : (purchasePrice - limitPrice);
          projectedPl = qty * perShare;
          projectedPct = purchasePrice ? (perShare / purchasePrice * 100) : null;
          totalPl += projectedPl;
          totalCostBasis += qty * purchasePrice;
        }}

        let projectedStopPl = null, projectedStopPct = null;
        if (stopPrice != null && purchasePrice != null) {{
          const stopPerShare = side === 'sell' ? (stopPrice - purchasePrice) : (purchasePrice - stopPrice);
          projectedStopPl = qty * stopPerShare;
          projectedStopPct = purchasePrice ? (stopPerShare / purchasePrice * 100) : null;
          totalStopPl += projectedStopPl;
          totalStopCostBasis += qty * purchasePrice;
        }}

        const name = (names && names[o.symbol]) || o.symbol;
        rows += `<tr>
          <td data-value="${{submittedDt.toISOString()}}">${{submitted}}</td>
          <td data-value="${{o.symbol}}">${{o.symbol}}</td>
          <td data-value="${{name}}">${{name}}</td>
          <td class="${{side === 'buy' ? 'pos' : 'neg'}}" data-value="${{side}}">${{side.toUpperCase()}}</td>
          <td class="num" data-value="${{qty}}">${{o.qty}}</td>
          <td data-value="${{status}}">${{status}}</td>
          <td class="num" data-value="${{currentPrice ?? ''}}">${{currentPrice != null ? fmtMoneyJS(currentPrice) : '—'}}</td>
          <td class="num" data-value="${{currentMktValue ?? ''}}">${{currentMktValue != null ? fmtMoneyJS(currentMktValue) : '—'}}</td>
          <td class="num" data-value="${{purchasePrice ?? ''}}">${{purchasePrice != null ? fmtMoneyJS(purchasePrice) : '—'}}</td>
          <td class="num ${{cls(deltaPrice)}}" data-value="${{deltaPrice ?? ''}}">${{deltaPrice != null ? fmtMoneyJS(deltaPrice) : '—'}}</td>
          <td class="num" data-value="${{limitPrice ?? ''}}">${{limitPrice != null ? fmtMoneyJS(limitPrice) : '—'}}</td>
          <td class="num ${{cls(limitMinusPurchase)}}" data-value="${{limitMinusPurchase ?? ''}}">${{limitMinusPurchase != null ? fmtMoneyJS(limitMinusPurchase) : '—'}}</td>
          <td class="num ${{cls(projectedPl)}}" data-value="${{projectedPl ?? ''}}">${{projectedPl != null ? fmtMoneyJS(projectedPl) : '—'}}</td>
          <td class="num ${{cls(projectedPct)}}" data-value="${{projectedPct ?? ''}}">${{projectedPct != null ? pctJS(projectedPct) : '—'}}</td>
          <td class="num" data-value="${{stopPrice ?? ''}}">${{stopPrice != null ? fmtMoneyJS(stopPrice) : '—'}}</td>
          <td class="num ${{cls(projectedStopPl)}}" data-value="${{projectedStopPl ?? ''}}">${{projectedStopPl != null ? fmtMoneyJS(projectedStopPl) : '—'}}</td>
          <td class="num ${{cls(projectedStopPct)}}" data-value="${{projectedStopPct ?? ''}}">${{projectedStopPct != null ? pctJS(projectedStopPct) : '—'}}</td>
        </tr>`;
      }});

      const totalPct = totalCostBasis ? (totalPl / totalCostBasis * 100) : null;
      const totalStopPct = totalStopCostBasis ? (totalStopPl / totalStopCostBasis * 100) : null;
      rows += `<tr class="totals-row">
        <td>Total</td><td></td><td></td><td></td><td></td><td></td><td></td>
        <td class="num ${{cls(totalCurrentMktValue)}}">${{fmtMoneyJS(totalCurrentMktValue)}}</td>
        <td></td><td></td><td></td><td></td>
        <td class="num ${{cls(totalPl)}}">${{fmtMoneyJS(totalPl)}}</td>
        <td class="num ${{cls(totalPct)}}">${{totalPct != null ? pctJS(totalPct) : '—'}}</td>
        <td></td>
        <td class="num ${{cls(totalStopPl)}}">${{fmtMoneyJS(totalStopPl)}}</td>
        <td class="num ${{cls(totalStopPct)}}">${{totalStopPct != null ? pctJS(totalStopPct) : '—'}}</td>
      </tr>`;
      return rows;
    }}

    function buildOrdersRows(orders, names, snapshots) {{
      if (!orders || orders.length === 0) {{
        return "<tr><td colspan='15' class='muted'>No orders yet</td></tr>";
      }}
      let rows = '';
      orders.forEach(o => {{
        const submittedDt = new Date(o.submitted_at);
        const submitted = submittedDt.toLocaleString('en-US', {{
          timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        }});
        const side = o.side || '—';
        const status = o.status || '—';
        const filledPrice = o.filled_avg_price != null ? Number(o.filled_avg_price) : null;
        const qty = o.qty != null ? Number(o.qty) : 0;
        const [current, lastday] = snapshotPrices(snapshots, o.symbol);
        const costBasis = filledPrice != null ? qty * filledPrice : null;
        const mktValue = current != null ? qty * current : null;
        const pl = (mktValue != null && costBasis != null) ? (mktValue - costBasis) : null;
        const gainPct = (pl != null && costBasis) ? (pl / costBasis * 100) : null;
        const dailyChange = (current != null && lastday != null) ? (current - lastday) : null;
        const dailyPct = (dailyChange != null && lastday) ? (dailyChange / lastday * 100) : null;
        const todaysChange = dailyChange != null ? qty * dailyChange : null;
        const name = (names && names[o.symbol]) || o.symbol;
        rows += `<tr data-status="${{status}}">
          <td data-value="${{submittedDt.toISOString()}}">${{submitted}}</td>
          <td data-value="${{o.symbol}}">${{o.symbol}}</td>
          <td data-value="${{name}}">${{name}}</td>
          <td class="${{side === 'buy' ? 'pos' : 'neg'}}" data-value="${{side}}">${{side.toUpperCase()}}</td>
          <td class="num" data-value="${{qty}}">${{o.qty}}</td>
          <td data-value="${{status}}">${{status}}</td>
          <td class="num" data-value="${{current ?? ''}}">${{current != null ? fmtMoneyJS(current) : '—'}}</td>
          <td class="num" data-value="${{filledPrice ?? ''}}">${{filledPrice != null ? fmtMoneyJS(filledPrice) : '—'}}</td>
          <td class="num" data-value="${{costBasis ?? ''}}">${{costBasis != null ? fmtMoneyJS(costBasis) : '—'}}</td>
          <td class="num" data-value="${{mktValue ?? ''}}">${{mktValue != null ? fmtMoneyJS(mktValue) : '—'}}</td>
          <td class="num ${{cls(pl)}}" data-value="${{pl ?? ''}}">${{pl != null ? fmtMoneyJS(pl) : '—'}}</td>
          <td class="num ${{cls(gainPct)}}" data-value="${{gainPct ?? ''}}">${{gainPct != null ? pctJS(gainPct) : '—'}}</td>
          <td class="num ${{cls(dailyChange)}}" data-value="${{dailyChange ?? ''}}">${{dailyChange != null ? fmtMoneyJS(dailyChange) : '—'}}</td>
          <td class="num ${{cls(dailyPct)}}" data-value="${{dailyPct ?? ''}}">${{dailyPct != null ? pctJS(dailyPct) : '—'}}</td>
          <td class="num ${{cls(todaysChange)}}" data-value="${{todaysChange ?? ''}}">${{todaysChange != null ? fmtMoneyJS(todaysChange) : '—'}}</td>
        </tr>`;
      }});
      return rows;
    }}

    async function refreshDashboard() {{
      if (!REFRESH_ENDPOINT) return;
      const btn = document.getElementById('refreshBtn');
      const statusEl = document.getElementById('refreshStatus');
      btn.disabled = true;
      statusEl.textContent = ' Refreshing…';
      try {{
        const url = REFRESH_ENDPOINT + '?token=' + encodeURIComponent(REFRESH_TOKEN) + '&_=' + Date.now();
        const resp = await fetch(url);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));

        document.querySelector('.card:nth-child(1) .value').textContent = fmtMoneyJS(data.account.equity);
        document.querySelector('.card:nth-child(2) .value').textContent = fmtMoneyJS(data.account.cash);
        document.querySelector('.card:nth-child(3) .value').textContent = fmtMoneyJS(data.account.buying_power);
        document.querySelector('.card:nth-child(4) .value').textContent = (data.positions || []).length;

        document.querySelector('#openOrdersTable tbody').innerHTML = buildOpenOrdersRows(data.orders, data.names, data.positions, data.snapshots);
        document.querySelector('#positionsTable tbody').innerHTML = buildPositionsRows(data.positions, data.names);
        document.querySelector('#ordersTable tbody').innerHTML = buildOrdersRows(data.orders, data.names, data.snapshots);
        filterOrders();

        const now = new Date().toLocaleString('en-US', {{
          timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        }});
        document.getElementById('lastUpdated').textContent = now + ' ET (live refresh — Closed Orders/Income by Day still reflect the last full session)';
        statusEl.textContent = '';
      }} catch (e) {{
        statusEl.textContent = ' Refresh failed: ' + e.message;
      }} finally {{
        btn.disabled = false;
      }}
    }}

    // Apply the default status filter (Filled only, checked above) on first
    // load, same as if the user had just toggled the checkboxes themselves.
    filterOrders();
    filterClosed();

    // --- Dark / light theme toggle ------------------------------------------------
    // Defaults to dark (matches the original look) and remembers the choice
    // per-browser via localStorage. Wrapped in try/catch since localStorage
    // can throw in some browser contexts (private mode, blocked storage).
    function getStoredTheme() {{
      try {{ return localStorage.getItem('orb-dashboard-theme'); }} catch (e) {{ return null; }}
    }}
    function storeTheme(theme) {{
      try {{ localStorage.setItem('orb-dashboard-theme', theme); }} catch (e) {{ /* ignore */ }}
    }}
    function applyTheme(theme) {{
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('themeToggleBtn');
      if (btn) btn.textContent = theme === 'light' ? '☽ Dark' : '☀ Light';
    }}
    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      const next = current === 'light' ? 'dark' : 'light';
      applyTheme(next);
      storeTheme(next);
    }}
    applyTheme(getStoredTheme() === 'light' ? 'light' : 'dark');
  </script>
</body>
</html>"""

    with open(OUTPUT_FILE, "w") as f:
        f.write(html)

    print(f"Dashboard written to {OUTPUT_FILE}")


def main():
    generate()


if __name__ == "__main__":
    main()
