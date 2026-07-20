from src import checkouts


def _mk_checkout(config, app, tag):
    """Create a fake on-disk checkout (a .git dir marks it good) with a file."""
    path = checkouts.checkout_path(config, app, tag)
    (path / ".git").mkdir(parents=True)
    (path / "readme.txt").write_text(f"content for {tag}\n")
    return path


def test_parse_version_and_norm():
    assert checkouts.parse_version("v1.2.3") == checkouts.parse_version("1.2.3")
    assert checkouts.parse_version("26.0.0") > checkouts.parse_version("1.1.1")
    assert checkouts.parse_version("not-a-version") is None


def test_release_tags_filters_and_sorts(monkeypatch):
    releases = [
        {"tag_name": "1.1.1", "draft": False, "prerelease": False},
        {"tag_name": "1.1.0", "draft": False, "prerelease": True},   # prerelease
        {"tag_name": "26.0.0", "draft": False, "prerelease": False},
        {"tag_name": "2.0.0", "draft": True, "prerelease": False},   # draft
        {"tag_name": "1.0.2", "draft": False, "prerelease": False},
        {"tag_name": "1.0.0rc1", "draft": False, "prerelease": False},  # rc via version
    ]
    monkeypatch.setattr(checkouts.github_api, "get", lambda path, **p: releases)
    assert checkouts.release_tags("O/qsiprep", 3) == ["26.0.0", "1.1.1", "1.0.2"]


def test_cloned_tags_lists_only_good_checkouts(config):
    _mk_checkout(config, "qsiprep", "26.0.0")
    _mk_checkout(config, "qsiprep", "1.1.1")
    # a partial clone (no .git) must be ignored
    checkouts.checkout_path(config, "qsiprep", "broken").mkdir(parents=True)
    assert checkouts.cloned_tags(config, "qsiprep") == ["1.1.1", "26.0.0"]


def test_resolve_version_mapping(config):
    for tag in ("1.0.2", "1.1.1", "26.0.0"):
        _mk_checkout(config, "qsiprep", tag)

    assert checkouts.resolve_version(config, "qsiprep", "26.0.0")[0] == "26.0.0"  # exact
    assert checkouts.resolve_version(config, "qsiprep", "1.1.0")[0] == "1.0.2"    # nearest older
    assert checkouts.resolve_version(config, "qsiprep", "0.16.1")[0] == "1.0.2"   # predates -> oldest
    assert checkouts.resolve_version(config, "qsiprep", "99.0.0")[0] == "26.0.0"  # newer -> newest available
    assert checkouts.resolve_version(config, "qsiprep", None)[0] == "26.0.0"      # none -> newest
    assert checkouts.resolve_version(config, "qsiprep", "garbage")[0] == "26.0.0" # unparseable -> newest


def test_resolve_version_no_checkouts_raises(config):
    import pytest
    with pytest.raises(FileNotFoundError):
        checkouts.resolve_version(config, "qsiprep", "1.0.0")


def test_ensure_checkout_reuses_existing(config, monkeypatch):
    path = _mk_checkout(config, "qsiprep", "26.0.0")
    called = []
    monkeypatch.setattr(checkouts.subprocess, "run",
                        lambda *a, **k: called.append(a))
    got = checkouts.ensure_checkout(config, "qsiprep", "O/qsiprep", "26.0.0")
    assert got == path
    assert called == []  # already present: no git clone shelled out
