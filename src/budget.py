"""In-app daily spend ceiling — the only hard cost backstop.

No provider-side cap is available to us: OpenAI key-level limits can't be set on
our account, and project-level caps are soft (email alert, no stop). So the app
must stop itself. Each model call returns token `usage`; we convert it to a
dollar estimate with the configured per-model rates, accumulate it per UTC day,
and refuse to answer once the day's total crosses llm.daily_budget_usd.

Single-node app: an in-process counter guarded by a lock, written through to a
small JSON file so it survives restarts. Not built for multi-process serving.

The estimate is deliberately CONSERVATIVE (over-counts, so the ceiling trips
early rather than late):
  - It prices all input at the standard rate, ignoring prompt caching. The
    agent path threads context via previous_response_id, so repeated tokens are
    cached at ~1/10th the rate — real spend is somewhat lower. Fine while the
    ceiling is high; if the ceiling is lowered a lot, credit cached tokens
    (usage.*_details.cached_tokens) at a cached rate to avoid cutting off early.
  - Rates are the short-context tier. Requests above ~272K input tokens bill on
    OpenAI's long-context schedule; our questions are far below that.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(".state/daily_spend.json")

# Used only when a model is missing from llm.pricing — deliberately high so an
# unpriced model over-counts (fails safe toward the ceiling) rather than costing
# silently. Real rates belong in config.
DEFAULT_RATE = {"input": 5.0, "output": 15.0}  # $ per 1M tokens


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _tokens(usage) -> tuple[int, int]:
    """(input, output) tokens from a chat.completions or Responses usage object
    (or a dict) — the two APIs name the fields differently."""
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else lambda k: getattr(usage, k, None)
    inp = get("input_tokens") or get("prompt_tokens") or 0
    out = get("output_tokens") or get("completion_tokens") or 0
    return int(inp), int(out)


def estimate_cost(model: str, inp: int, out: int, pricing: dict) -> float:
    rate = (pricing or {}).get(model) or DEFAULT_RATE
    return inp / 1e6 * rate["input"] + out / 1e6 * rate["output"]


class Budget:
    """Tracks and caps daily spend. `record()` after each call; `over()` before."""

    def __init__(self, config: dict, state_path: Path | None = None,
                 today_fn=_utc_today):
        llm = config.get("llm") or {}
        self.limit = llm.get("daily_budget_usd")   # None = track but don't enforce
        self.pricing = llm.get("pricing") or {}
        self.path = state_path or STATE_PATH
        self._today_fn = today_fn
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("date") != self._today_fn():   # new UTC day -> reset
            return {"date": self._today_fn(), "usd": 0.0}
        return data

    def spent_today(self) -> float:
        with self._lock:
            return round(self._load().get("usd", 0.0), 4)

    def over(self) -> bool:
        if self.limit is None:
            return False
        return self.spent_today() >= self.limit

    def remaining(self) -> float | None:
        if self.limit is None:
            return None
        return max(self.limit - self.spent_today(), 0.0)

    def record(self, model: str, usage) -> float:
        """Add one call's cost to today's total; returns the new daily total."""
        inp, out = _tokens(usage)
        cost = estimate_cost(model, inp, out, self.pricing)
        with self._lock:
            data = self._load()
            data["usd"] = round(data.get("usd", 0.0) + cost, 6)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data))
            return data["usd"]
