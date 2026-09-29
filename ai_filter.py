"""
AI trade filter: asks Claude to CONFIRM or VETO each opening-range breakout
signal before live_bot.py sends the order to Alpaca.

Design rules (why it looks the way it does):

- Lives outside strategy.py on purpose. strategy.py is pure and shared with
  backtest.py; an API call there would make backtests slow, costly and
  non-repeatable. live_bot.py calls evaluate() right after check_breakout().
- The AI can only make a trade SMALLER or SKIP it — never riskier. It can
  veto, cut position size (0.25x-1.0x), and pick a take-profit between 1R and
  3R. The stop stays at the opening-range level (market structure), and the
  hard stop-distance limit is enforced here in code, not left to the model.
- Fail-open: if the API key is missing, the call times out, errors, is rate
  limited, or returns something unusable, the trade goes ahead exactly as the
  plain ORB strategy would have placed it. An outage never stalls the bot.
- Synchronous, short timeout. The bot polls every 15 s in a simple loop, so an
  async client would add complexity without making anything faster.
- Every decision (including fail-opens) is appended to ai_decisions.csv so the
  AI's record can be judged against real outcomes on the dashboard.
"""

import csv
import json
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from zoneinfo import ZoneInfo

import config

try:
    import anthropic
except ImportError:  # dependency missing -> behave as if the filter is off
    anthropic = None

ET = ZoneInfo("America/New_York")
DECISIONS_FILE = "ai_decisions.csv"

MODEL = "claude-opus-5-5"
REQUEST_TIMEOUT_SECONDS = 25.0

# Bounds the model's answer is clamped to, whatever it returns.
MIN_SIZE_MULT, MAX_SIZE_MULT = 0.25, 1.0
MIN_TARGET_R, MAX_TARGET_R = 1.0, 3.0


@dataclass
class Decision:
    action: str                       # "CONFIRM" or "VETO"
    confidence_score: float
    reasoning: str
    position_size_multiplier: float   # applied to the risk-sized share count
    take_profit_r: float              # target distance as a multiple of initial risk
    dynamic_stop_loss_pct: float      # the model's view of a safe stop distance (informational)
    dynamic_take_profit_pct: float    # target distance as % of entry (informational)
    source: str = "ai"                # "ai", "fail_open", "hard_limit", "disabled"
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.action == "CONFIRM"


SYSTEM_PROMPT = """You are the Risk Officer for an automated intraday Opening Range Breakout (ORB) desk. \
Every signal the strategy produces reaches you before an order is sent, and you decide whether it trades. \
You are paid to protect capital first and to let only high-quality breakouts through; a missed trade costs \
nothing, a fakeout costs a full 1R.

How the desk trades: the opening range (OR) is the high/low of the first minutes after 9:30 ET. A long \
signal fires when a 1-minute bar closes above the OR high by a small buffer on volume at least a set \
multiple of the average OR bar volume. The stop sits at the far side of the opening range, position size \
risks a fixed percentage of equity on that stop distance, every position is flat by 15:45 ET, and there \
are no overnight holds. Market data comes from the IEX feed, which is only a slice of consolidated volume, \
so treat absolute volumes as partial and focus on the ratios.

For each signal, judge it mechanically from the numbers provided:
1. Volume quality. Is the breakout bar's relative volume genuinely strong, and is today's cumulative \
volume running at or above a normal pace for this time of day? A breakout on fading or merely average \
participation, or one that has been drifting sideways just under the level for a long time, is a fakeout \
risk.
2. Overhead levels. For a long, is the entry pressing directly into the prior day's high, the prior \
day's close, or today's session high with little room to the 1R-2R target? For a short, the same against \
supports. A target that sits beyond a nearby unbroken level should be cut or the trade vetoed.
3. Extension and timing. Is price already stretched far above VWAP or far beyond the OR high, or has the \
stock gapped so much that the day's range is largely spent? Late-day breakouts (after about 14:00 ET) \
have less time to reach target before the forced exit.
4. Market backdrop. A long against a clearly falling QQQ, or a short against a clearly rising one, needs \
stronger evidence.
5. Stop distance. If the stop distance as a percentage of entry exceeds the stated maximum, veto: the \
risk is too wide for an intraday trade.

Then call the record_decision tool exactly once, and write nothing else. Use CONFIRM only when the \
evidence clearly supports the breakout; when the picture is mixed, VETO. Your confidence_score is your \
probability that the trade reaches at least +1R before its stop. Use position_size_multiplier below 1.0 \
to take a smaller position on a valid but imperfect setup. Set take_profit_r to where you expect the move \
to realistically run, shortening it when a nearby level stands in the way. Keep reasoning to one plain \
sentence naming the deciding factor."""

DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record the risk decision for this breakout signal. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "action", "confidence_score", "reasoning", "position_size_multiplier",
            "take_profit_r", "dynamic_stop_loss_pct", "dynamic_take_profit_pct",
        ],
        "properties": {
            "action": {"type": "string", "enum": ["CONFIRM", "VETO"]},
            "confidence_score": {
                "type": "number",
                "description": "0.0-1.0: probability the trade reaches +1R before its stop.",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence naming the mechanical reason for the decision.",
            },
            "position_size_multiplier": {
                "type": "number",
                "description": "0.25-1.0 multiplier on the standard risk-sized position. 1.0 = full size.",
            },
            "take_profit_r": {
                "type": "number",
                "description": "Take-profit distance as a multiple of the initial risk (stop distance), 1.0-3.0.",
            },
            "dynamic_stop_loss_pct": {
                "type": "number",
                "description": "Stop distance you consider appropriate, as a percent of entry (e.g. 1.2 = 1.2%).",
            },
            "dynamic_take_profit_pct": {
                "type": "number",
                "description": "Take-profit distance as a percent of entry (e.g. 2.4 = 2.4%).",
            },
        },
    },
}

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=1,
        )
    return _client


def mode() -> str:
    """'enforce' (AI decides), 'shadow' (AI decides nothing, only logged), or 'off'."""
    m = str(getattr(config, "AI_FILTER_MODE", "enforce")).lower()
    return m if m in ("enforce", "shadow", "off") else "enforce"


def _default_decision(context: dict, source: str, reason: str) -> Decision:
    """The plain-ORB trade, unchanged. Used whenever the AI can't give an answer."""
    entry = context.get("entry_price") or 0.0
    risk = context.get("risk_per_share") or 0.0
    r_mult = float(getattr(config, "REWARD_RISK_MULTIPLE", 2.0))
    return Decision(
        action="CONFIRM",
        confidence_score=0.0,
        reasoning=reason,
        position_size_multiplier=1.0,
        take_profit_r=r_mult,
        dynamic_stop_loss_pct=round(risk / entry * 100, 3) if entry else 0.0,
        dynamic_take_profit_pct=round(risk * r_mult / entry * 100, 3) if entry else 0.0,
        source=source,
    )


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _parse(tool_input: dict, context: dict) -> Decision:
    action = str(tool_input.get("action", "")).upper()
    if action not in ("CONFIRM", "VETO"):
        raise ValueError(f"unexpected action {action!r}")
    r_default = float(getattr(config, "REWARD_RISK_MULTIPLE", 2.0))
    d = Decision(
        action=action,
        confidence_score=_clamp(tool_input.get("confidence_score"), 0.0, 1.0, 0.0),
        reasoning=str(tool_input.get("reasoning", "")).strip()[:300],
        position_size_multiplier=_clamp(tool_input.get("position_size_multiplier"), MIN_SIZE_MULT, MAX_SIZE_MULT, 1.0),
        take_profit_r=_clamp(tool_input.get("take_profit_r"), MIN_TARGET_R, MAX_TARGET_R, r_default),
        dynamic_stop_loss_pct=_clamp(tool_input.get("dynamic_stop_loss_pct"), 0.0, 100.0, 0.0),
        dynamic_take_profit_pct=_clamp(tool_input.get("dynamic_take_profit_pct"), 0.0, 100.0, 0.0),
        source="ai",
        raw=tool_input,
    )
    # A confirmation the model itself isn't confident in is not good enough.
    min_conf = float(getattr(config, "AI_MIN_CONFIDENCE", 0.55))
    if d.approved and d.confidence_score < min_conf:
        d.action = "VETO"
        d.reasoning = f"Confidence {d.confidence_score:.2f} below minimum {min_conf:.2f}. " + d.reasoning
    return d


def evaluate(context: dict) -> Decision:
    """Returns the decision for one breakout signal. Never raises."""
    started = time.monotonic()

    # Hard limit, enforced in code regardless of mode: too-wide stops never trade
    # in enforce mode, and are logged as such in shadow mode.
    max_stop_pct = float(getattr(config, "AI_MAX_STOP_PCT", 0.025)) * 100
    stop_pct = context.get("stop_distance_pct")
    if stop_pct is not None and stop_pct > max_stop_pct:
        d = _default_decision(context, "hard_limit",
                              f"Stop distance {stop_pct:.2f}% exceeds the {max_stop_pct:.2f}% intraday limit.")
        d.action = "VETO"
        return _finish(d, context, started)

    if anthropic is None:
        return _finish(_default_decision(context, "fail_open", "anthropic package not installed; trading as plain ORB."),
                       context, started)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _finish(_default_decision(context, "fail_open", "ANTHROPIC_API_KEY not set; trading as plain ORB."),
                       context, started)

    try:
        response = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},   # fast, mechanical judgement on a 15 s poll
            tools=[DECISION_TOOL],
            tool_choice={"type": "auto"},      # forced tool_choice isn't supported on this model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{
                "role": "user",
                "content": "Evaluate this breakout signal and call record_decision.\n\n"
                           + json.dumps(context, indent=2, sort_keys=True, default=str),
            }],
        )
        if response.stop_reason == "refusal":
            return _finish(_default_decision(context, "fail_open", "Model declined the request; trading as plain ORB."),
                           context, started)
        tool_use = next((b for b in response.content if getattr(b, "type", None) == "tool_use"
                         and b.name == "record_decision"), None)
        if tool_use is None:
            return _finish(_default_decision(context, "fail_open", "No decision returned; trading as plain ORB."),
                           context, started)
        tool_input = tool_use.input if isinstance(tool_use.input, dict) else json.loads(tool_use.input)
        return _finish(_parse(tool_input, context), context, started)

    except anthropic.RateLimitError:
        reason = "Claude API rate limited"
    except anthropic.APITimeoutError:
        reason = "Claude API timed out"
    except anthropic.AuthenticationError:
        reason = "Claude API key rejected"
    except anthropic.APIStatusError as e:
        reason = f"Claude API error {e.status_code}"
    except anthropic.APIConnectionError:
        reason = "Could not reach the Claude API"
    except Exception as e:  # malformed output, parsing bugs, anything else
        reason = f"AI filter error: {type(e).__name__}"
    return _finish(_default_decision(context, "fail_open", f"{reason}; trading as plain ORB."), context, started)


def _finish(decision: Decision, context: dict, started: float) -> Decision:
    decision.latency_ms = int((time.monotonic() - started) * 1000)
    _log(decision, context)
    return decision


def _log(d: Decision, context: dict):
    row = {
        "timestamp": datetime.now(ET).isoformat(timespec="seconds"),
        "mode": mode(),
        "symbol": context.get("symbol"),
        "direction": context.get("direction"),
        "entry_price": context.get("entry_price"),
        "stop_price": context.get("stop_price"),
        "action": d.action,
        "source": d.source,
        "confidence": round(d.confidence_score, 3),
        "size_mult": round(d.position_size_multiplier, 2),
        "take_profit_r": round(d.take_profit_r, 2),
        "stop_pct": round(d.dynamic_stop_loss_pct, 3),
        "take_profit_pct": round(d.dynamic_take_profit_pct, 3),
        "latency_ms": d.latency_ms,
        "reasoning": d.reasoning,
    }
    try:
        is_new = not os.path.exists(DECISIONS_FILE)
        with open(DECISIONS_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if is_new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[ai_filter] could not write {DECISIONS_FILE}: {e}")
