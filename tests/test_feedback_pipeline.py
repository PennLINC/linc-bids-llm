"""Feedback aggregation + promotion to regression cases (pure logic)."""
import json

from eval.feedback_report import load_entries, prepare, summarize
from eval.feedback_to_cases import entries_to_cases, merge_cases, _case_id


ENTRIES = [
    {"rating": "up", "path": "oneshot", "question": "how to set output res?"},
    {"rating": "down", "path": "agent", "question": "eddy crashes on my data",
     "category": "wrong fix / advice", "correct_url": "https://gh/issues/42",
     "comment": "should point at --eddy-config cnr_maps", "ts": "2026-07-20T01:00Z"},
    {"rating": "down", "path": "agent", "question": "why is FA inverted?",
     "category": "hallucination / made-up detail", "correct_url": "",
     "comment": "it invented a flag", "ts": "2026-07-20T02:00Z"},
    {"rating": "down", "path": "oneshot", "question": "",   # no query -> skipped
     "correct_url": "https://gh/x", "ts": "2026-07-20T03:00Z"},
]


def test_load_entries_skips_blank_and_bad_json(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"rating": "up"}\n\n{not json\n{"rating": "down"}\n')
    entries = load_entries([p, tmp_path / "missing.jsonl"])
    assert [e["rating"] for e in entries] == ["up", "down"]


def test_summarize_counts():
    s = summarize(ENTRIES)
    assert s["total"] == 4 and s["up"] == 1 and s["down"] == 3
    assert s["by_path"]["agent"]["down"] == 2
    assert s["by_path"]["oneshot"]["up"] == 1
    assert s["by_category"]["wrong fix / advice"] == 1
    assert len(s["failures"]) == 3


def test_summarize_by_app():
    entries = [
        {"rating": "up", "app": "qsiprep", "path": "oneshot"},
        {"rating": "down", "app": "xcp_d", "path": "agent"},
        {"rating": "up", "app": "xcp_d", "path": "oneshot"},
        {"rating": "down", "path": "agent"},          # no app -> "?"
    ]
    s = summarize(entries)
    assert s["by_app"]["qsiprep"] == {"up": 1, "down": 0, "total": 1}
    assert s["by_app"]["xcp_d"] == {"up": 1, "down": 1, "total": 2}
    assert s["by_app"]["?"]["total"] == 1


def test_summarize_by_model_enables_comparison():
    entries = [
        {"rating": "up", "path": "agent", "model": "gpt-5.6-terra"},
        {"rating": "down", "path": "agent", "model": "gpt-5.6-terra"},
        {"rating": "up", "path": "agent", "model": "glm-4.7-32b"},
        {"rating": "up", "path": "agent"},           # pre-provenance entry
    ]
    s = summarize(entries)
    assert s["by_model"]["gpt-5.6-terra"] == {"up": 1, "down": 1, "total": 2}
    assert s["by_model"]["glm-4.7-32b"]["up"] == 1
    assert s["by_model"]["(unknown)"]["total"] == 1   # older entries bucketed


def test_run_context_records_model_per_path():
    from src.feedback import run_context
    cfg = {"llm": {"oneshot_model": "m-small", "agent_model": "m-big",
                   "api_base": "http://localhost:11434/v1"},
           "retrieval": {"embed_model": "bge"}}
    one = run_context(cfg, "oneshot")
    agent = run_context(cfg, "agent")
    assert one["model"] == "m-small" and agent["model"] == "m-big"
    # both models always recorded, so a rating stays interpretable later
    assert one["oneshot_model"] == "m-small" and one["agent_model"] == "m-big"
    assert one["embed_model"] == "bge"
    assert one["api_base"] == "http://localhost:11434/v1"


def test_entries_to_cases_filters_and_shapes():
    cases = entries_to_cases(ENTRIES)
    # up-rated skipped; empty-question skipped -> 2 usable failures
    assert len(cases) == 2
    by_q = {c["query"]: c for c in cases}
    eddy = by_q["eddy crashes on my data"]
    assert eddy["source"] == "feedback"
    assert eddy["gold_url"] == "https://gh/issues/42"      # retrieval-scorable
    assert eddy["reference"].startswith("should point")
    fa = by_q["why is FA inverted?"]
    assert fa["gold_url"] == ""                            # answer-only
    assert fa["reference"] == "it invented a flag"


def test_case_id_stable_and_query_url_keyed():
    a = {"question": "q", "correct_url": "u"}
    b = {"question": "q", "correct_url": "u", "comment": "different"}
    assert _case_id(a) == _case_id(b)                      # id ignores comment
    assert _case_id(a) != _case_id({"question": "q", "correct_url": "v"})


def test_case_id_ignores_url_form_but_separates_apps():
    a = {"app": "qsiprep", "question": "q",
         "correct_url": "https://neurostars.org/t/slug/42"}
    scrolled = {**a, "correct_url": "https://neurostars.org/t/slug/42/7"}
    assert _case_id(a) == _case_id(scrolled)
    assert _case_id(a) != _case_id({**a, "app": "aslprep"})


def test_cases_carry_what_a_replay_needs():
    history = [{"role": "user", "content": "first q"},
               {"role": "assistant", "content": "first a"}]
    case, = entries_to_cases([
        {"rating": "down", "app": "qsiprep", "path": "agent", "question": "and on 1.0?",
         "answer": "bad answer", "comment": "flag didn't exist yet", "history": history}])
    assert case["flagged_answer"] == "bad answer"
    assert case["history"] == history and case["rated_path"] == "agent"


def test_prepare_keeps_one_entry_per_rated_answer():
    click = {"rating": "down", "app": "qsiprep", "question": "q", "answer": "a"}
    entries = prepare([
        click, dict(click),                                  # double-click
        {**click, "comment": "added the url", "correct_url": "u"},   # revised
        {**click, "answer": "a different answer"},           # another answer: kept
    ])
    assert len(entries) == 2
    assert entries[0]["correct_url"] == "u"                  # last click wins


def test_prepare_keys_on_session_and_turn_once_logged():
    base = {"rating": "down", "chat_id": "c1", "question": "q", "answer": "a"}
    entries = prepare([
        {**base, "session": "s1", "turn": 1},
        {**base, "session": "s1", "turn": 1, "rating": "up"},   # same tester re-rates
        {**base, "session": "s1", "turn": 2},                   # a later turn
        {**base, "session": "s2", "turn": 1},                   # someone else's view
    ])
    assert [(e["session"], e["turn"], e["rating"]) for e in entries] == [
        ("s1", 1, "up"), ("s1", 2, "down"), ("s2", 1, "down")]


def test_prepare_counts_a_category_without_a_thumb_as_down():
    entries = prepare([
        {"rating": None, "category": "wrong version", "question": "q1", "answer": "a",
         "comment": "described 0.7"},
        {"rating": None, "category": None, "question": "q2", "answer": "a",
         "comment": "just a remark"},
    ])
    s = summarize(entries)
    assert s["down"] == 1 and s["implied"] == 1
    assert [e["question"] for e in s["failures"]] == ["q1"]
    assert [e["question"] for e in s["notes"]] == ["q2"]     # surfaced, not dropped
    assert len(entries_to_cases(entries)) == 1               # and promoted


def test_merge_cases_dedupes_and_updates():
    old = [{"case_id": "x", "created": "1", "reference": "old"},
           {"case_id": "y", "created": "2"}]
    new = [{"case_id": "x", "created": "3", "reference": "new"},   # updates x
           {"case_id": "z", "created": "4"}]
    merged = merge_cases(old, new)
    ids = {c["case_id"] for c in merged}
    assert ids == {"x", "y", "z"}
    x = next(c for c in merged if c["case_id"] == "x")
    assert x["reference"] == "new"                          # newer wins


def test_regression_cases_are_run_eval_compatible():
    """Promoted cases must carry the fields run_eval's scorers read."""
    from eval.run_eval import retrieval_scores
    cases = entries_to_cases(ENTRIES)

    class FakeStore:
        def hybrid_query(self, q, k, where=None):
            return [{"url": "https://gh/issues/42"}]
        def _vector_ids(self, q, k, where=None): return ["a"]
        def _bm25_ids(self, q, k, where=None): return ["a"]
        def _hydrate(self, ids): return {"a": {"url": "https://gh/issues/42"}}

    r = retrieval_scores(FakeStore(), cases, k=8)
    assert r["n"] == 1                                      # only the gold_url case scored
    assert r["overall"]["hybrid"]["hit_rate"] == 1.0
