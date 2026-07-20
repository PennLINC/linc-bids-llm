from src.sources import docs_source


def test_wanted_filters():
    docs_paths = ["docs/"]
    assert docs_source._wanted("docs/usage.rst", docs_paths)
    assert docs_source._wanted("docs/api/index.md", docs_paths)
    assert docs_source._wanted("README.md", docs_paths)          # top-level rides along
    assert not docs_source._wanted("sub/README.md", docs_paths)  # nested one doesn't
    assert not docs_source._wanted("docs/conf.py", docs_paths)   # code excluded
    assert not docs_source._wanted("qsiprep/workflow.py", docs_paths)
    assert not docs_source._wanted("docs/_static/logo.png", docs_paths)


def test_blob_url_is_tag_pinned_plain():
    url = docs_source.blob_url("PennLINC/qsiprep", "1.0.1", "docs/usage.rst")
    assert url == "https://github.com/PennLINC/qsiprep/blob/1.0.1/docs/usage.rst?plain=1"


def test_fetch_app_builds_records(monkeypatch):
    responses = {
        "/repos/PennLINC/qsiprep/releases/latest": {"tag_name": "1.0.1"},
        "/repos/PennLINC/qsiprep/commits/1.0.1": {"sha": "abc123def456"},
        "/repos/PennLINC/qsiprep/git/trees/abc123def456": {"tree": [
            {"type": "blob", "path": "docs/usage.rst", "sha": "s1", "size": 100},
            {"type": "blob", "path": "qsiprep/cli.py", "sha": "s2", "size": 100},
        ]},
        "/repos/PennLINC/qsiprep/git/blobs/s1": {
            "content": "VXNhZ2UKPT09PT0KUnVuIHFzaXByZXAu"},  # "Usage\n=====\nRun qsiprep."
    }
    monkeypatch.setattr(docs_source.github_api, "get",
                        lambda path, **p: responses[path])
    records = docs_source.fetch_app("qsiprep", {
        "github_repo": "PennLINC/qsiprep", "docs_paths": ["docs/"]})
    assert len(records) == 1
    rec = records[0]
    assert rec["app"] == "qsiprep" and rec["source"] == "docs"
    assert rec["gh_tag"] == "1.0.1" and rec["gh_sha"] == "abc123def456"
    assert rec["url"].endswith("/blob/1.0.1/docs/usage.rst?plain=1")
    assert "Run qsiprep." in rec["text"]
