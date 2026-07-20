import pytest


class Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


@pytest.fixture
def resp():
    return Resp


@pytest.fixture
def config(tmp_path):
    """A full config dict pointing index + checkouts at tmp dirs."""
    return {
        "contact_email": "test@example.edu",
        "apps": {
            "qsiprep": {
                "github_repo": "TestOrg/qsiprep",
                "docs_paths": ["docs/"],
                "neurostars_tags": ["qsiprep"],
                "neurostars_search": [],
                "checkout_tags": 2,
                "neighbors": [],
            },
        },
        "retrieval": {"top_k": 8, "candidates": 40,
                      "embed_model": "BAAI/bge-small-en-v1.5"},
        "chunk": {"size_tokens": 100, "overlap_tokens": 20},
        "llm": {"oneshot_model": "gpt-test", "agent_model": "gpt-test-big",
                "max_output_tokens": 500, "max_tool_iterations": 4},
        "index": {"path": str(tmp_path / "index")},
        "checkouts": {"path": str(tmp_path / "checkouts")},
        "feedback": {"github_repo": ""},
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry/backoff tests shouldn't actually sleep."""
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
