"""Daily spend ceiling: cost math, accumulation, UTC-day reset, enforcement."""
from src.budget import Budget, estimate_cost, _tokens


CONFIG = {"llm": {
    "daily_budget_usd": 10.0,
    "pricing": {"m-small": {"input": 1.0, "output": 2.0}},
}}


def _budget(tmp_path, day="2026-08-04", **over):
    cfg = {"llm": {**CONFIG["llm"], **over}}
    return Budget(cfg, state_path=tmp_path / "spend.json", today_fn=lambda: day)


def test_tokens_handles_both_api_shapes():
    class ChatUsage:  # chat.completions
        prompt_tokens, completion_tokens = 100, 50
    class RespUsage:  # Responses API
        input_tokens, output_tokens = 200, 25
    assert _tokens(ChatUsage()) == (100, 50)
    assert _tokens(RespUsage()) == (200, 25)
    assert _tokens({"input_tokens": 7, "output_tokens": 3}) == (7, 3)
    assert _tokens(None) == (0, 0)


def test_estimate_cost():
    pricing = {"m": {"input": 1.0, "output": 2.0}}
    assert estimate_cost("m", 1_000_000, 1_000_000, pricing) == 3.0
    # unknown model falls back to the high default (fails safe, non-zero)
    assert estimate_cost("mystery", 1_000_000, 0, pricing) == 5.0


def test_record_accumulates_and_persists(tmp_path):
    b = _budget(tmp_path)
    b.record("m-small", {"input_tokens": 1_000_000, "output_tokens": 0})   # $1
    b.record("m-small", {"input_tokens": 0, "output_tokens": 1_000_000})   # $2
    assert b.spent_today() == 3.0
    # a fresh Budget reads the same persisted state (survives restart)
    assert _budget(tmp_path).spent_today() == 3.0


def test_over_threshold(tmp_path):
    b = _budget(tmp_path)
    assert not b.over()
    b.record("m-small", {"input_tokens": 9_000_000, "output_tokens": 0})   # $9
    assert not b.over() and b.remaining() == 1.0
    b.record("m-small", {"input_tokens": 1_000_000, "output_tokens": 0})   # $10
    assert b.over() and b.remaining() == 0.0


def test_new_utc_day_resets(tmp_path):
    day1 = _budget(tmp_path, day="2026-08-04")
    day1.record("m-small", {"input_tokens": 20_000_000, "output_tokens": 0})  # $20
    assert day1.over()
    day2 = _budget(tmp_path, day="2026-08-05")     # same state file, next day
    assert day2.spent_today() == 0.0 and not day2.over()


def test_no_limit_never_blocks(tmp_path):
    b = _budget(tmp_path, daily_budget_usd=None)
    b.record("m-small", {"input_tokens": 999_000_000, "output_tokens": 0})
    assert b.over() is False and b.remaining() is None    # tracked but unenforced
