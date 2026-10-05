"""Tests for the eval scoring logic (pure functions; no network)."""
from eval.run_eval import (JUDGE_SYS, JUDGE_SYS_FEEDBACK, _reciprocal_rank, _urls,
                           explain_miss, judge_messages, retrieval_scores)
from eval.harvest_eval import _stratified_sample
from eval.urls import canon_url


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

    def hybrid_query(self, query, k, where=None):
        return self.ranking[:k]

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
        def hybrid_query(self, query, k, where=None):
            seen.append(where)
            return super().hybrid_query(query, k, where)

    config = {"apps": {"qsiprep": {"neighbors": ["qsirecon"]}}}
    cases = [{"app": "qsiprep", "source": "feedback", "query": "q", "gold_url": "u"}]
    retrieval_scores(ScopeStore([]), cases, k=8, config=config)
    retrieval_scores(ScopeStore([]), cases, k=8)
    assert seen == [{"app": ["qsiprep", "qsirecon"]}, {"app": "qsiprep"}]


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


def test_answer_scores_routes_a_followup_case_with_its_history(monkeypatch):
    """A replayed follow-up must reach route() with the chat it was asked in,
    so it takes the same (agent) path it took in the app. No LLM is called."""
    import eval.run_eval as run_eval
    from src.router import Decision

    seen = {}

    def fake_route(question, store, config, app, history=None):
        seen["history"] = history
        return Decision("agent", [], "follow-up turn; needs chat context")

    class Result:
        answer = "agent answer"

    monkeypatch.setattr(run_eval.router_mod, "route", fake_route)
    monkeypatch.setattr(run_eval.answer_mod, "_client", lambda: object())
    monkeypatch.setattr(run_eval.answer_mod, "answer_agent",
                        lambda *a, **kw: Result())
    monkeypatch.setattr(run_eval.answer_mod, "answer_oneshot",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("one-shot path must not run")))
    monkeypatch.setattr(run_eval, "judge_answer",
                        lambda case, cand, config, client:
                            {"verdict": "pass", "reason": "ok"})

    history = [{"role": "user", "content": "how do I set the resolution?"},
               {"role": "assistant", "content": "Use --output-resolution."}]
    case = {"source": "feedback", "case_id": 1, "app": "qsiprep",
            "query": "why?", "reference": "because ...", "history": history}
    out = run_eval.answer_scores(store=None, cases=[case], config={}, sample=1)

    assert seen["history"] == history
    assert out["by_path"] == {"agent": {"n": 1, "pass_rate": 1.0}}
    assert out["details"][0]["path"] == "agent"
