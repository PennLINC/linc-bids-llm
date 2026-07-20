"""Incremental sync: docs by tag-sha, issues by updated_at, neurostars by
bumped_at. Sources are monkeypatched; the store is a recording fake."""
from src import ingest
from src.sources import docs_source, issues_source, neurostars_source


class FakeStore:
    def __init__(self):
        self.added, self.deleted = [], []

    def add(self, chunks):
        self.added.extend(chunks)

    def delete(self, where):
        self.deleted.append(where)


# --- docs ---------------------------------------------------------------------

def test_sync_docs_skips_unchanged_replaces_changed(config, monkeypatch):
    monkeypatch.setattr(docs_source, "latest_release_tag", lambda repo: "2.0")
    monkeypatch.setattr(docs_source, "tag_sha", lambda repo, tag: "sha-new")
    monkeypatch.setattr(docs_source, "fetch_app",
                        lambda app, cfg, tag=None: [{
                            "text": "# D\ncontent", "app": app, "source": "docs",
                            "title": "d", "url": "u", "gh_repo": "O/qsiprep",
                            "gh_path": "docs/d.rst", "gh_tag": tag, "gh_sha": "sha-new"}])

    store = FakeStore()
    shas, tags = ingest.sync_docs(config, store, {"qsiprep": "sha-new"}, full=False)
    assert store.added == [] and store.deleted == []      # sha unchanged: skipped
    assert shas == {"qsiprep": "sha-new"} and tags == {"qsiprep": "2.0"}

    store = FakeStore()
    ingest.sync_docs(config, store, {"qsiprep": "sha-old"}, full=False)
    assert {"app": "qsiprep", "source": "docs"} in store.deleted  # replaced
    assert store.added


# --- issues -------------------------------------------------------------------

def test_sync_issues_since_and_per_thread_replace(config, monkeypatch):
    captured = {}

    def fake_fetch(app, cfg, since=None):
        captured["since"] = since
        return [{"text": "# t\nbody", "app": app, "source": "issues",
                 "title": "t", "url": "u", "gh_issue": 7,
                 "gh_updated": "2026-07-10T00:00:00Z"}]

    monkeypatch.setattr(issues_source, "fetch_app", fake_fetch)

    store = FakeStore()
    new_since = ingest.sync_issues(config, store, {"qsiprep": "2026-06-01T00:00:00Z"},
                                   full=False)
    assert captured["since"] == "2026-06-01T00:00:00Z"     # passes prior watermark
    assert {"app": "qsiprep", "source": "issues", "gh_issue": 7} in store.deleted
    assert store.added
    assert new_since["qsiprep"] == "2026-07-10T00:00:00Z"  # advanced to newest


def test_sync_issues_full_passes_no_since(config, monkeypatch):
    captured = {}
    monkeypatch.setattr(issues_source, "fetch_app",
                        lambda app, cfg, since=None: captured.update(since=since) or [])
    store = FakeStore()
    ingest.sync_issues(config, store, {"qsiprep": "2026-06-01T00:00:00Z"}, full=True)
    assert captured["since"] is None
    assert store.deleted == []                             # full rebuild: no deletes


# --- neurostars ---------------------------------------------------------------

def test_sync_neurostars_changed_and_gone(config, monkeypatch):
    topics = [
        {"id": 1, "title": "a", "slug": "a", "bumped_at": "2026-01-01T00:00:00Z",
         "posts_count": 2, "has_accepted_answer": True},
        {"id": 2, "title": "b", "slug": "b", "bumped_at": "2026-07-01T00:00:00Z",
         "posts_count": 3, "has_accepted_answer": False},
    ]
    fetched = []
    monkeypatch.setattr(neurostars_source, "list_topics", lambda tag: topics)
    monkeypatch.setattr(neurostars_source, "fetch_posts",
                        lambda tid: fetched.append(tid) or [
                            {"post_number": 1, "username": "u", "cooked": "<p>hi</p>",
                             "post_type": 1, "accepted_answer": False}])

    store = FakeStore()
    old = {"qsiprep": {"1": "2026-01-01T00:00:00Z",   # unchanged
                       "2": "2026-06-01T00:00:00Z",   # bumped since
                       "9": "2025-01-01T00:00:00Z"}}  # gone
    new_bumped = ingest.sync_neurostars(config, store, old, full=False)

    assert fetched == [2]                              # only the changed topic fetched
    assert {"app": "qsiprep", "source": "neurostars", "ns_topic_id": 2} in store.deleted
    assert {"app": "qsiprep", "source": "neurostars", "ns_topic_id": 9} in store.deleted
    assert new_bumped["qsiprep"] == {"1": "2026-01-01T00:00:00Z",
                                     "2": "2026-07-01T00:00:00Z"}


def test_sync_neurostars_full_fetches_all(config, monkeypatch):
    topics = [{"id": 1, "title": "a", "slug": "a", "bumped_at": "b1",
               "posts_count": 1, "has_accepted_answer": False}]
    fetched = []
    monkeypatch.setattr(neurostars_source, "list_topics", lambda tag: topics)
    monkeypatch.setattr(neurostars_source, "fetch_posts",
                        lambda tid: fetched.append(tid) or [
                            {"post_number": 1, "username": "u", "cooked": "<p>hi</p>",
                             "post_type": 1, "accepted_answer": False}])
    store = FakeStore()
    ingest.sync_neurostars(config, store, {"qsiprep": {"1": "b1"}}, full=True)
    assert fetched == [1]              # full mode ignores the unchanged bumped_at
    assert store.deleted == []
