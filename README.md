# Alpaca Opening-Range Breakout (ORB) Day Trading System

A rules-based, risk-managed day trading system for Alpaca's **paper trading** API, built
around the Opening Range Breakout strategy — one of the more durable, well-documented
intraday momentum approaches (it has real academic and practitioner backing, unlike most
"systems" sold online). This is a starting framework to test and iterate on, not a
guaranteed money-maker. Trade what you can afford to lose, and treat every result here as
a hypothesis until it survives months of paper trading.

## Honest framing before you use this

- **Most day traders lose money after costs.** Studies of retail day traders (e.g. Barber
  & Odean, and Brazilian/Taiwanese regulator studies) consistently find <15-20% are
  net profitable over multi-year periods, and it gets worse the more frequently someone trades.
- This system has an **edge case, not a guarantee**: ORB works because a meaningful chunk of
  institutional order flow shows up early and pushes stocks in the direction they'll trend for the day,
  often enough to be profitable with strict risk control — but it goes through losing streaks and
  regime changes like anything else.
- **Backtest first. Paper trade for at least 4-8 weeks before ever considering real money.**
  Backtest results here use historical minute bars and are still optimistic vs. live paper
  trading (no slippage modeling beyond a fixed assumption, imperfect fill simulation).
- Nothing here is financial advice. You are the one deciding position sizing, risk limits, and
  whether to ever go live.

## The strategy (Opening Range Breakout)

1. **Opening range**: for each symbol in the watchlist, record the high/low of the first
   N minutes after the open (default 15 min, i.e. 9:30–9:45 ET).
2. **Entry**: after the opening range completes, if price breaks above the range high on
   above-average volume, go long. (Short-on-breakdown is supported but off by default —
   turn on `ALLOW_SHORTS` once you're comfortable, and confirm your Alpaca account supports
   shorting in paper mode.)
3. **Stop loss**: opening range low (long) — i.e. your stop is defined by market structure,
   not an arbitrary %.
4. **Take profit**: risk-multiple based (default 2R — twice the entry-to-stop distance).
5. **Time stop**: flatten everything by 3:45 PM ET regardless of P&L. No overnight risk.
6. **Risk per trade**: default 1% of account equity, sized from stop distance.
7. **Daily circuit breaker**: stop trading for the day after a configurable max loss
   (default 3% of equity) or after a configurable number of trades (default 6).
8. **One trade per symbol per day**: no revenge-trading a symbol after a stop-out.

This is intentionally simple and mechanical — the value of a system like this is that
you can test it, measure it, and know exactly why it did what it did. Fancier doesn't
mean better; most retail strategies fail from lack of discipline, not lack of complexity.

## Files

- `config.py` — all tunable parameters (watchlist, risk %, OR length, R-multiple, hours, circuit breakers)
- `strategy.py` — pure signal logic (no I/O), used by both the backtester and the live bot
- `backtest.py` — fetches historical minute bars from Alpaca and simulates the strategy
- `live_bot.py` — connects to Alpaca **paper** trading, runs the strategy live during market hours
- `requirements.txt` — Python dependencies
- `.env.example` — where your API keys go (never commit real keys)

## Setup

1. Get free paper trading API keys: log into https://app.alpaca.markets, switch to
   "Paper Trading" in the left sidebar, and generate an API key/secret there. (Do NOT use
   live-trading keys for any of this.)
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`.
4. Edit `config.py` — at minimum review `WATCHLIST` and `RISK_PCT_PER_TRADE`.

## Backtesting

```bash
python backtest.py --days 60
```

This pulls the last 60 trading days of minute bars for your watchlist from Alpaca's
market data API and simulates every trade the strategy would have taken, printing a
performance summary (win rate, avg R, max drawdown, equity curve) and writing a CSV of
every trade to `backtest_trades.csv`.

Run this first. If the strategy doesn't show a positive expectancy (average R > 0) over
multiple market regimes (a trending period and a choppy period), don't move on to paper
trading it live yet — go back and adjust `config.py`, or accept that this particular
watchlist/timeframe isn't a fit and pick a different one.

## Running the live paper bot

```bash
python live_bot.py
```

This runs during market hours (9:30 AM – 4:00 PM ET), scans the watchlist once the
opening range completes, places bracket orders (entry + stop + target) through Alpaca's
paper API, manages the time-stop flatten, and logs everything to `trade_log.csv` and
stdout. It's designed to be left running in a terminal (or as a background process /
cron-launched job) on any machine — this sandbox included, though for continuous
unattended operation you'll want it running somewhere that stays on during market hours
(a small VPS, or your existing Bluehost setup if it supports long-running Python
processes, or just your own machine).

**Kill switch**: Ctrl+C at any time flattens no positions automatically — the bot relies
on Alpaca's own bracket order stops/targets to manage risk even if the script itself
isn't running, which is why every entry is placed as a bracket order rather than managed
purely in-process.

## Running it for free on GitHub (no computer required to stay on)

The `live_bot.py` script and its `--session-start` / `--session-end` / `--state-file`
options are built to run as a **GitHub Actions scheduled workflow** — GitHub's servers
run the bot for you, for free, and your own computer doesn't need to be on at all.

The trading day (9:30 AM–3:45 PM ET) is split into two scheduled runs because a single
GitHub Actions job is capped at 6 hours and the full session is a bit longer than that.
State (opening ranges captured, trades already taken today, circuit-breaker counters) is
saved to `state.json` at the end of the first run and committed back to the repo, so the
second run picks up right where the first left off. The workflow file is already written:
`.github/workflows/trading-bot.yml`.

### One-time setup (about 10 minutes)

1. **Create a GitHub account** if you don't have one: github.com → Sign up (free).
2. **Create a new repository**: click the "+" in the top right → "New repository" →
   name it something like `alpaca-orb-bot` → make it **Public** (this keeps your GitHub
   Actions minutes unlimited/free; your API keys are never in the code, so this is safe
   — see the note below) → Create repository.
3. **Upload this project's files** to that repo. Easiest way with no command-line git
   experience: on the new repo's page, click "uploading an existing file" and drag in
   every file and folder from the `alpaca_orb_bot` folder you downloaded (including the
   hidden `.github` folder — if your file browser hides folders starting with a dot,
   show hidden files first, or use GitHub Desktop instead, which handles this
   automatically: desktop.github.com).
4. **Add your Alpaca paper keys as GitHub Secrets** (this keeps them out of your code
   entirely — nobody looking at your public repo can see them): in your repo, go to
   Settings → Secrets and variables → Actions → "New repository secret". Add two:
   - Name: `APCA_API_KEY_ID`, Value: your paper key ID
   - Name: `APCA_API_SECRET_KEY`, Value: your paper secret key
5. **Enable the workflow**: go to the "Actions" tab in your repo. GitHub may show a
   button to enable workflows for the repo — click it. You should see "ORB Paper Trading
   Bot" listed.
6. **Test it manually**: on that workflow's page, click "Run workflow" (this uses the
   `workflow_dispatch` trigger already built into the file) to confirm it runs
   successfully before waiting for the schedule. Click into the run to watch the logs —
   this is the same output you'd see running it locally.

After that, it runs automatically every weekday on the schedule — nothing more to do.
You can check on it any time from the Actions tab, and `trade_log.csv` in your repo
will show every trade it's placed.

### Important caveats specific to running it this way

- **Daylight Saving Time**: the schedule times in the workflow file are in UTC and are
  set for when the US is on Daylight Time. Twice a year (when clocks change in March and
  November) the cron times need to shift by one hour — the workflow file has comments
  showing both versions. I can update this for you when the time comes if you'd like a
  reminder.
- **GitHub Actions doesn't guarantee the exact minute** — a scheduled run might start a
  few minutes late during high load on GitHub's infrastructure. For a strategy trading
  a 15-minute opening range this is a minor, not critical, timing slip, but worth knowing.
- If a run fails (bad API keys, Alpaca outage, etc.), GitHub will email the account that
  owns the repo — check that inbox periodically, especially in the first few weeks.

## Suggested next steps once you've paper traded this for a while

- Track results in the same style as your Schwab options tracker — dashboard of win
  rate, R-multiples, and equity curve over time, so you're evaluating this with the same
  rigor as your options portfolio.
- Consider walk-forward testing (re-optimize on a rolling window, test out-of-sample) rather
  than a single fixed backtest window, to guard against overfitting.
- Only after a genuinely long, honest paper-trading track record (months, multiple market
  conditions) should real capital — and even then, sized small — be considered.
