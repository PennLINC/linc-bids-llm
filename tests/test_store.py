"""Store tests run against a real Chroma client + real FTS5 in a tmp dir, with
a fake embedding model so no model download or GPU is involved."""
import hashlib

import pytest

from src import common
from src.store import Store, _fts_match_query


class FakeModel:
    """Deterministic per-text embeddings; the query prefix is stripped so a
    query for a doc's exact text lands on that doc."""

    def encode(self, texts, normalize_embeddings=True, **kw):
        out = []
        for t in texts:
            t = t.removeprefix(common.BGE_QUERY_PREFIX)
            h = hashlib.sha256(t.encode()).digest()
            vec = [b / 255 for b in h[:16]]
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return _Arr(out)


class _Arr(list):
    def tolist(self):
        return list(self)


RECORDS = [
    {"id": "1", "text": "how to run the qsiprep pipeline", "app": "qsiprep",
     "source": "docs", "title": "usage", "url": "u1"},
    {"id": "2", "text": "RuntimeError CUDA out of memory during eddy",
     "app": "qsiprep", "source": "issues", "title": "oom", "url": "u2",
     "gh_issue": 42},
    {"id": "3", "text": "pipeline troubleshooting for fieldmaps",
     "app": "qsiprep", "source": "neurostars", "title": "fmap", "url": "u3",
     "ns_topic_id": 99},
]


@pytest.fixture
def store(config, monkeypatch):
    monkeypatch.setattr(common, "get_embedding_model", lambda: FakeModel())
    s = Store(config)
    s.add(RECORDS)
    return s


def test_add_and_count(store):
    assert store.count() == 3


def test_fts_match_query_is_safe():
    # error-string punctuation and FTS operators must not break the parse
    q = _fts_match_query('RuntimeError: CUDA "out" of memory (OR AND)')
    assert q is not None
    assert '"runtimeerror"' in q and '"cuda"' in q
    assert _fts_match_query("!!! ??") is None  # nothing tokenizable


def test_bm25_catches_exact_error_string(store):
    # A literal error paste the vectors would blur, BM25 nails via the rare token.
    results = store.hybrid_query("CUDA out of memory", k=3)
    assert results[0]["id"] == "2"
    assert results[0]["in_bm25"] is True
    assert "score" in results[0]


def test_hybrid_query_where_scopes_both_halves(store):
    results = store.hybrid_query("pipeline", k=3, where={"source": "docs"})
    assert [r["id"] for r in results] == ["1"]
    results = store.hybrid_query("pipeline", k=3,
                                 where={"app": "qsiprep", "source": "neurostars"})
    assert [r["id"] for r in results] == ["3"]


def test_where_app_list_matches_membership(store, monkeypatch):
    """A list value scopes to app membership (app + neighbors), on both the
    vector ($in) and BM25 (IN) halves."""
    # add a neighbor-app chunk so the set filter has something to include/exclude
    store.add([{"id": "9", "text": "qsirecon reconstruction recon_spec details",
                "app": "qsirecon", "source": "docs", "title": "recon", "url": "u9"}])
    # scoped to qsiprep only: the qsirecon chunk is excluded
    out = store.hybrid_query("reconstruction recon_spec", k=5,
                             where={"app": "qsiprep"})
    assert all(r["id"] != "9" for r in out)
    # scoped to the set {qsiprep, qsirecon}: the neighbor chunk is now reachable
    out = store.hybrid_query("reconstruction recon_spec", k=5,
                             where={"app": ["qsiprep", "qsirecon"]})
    assert any(r["id"] == "9" for r in out)


def test_delete_removes_from_both_indexes(store):
    store.delete({"source": "issues", "gh_issue": 42})
    assert store.count() == 2
    # gone from BM25 too: the exact error string no longer retrieves it
    results = store.hybrid_query("CUDA out of memory", k=3)
    assert all(r["id"] != "2" for r in results)


def test_reset_empties_both(store):
    store.reset()
    assert store.count() == 0
    assert store.hybrid_query("anything", k=3) == []


def test_query_works_from_another_thread(store):
    """Streamlit serves reruns on worker threads; the FTS connection must not
    raise sqlite3.ProgrammingError when queried off the creating thread."""
    import threading

    out = {}

    def worker():
        try:
            out["result"] = store.hybrid_query("CUDA out of memory", k=2)
        except Exception as e:  # ProgrammingError before the check_same_thread fix
            out["error"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "error" not in out, out.get("error")
    assert out["result"][0]["id"] == "2"
