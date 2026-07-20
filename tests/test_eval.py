"""Tests for the eval scoring logic (pure functions; no network)."""
from eval.run_eval import _reciprocal_rank, _urls, retrieval_scores
from eval.harvest_eval import _stratified_sample


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
