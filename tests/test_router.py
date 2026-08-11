from src import router


def test_looks_like_traceback():
    assert router.looks_like_traceback(
        "Traceback (most recent call last):\n  File \"x.py\", line 3\nValueError: bad")
    assert router.looks_like_traceback('  File "/app/run.py", line 88, in main')
    assert router.looks_like_traceback("I hit a RuntimeError: CUDA oom\nwhile running")
    assert not router.looks_like_traceback("how do I set the output resolution?")
    assert not router.looks_like_traceback("what does the eddy step do?")


def test_is_long_paste():
    assert router.is_long_paste("\n".join(f"line {i}" for i in range(15)))
    assert router.is_long_paste("x" * 1300)
    assert not router.is_long_paste("a short question about qsiprep")


class FakeStore:
    def __init__(self, results):
        self.results = results
        self.where = None

    def hybrid_query(self, query, k, where=None):
        self.where = where
        return self.results


def _cfg():
    return {"retrieval": {"top_k": 8}}


def test_route_traceback_goes_agent():
    store = FakeStore([{"in_vector": True, "in_bm25": True, "gh_solved": True}])
    d = router.route("Traceback (most recent call last):\n  File \"x\"",
                     store, _cfg(), "qsiprep")
    assert d.path == "agent" and "traceback" in d.reason


def test_route_faq_near_duplicate_goes_oneshot():
    store = FakeStore([
        {"in_vector": True, "in_bm25": True, "ns_solved": True, "source": "neurostars"}])
    d = router.route("how do I fix the cnr_maps eddy config error?",
                     store, _cfg(), "qsiprep")
    assert d.path == "oneshot"
    assert d.chunks is store.results          # reused by the oneshot path


def test_route_docs_agreement_goes_oneshot():
    store = FakeStore([{"in_vector": True, "in_bm25": True, "source": "docs"}])
    d = router.route("what is --output-resolution?", store, _cfg(), "qsiprep")
    assert d.path == "oneshot"


def test_route_weak_match_goes_agent():
    # only one retrieval half agrees, and it isn't solved -> not FAQ-shaped
    store = FakeStore([{"in_vector": True, "in_bm25": False, "source": "issues"}])
    d = router.route("why might eddy behave oddly here?", store, _cfg(), "qsiprep")
    assert d.path == "agent" and "no strong FAQ" in d.reason


def test_route_no_results_goes_agent():
    d = router.route("something never seen", FakeStore([]), _cfg(), "qsiprep")
    assert d.path == "agent"


def test_scope_includes_neighbors():
    cfg = {"apps": {"qsiprep": {"neighbors": ["qsirecon"]},
                    "qsirecon": {"neighbors": ["qsiprep"]},
                    "aslprep": {"neighbors": []}}}
    assert router.scope(cfg, "qsiprep") == ["qsiprep", "qsirecon"]
    assert router.scope(cfg, "aslprep") == ["aslprep"]          # no neighbors
    assert router.scope(cfg, "cubids") == ["cubids"]            # app not in config


def test_route_scopes_query_to_neighbors():
    store = FakeStore([{"in_vector": True, "in_bm25": True, "source": "docs"}])
    cfg = {"retrieval": {"top_k": 8},
           "apps": {"qsiprep": {"neighbors": ["qsirecon"]}}}
    router.route("what is --output-resolution?", store, cfg, "qsiprep")
    assert store.where == {"app": ["qsiprep", "qsirecon"]}
