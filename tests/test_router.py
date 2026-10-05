import pytest

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


# --- follow-up turns ------------------------------------------------------------
# The one-shot path takes no chat history, so a short follow-up routed there is
# answered from chunks retrieved for its bare text alone. In a chat with prior
# turns the router must pick the agent, the only path that reads history.

HISTORY = [{"role": "user", "content": "how do I set the output resolution?"},
           {"role": "assistant", "content": "Use --output-resolution ..."}]

FOLLOWUPS = ["can you explain that in more detail?",
             "which of those should I use?",
             "what does that flag do?",
             "why?",
             "does that also apply to multi-shell data?",
             "ok and what about on 1.0.0?"]


class StrictStore(FakeStore):
    """Fails the test if the router queries the index at all."""
    def hybrid_query(self, query, k, where=None):
        raise AssertionError("follow-up turns must not hit retrieval")


def _faq_hit():
    # a docs chunk both retrieval halves agree on: one-shot bait for bare text
    return [{"in_vector": True, "in_bm25": True, "source": "docs"}]


@pytest.mark.parametrize("question", FOLLOWUPS)
def test_route_followup_goes_agent_without_retrieval(question):
    d = router.route(question, StrictStore(_faq_hit()), _cfg(), "qsiprep",
                     history=HISTORY)
    assert d.path == "agent"
    assert d.reason == "follow-up turn; needs chat context"
    assert d.chunks == []


@pytest.mark.parametrize("history", [None, []])
def test_route_first_turn_still_goes_oneshot(history):
    # no prior turns (missing or empty) -> behaviour unchanged
    store = FakeStore(_faq_hit())
    d = router.route("what does that flag do?", store, _cfg(), "qsiprep",
                     history=history)
    assert d.path == "oneshot" and d.chunks is store.results


def test_route_traceback_with_history_keeps_traceback_reason():
    d = router.route("Traceback (most recent call last):\n  File \"x\"",
                     StrictStore([]), _cfg(), "qsiprep", history=HISTORY)
    assert d.path == "agent" and "traceback" in d.reason
