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


# --- grouping by document ---------------------------------------------------------

def _thread_chunks(n, gh_issue, title):
    """n chunk records of one issue thread, all about eddy (so all match)."""
    return [{"id": f"t{gh_issue}-{i}", "text": f"eddy eddy eddy part {i} of {title}",
             "app": "qsiprep", "source": "issues", "title": title,
             "url": f"https://github.com/PennLINC/qsiprep/issues/{gh_issue}",
             "gh_issue": gh_issue} for i in range(n)]


def test_per_doc_keeps_one_long_thread_from_filling_the_top_k(store):
    store.add(_thread_chunks(6, 703, "b0 threshold") + _thread_chunks(2, 747, "eddy gpu"))
    # 9 chunks mention eddy, from 3 documents: the top 4 must repeat one
    flat = store.hybrid_query("eddy", k=4)
    assert len({r["url"] for r in flat}) < len(flat)
    grouped = store.hybrid_query("eddy", k=4, per_doc=1)
    urls = [r["url"] for r in grouped]
    assert len(urls) == len(set(urls))                       # one chunk per thread
    assert "https://github.com/PennLINC/qsiprep/issues/747" in urls
    hits = {r["url"]: r["doc_hits"] for r in grouped}
    assert hits["https://github.com/PennLINC/qsiprep/issues/703"] == 6
    assert hits["https://github.com/PennLINC/qsiprep/issues/747"] == 2


def test_per_doc_groups_docs_chunks_by_file():
    from src.store import doc_url
    assert doc_url({"url": "https://x/blob/1.0/docs/a.rst?plain=1#L1-L9"}) \
        == doc_url({"url": "https://x/blob/1.0/docs/a.rst?plain=1#L10-L30"})
    assert doc_url({"id": "z"}) == "z"                        # no url: the chunk alone


def test_doc_chunks_come_back_in_order(store):
    from src import ingest
    rec = {"text": "\n\n".join(f"paragraph {i} " + "word " * 30 for i in range(12)),
           "app": "qsiprep", "source": "issues", "title": "long one",
           "url": "https://github.com/PennLINC/qsiprep/issues/5", "gh_issue": 5}
    chunks = ingest.chunk_record(rec, 60, 10)
    assert len(chunks) > 3
    store.add(list(reversed(chunks)))                         # stored back to front
    got = store.doc_chunks({"app": "qsiprep", "source": "issues", "gh_issue": 5})
    assert [c["id"] for c in got] == [c["id"] for c in chunks]
    assert store.doc_chunks({"app": "qsiprep", "source": "issues", "gh_issue": 6}) == []
