import pytest

from src import ingest


DOC = {
    "text": "# Title\n\n" + "\n".join(f"line {i} words words words" for i in range(80)),
    "app": "qsiprep", "source": "docs", "title": "usage (qsiprep 1.0)",
    "url": "https://github.com/PennLINC/qsiprep/blob/1.0/docs/usage.rst?plain=1",
    "gh_repo": "PennLINC/qsiprep", "gh_path": "docs/usage.rst",
    "gh_tag": "1.0", "gh_sha": "abc",
}

THREAD = {
    "text": "# CUDA out of memory\n\n" + "\n".join(
        f"body line {i} with several words" for i in range(80)),
    "app": "qsiprep", "source": "issues", "title": "CUDA out of memory",
    "url": "https://github.com/PennLINC/qsiprep/issues/42", "gh_issue": 42,
}


def test_chunk_record_docs_line_anchors():
    chunks = ingest.chunk_record(DOC, 60, 10)
    assert len(chunks) > 1
    for c in chunks:
        assert c["url"].startswith(DOC["url"] + "#L")
        assert c["url"].endswith(f"-L{c['gh_line_end']}")
        assert c["gh_line_start"] <= c["gh_line_end"]
        assert c["app"] == "qsiprep"


def test_chunk_record_thread_keeps_url_and_prefixes_title():
    chunks = ingest.chunk_record(THREAD, 60, 10)
    assert len(chunks) > 1
    for c in chunks:
        assert c["url"] == THREAD["url"]           # no line anchor on threads
        assert "gh_line_start" not in c
    assert chunks[0]["text"].startswith("# CUDA out of memory")
    # later chunks get the title prepended so each is self-describing
    assert chunks[1]["text"].startswith("# CUDA out of memory\n\n")


def test_chunk_ids_stable_unique_and_app_namespaced():
    a = ingest.chunk_record(DOC, 60, 10)
    b = ingest.chunk_record(DOC, 60, 10)
    assert [c["id"] for c in a] == [c["id"] for c in b]   # stable
    assert len({c["id"] for c in a}) == len(a)            # unique per chunk
    # same doc key under a different app must not collide
    other = ingest.chunk_record({**DOC, "app": "aslprep"}, 60, 10)
    assert set(c["id"] for c in a).isdisjoint(c["id"] for c in other)


def test_needs_full_reasons(config):
    good = {
        "embedding_model": config["retrieval"]["embed_model"],
        "chunk_config": {"size_tokens": config["chunk"]["size_tokens"],
                         "overlap_tokens": config["chunk"]["overlap_tokens"]},
        "docs_shas": {}, "issues_since": {}, "ns_bumped": {},
    }
    assert ingest.needs_full(config, good) is None
    assert ingest.needs_full(config, None) == "no previous manifest"
    assert "embedding" in ingest.needs_full(config, {**good, "embedding_model": "x"})
    assert "chunking" in ingest.needs_full(
        config, {**good, "chunk_config": {"size_tokens": 1, "overlap_tokens": 0}})
    legacy = {k: v for k, v in good.items() if k != "ns_bumped"}
    assert "predates" in ingest.needs_full(config, legacy)


def test_fresh_index_dir_creates_and_wipes(tmp_path):
    target = tmp_path / "index"
    ingest.fresh_index_dir(target)
    assert target.is_dir()
    (target / "fts.sqlite").write_text("stub")
    (target / "junk.bin").write_text("old segment")
    ingest.fresh_index_dir(target)
    assert list(target.iterdir()) == []


def test_fresh_index_dir_refuses_non_index(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "thesis.docx").write_text("do not delete")
    with pytest.raises(SystemExit):
        ingest.fresh_index_dir(target)
    assert (target / "thesis.docx").exists()
