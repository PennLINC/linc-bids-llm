"""Tests for the eval scoring logic (pure functions; no network)."""
import json
from types import SimpleNamespace

from eval.run_eval import (ANSWER_CHARS, JUDGE_RUNS, JUDGE_SYS, JUDGE_SYS_FEEDBACK,
                           _clip, _first_docs, _reciprocal_rank, _urls,
                           explain_miss, judge_answer, judge_messages,
                           judge_model, retrieval_scores)
from eval.harvest_eval import _stratified_sample
from src.urls import canon_url


def test_reciprocal_rank():
    assert _reciprocal_rank("g", ["g", "b", "c"]) == 1.0
    assert _reciprocal_rank("g", ["a", "g", "c"]) == 0.5
    assert _reciprocal_rank("g", ["a", "b", "c"]) == 0.0


def test_urls_strips_doc_anchor():
    recs = [{"url": "https://x/blob/1.0/f.rst?plain=1#L1-L9"},
            {"url": "https://neurostars.org/t/slug/42"}]
    assert _urls(recs) == ["https://x/blob/1.0/f.rst?plain=1",
                           "https://neurostars.org/t/slug/42"]


def test_stratified_sample_spans_old_and_new():
    cases = [{"created": f"2020-{m:02d}-01", "i": m} for m in range(1, 13)]
    picked = _stratified_sample(cases, 4)
    assert len(picked) == 4
    months = [c["created"] for c in picked]
    assert any(m < "2020-07" for m in months)   # drew from the older half
    assert any(m >= "2020-07" for m in months)   # and the newer half


def test_stratified_sample_returns_all_when_small():
    cases = [{"created": "2020-01-01"}, {"created": "2020-02-01"}]
    assert len(_stratified_sample(cases, 10)) == 2


class FakeStore:
    """Returns a fixed ranking so scoring math is checked without embeddings."""
    def __init__(self, ranking):
        self.ranking = ranking

    def hybrid_query(self, query, k, where=None, per_doc=None):
        return _first_docs(self.ranking, k) if per_doc else self.ranking[:k]

    def _vector_ids(self, q, k, where=None):
        return [r["id"] for r in self.ranking[:k]]

    def _bm25_ids(self, q, k, where=None):
        return [r["id"] for r in self.ranking[:k]]

    def _hydrate(self, ids):
        by_id = {r["id"]: r for r in self.ranking}
        return {i: by_id[i] for i in ids}


def test_retrieval_scores_hit_and_mrr():
    ranking = [{"id": "1", "url": "u-miss"}, {"id": "2", "url": "u-gold"}]
    store = FakeStore(ranking)
    cases = [{"app": "qsiprep", "source": "issues",
              "query": "q", "gold_url": "u-gold"}]
    r = retrieval_scores(store, cases, k=8)
    assert r["n"] == 1
    assert r["overall"]["hybrid"]["hit_rate"] == 1.0
    assert r["overall"]["hybrid"]["mrr"] == 0.5      # gold at rank 2
    assert r["per_source"]["issues"]["hybrid"] == 1.0
    assert r["cases"][0]["rank"] == 2


def test_canon_url_collapses_the_forms_a_browser_hands_out():
    issue = "gh:pennlinc/qsiprep#681"
    assert canon_url("https://github.com/PennLINC/qsiprep/issues/681") == issue
    assert canon_url("https://github.com/PennLINC/qsiprep/issues/681#issuecomment-9") == issue
    assert canon_url("http://www.github.com/pennlinc/qsiprep/pull/681/files") == issue
    topic = "ns:29454"
    assert canon_url("https://neurostars.org/t/some-slug/29454") == topic
    assert canon_url("https://neurostars.org/t/some-slug/29454/3?u=me") == topic
    assert canon_url("https://neurostars.org/t/29454/3") == topic      # no slug
    doc = "ghfile:pennlinc/qsirecon:docs/quickstart.rst"
    assert canon_url("https://github.com/PennLINC/qsirecon/blob/26.0.0/"
                     "docs/quickstart.rst?plain=1#L1-L45") == doc
    assert canon_url("https://github.com/PennLINC/qsirecon/blob/main/"
                     "docs/quickstart.rst") == doc                       # other ref
    assert canon_url("https://github.com/PennLINC/qsiprep/issues/68") != issue
    assert canon_url(" u-gold ") == "u-gold"                            # not a URL
    assert canon_url("") == ""


def test_retrieval_scores_match_gold_pasted_in_another_form():
    ranking = [{"id": "1", "url": "https://neurostars.org/t/slug/42"}]
    cases = [{"app": "qsiprep", "source": "feedback", "query": "q",
              "gold_url": "https://neurostars.org/t/slug/42/7"}]   # scrolled to post 7
    r = retrieval_scores(FakeStore(ranking), cases, k=8)
    assert r["overall"]["hybrid"]["hit_rate"] == 1.0


def test_single_method_mrr_uses_rank_order_not_hydrate_order():
    class ScrambledStore(FakeStore):
        def _hydrate(self, ids):            # like Chroma: storage order, not rank
            return dict(reversed(list(super()._hydrate(ids).items())))

    ranking = [{"id": "1", "url": "u-gold"}, {"id": "2", "url": "u-miss"}]
    cases = [{"app": "qsiprep", "source": "issues", "query": "q", "gold_url": "u-gold"}]
    r = retrieval_scores(ScrambledStore(ranking), cases, k=8)
    assert r["overall"]["vector"]["mrr"] == 1.0
    assert r["overall"]["bm25"]["mrr"] == 1.0


def test_retrieval_scores_scope_like_the_app():
    seen = []

    class ScopeStore(FakeStore):
        def hybrid_query(self, query, k, where=None, per_doc=None):
            seen.append((where, per_doc))
            return super().hybrid_query(query, k, where, per_doc)

    config = {"apps": {"qsiprep": {"neighbors": ["qsirecon"]}}}
    cases = [{"app": "qsiprep", "source": "feedback", "query": "q", "gold_url": "u"}]
    retrieval_scores(ScopeStore([]), cases, k=8, config=config)
    retrieval_scores(ScopeStore([]), cases, k=8)
    # scoped like the app, and one result per document like search_kb
    assert seen == [({"app": ["qsiprep", "qsirecon"]}, 1), ({"app": "qsiprep"}, 1)]


def test_retrieval_scores_count_documents_not_chunks():
    # three chunks of one thread outrank the gold; the agent sees them as one
    # result, so the gold is 2nd, not 4th, and makes a top 2
    ranking = [{"id": f"a{i}", "url": "u-long#L%d" % i} for i in range(3)]
    ranking.append({"id": "g", "url": "u-gold"})
    cases = [{"app": "qsiprep", "source": "issues", "query": "q", "gold_url": "u-gold"}]
    r = retrieval_scores(FakeStore(ranking), cases, k=2)
    for method in ("hybrid", "vector", "bm25"):
        assert r["overall"][method] == {"hit_rate": 1.0, "mrr": 0.5}


def test_first_docs_keeps_the_best_chunk_of_each_document():
    recs = [{"id": "1", "url": "a#L1-L5"}, {"id": "2", "url": "a#L9-L20"},
            {"id": "3", "url": "b"}, {"id": "4", "url": "c"}]
    assert [r["id"] for r in _first_docs(recs, 2)] == ["1", "3"]
    assert [r["id"] for r in _first_docs(recs, 8)] == ["1", "3", "4"]


def test_judge_model_reads_older_configs():
    assert judge_model({"llm": {"judge_model": "j", "oneshot_model": "m"}}) == "j"
    assert judge_model({"llm": {"oneshot_model": "m"}}) == "m"    # pre-removal config


def test_explain_miss_separates_corpus_gaps_from_ranking():
    index_apps = {"gh:pennlinc/qsiprep#1": {"qsiprep"}, "ns:9": {"xcp_d"}}
    case = {"scope": ["qsiprep", "qsirecon"]}
    gap = explain_miss({**case, "gold_url": "https://qsiprep.readthedocs.io/x"}, index_apps)
    assert gap.startswith("not in the index")
    other = explain_miss({**case, "gold_url": "https://neurostars.org/t/s/9"}, index_apps)
    assert "outside this app's scope" in other and "xcp_d" in other
    ranked = explain_miss({**case, "gold_url": "https://github.com/PennLINC/qsiprep/issues/1"},
                          index_apps)
    assert "below the top-k" in ranked


def test_judge_frames_feedback_notes_differently_from_resolutions():
    heldout = {"source": "issues", "query": "q", "reference": "use --flag"}
    assert judge_messages(heldout, "cand")[0]["content"] == JUDGE_SYS
    fb = {"source": "feedback", "query": "q", "reference": "it invented a flag",
          "category": "hallucination / made-up detail", "flagged_answer": "use --fix-fa"}
    system, user = judge_messages(fb, "cand")
    assert system["content"] == JUDGE_SYS_FEEDBACK
    for part in ("it invented a flag", "use --fix-fa", "hallucination", "cand"):
        assert part in user["content"]


def test_answer_scores_replays_a_followup_with_its_history(monkeypatch):
    """A replayed follow-up must reach the agent with the chat it was asked
    in. A case a tester rated on the old one-shot path says so. No LLM is
    called."""
    import eval.run_eval as run_eval

    seen = []

    class Result:
        answer = "agent answer"

    def fake_agent(question, app, config, store, history=None, client=None):
        seen.append(history)
        return Result()

    monkeypatch.setattr(run_eval.answer_mod, "_client", lambda: object())
    monkeypatch.setattr(run_eval.answer_mod, "answer_agent", fake_agent)
    monkeypatch.setattr(run_eval, "judge_answer",
                        lambda case, cand, config, client:
                            {"verdict": "pass", "reason": "ok"})

    history = [{"role": "user", "content": "how do I set the resolution?"},
               {"role": "assistant", "content": "Use --output-resolution."}]
    followup = {"source": "feedback", "case_id": 1, "app": "qsiprep",
                "query": "why?", "reference": "because ...", "history": history,
                "rated_path": "agent"}
    oneshot = {"source": "feedback", "case_id": 2, "app": "qsiprep",
               "query": "what is eddy?", "reference": "...", "rated_path": "oneshot"}
    out = run_eval.answer_scores(store=None, cases=[followup, oneshot], config={},
                                 sample=2)

    assert sorted(seen, key=bool) == [None, history]
    assert out["n"] == 2 and out["pass_rate"] == 1.0
    rated = {d["case"]: d["rated"] for d in out["details"]}
    assert rated == {"feedback#1": "", "feedback#2": "rated on oneshot"}


def test_judge_reads_the_end_of_a_long_answer_and_the_earlier_chat():
    # the offending line of a long agent answer is often its last
    ending = "If you tell me your version I can be more specific."
    long_answer = "Background. " * 400 + ending              # well past 2.5k chars
    fb = {"source": "feedback", "query": "and compared to FAST?",
          "reference": "I gave the version at the start; it keeps asking",
          "flagged_answer": long_answer,
          "history": [{"role": "user", "content": "I'm on qsiprep 1.0.1 ..."},
                      {"role": "assistant", "content": "On 1.0.1 ..."}]}
    user = judge_messages(fb, long_answer)[1]["content"]
    assert user.count(ending) == 2                            # flagged + candidate
    assert "I'm on qsiprep 1.0.1" in user                     # the chat it was asked in
    assert user.index("I'm on qsiprep 1.0.1") < user.index("and compared to FAST?")
    heldout = {"source": "issues", "query": "q", "reference": "r"}
    assert ending in judge_messages(heldout, long_answer)[1]["content"]


def test_clip_keeps_head_and_tail():
    assert _clip("short", 100) == "short"
    text = "HEAD" + "x" * (2 * ANSWER_CHARS) + "TAIL"
    clipped = _clip(text, ANSWER_CHARS)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert "characters omitted" in clipped and len(clipped) < len(text)


class FakeJudge:
    """Stands in for the OpenAI client: replays a fixed run of verdicts."""
    def __init__(self, *contents):
        self.contents = list(contents)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        message = SimpleNamespace(content=self.contents.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _verdict(verdict, reason="r"):
    return json.dumps({"verdict": verdict, "reason": reason})


def test_judge_passes_only_when_every_run_passes():
    case = {"source": "feedback", "query": "q", "reference": "note"}
    config = {"llm": {"oneshot_model": "m"}}
    rest = [_verdict("pass")] * (JUDGE_RUNS - 2)

    unanimous = FakeJudge(_verdict("pass", "fixed"), _verdict("pass"), *rest)
    assert judge_answer(case, "cand", config, unanimous) == {
        "verdict": "pass", "reason": "fixed"}

    split = FakeJudge(_verdict("pass"), _verdict("fail", "still ignores it"), *rest)
    out = judge_answer(case, "cand", config, split)
    assert out["verdict"] == "fail"                          # a coin flip isn't a pass
    assert out["reason"] == (f"[judge split: {JUDGE_RUNS - 1} of {JUDGE_RUNS} runs "
                             "passed] still ignores it")

    clear = FakeJudge(_verdict("fail", "same answer"), "not json",
                      *[_verdict("fail")] * (JUDGE_RUNS - 2))
    assert judge_answer(case, "cand", config, clear) == {
        "verdict": "fail", "reason": "same answer"}          # no split label
