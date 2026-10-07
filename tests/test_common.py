import random

import pytest

from src import common

SAMPLE = """\
# Title

Intro paragraph with some words.

## Section one

- item one
- item two

## Section two

Body text here that goes on a little while so chunks have substance.
More body text on another line.
"""


def test_chunk_line_ranges_are_exact():
    lines = SAMPLE.splitlines()
    for chunk, start, end in common.chunk_text(SAMPLE, 50, 10):
        chunk_lines = chunk.splitlines()
        assert chunk_lines[0] == lines[start - 1]
        assert chunk_lines[-1] == lines[end - 1]
        assert start <= end


def test_chunk_splits_at_headings():
    chunks = common.chunk_text(SAMPLE, 30, 5)
    first_lines = [c.splitlines()[0] for c, _, _ in chunks]
    assert "## Section one" in first_lines
    assert "## Section two" in first_lines


def test_chunk_windows_overlap():
    text = "\n".join(f"line {i} with several words of filler text" for i in range(60))
    chunks = common.chunk_text(text, 80, 30)
    assert len(chunks) >= 2
    for (_, s1, e1), (_, s2, e2) in zip(chunks, chunks[1:]):
        assert s2 <= e1  # next window starts inside the previous one


def test_chunk_edge_cases():
    assert common.chunk_text("", 100, 10) == []
    assert common.chunk_text("\n\n\n", 100, 10) == []
    # a single line longer than the budget still becomes one (oversized) chunk
    out = common.chunk_text("x " * 2000, 100, 10)
    assert len(out) == 1


def test_chunk_no_shrinking_copies_before_an_oversized_line():
    # Short lines, then one line too long for any window (a log pasted on a
    # single line, as in qsiprep#703). The window that stops short of it used
    # to be followed by ever-shorter copies of its own tail, one per line,
    # before the long line got a chunk of its own.
    text = "\n".join([f"short line {i}" for i in range(6)] + ["log " * 400])
    spans = [(s, e) for _, s, e in common.chunk_text(text, 60, 20)]
    assert spans == [(1, 6), (7, 7)]


def test_chunk_overlap_shrinks_to_fit_a_long_line():
    # A line that fits a window on its own but not after the full overlap:
    # keep as much overlap as still fits with it, and move past it.
    long = " ".join(["word"] * 50)
    size, overlap = 60, 20
    assert size - overlap < common.count_tokens(long + "\n") <= size
    text = "\n".join([f"short line {i}" for i in range(6)] + [long])
    spans = [(s, e) for _, s, e in common.chunk_text(text, size, overlap)]
    assert spans[0] == (1, 6)
    assert spans[-1][1] == 7 and spans[-1][0] < 7   # the long line, with overlap
    assert len(spans) == 2


@pytest.mark.parametrize("seed", range(40))
def test_chunk_windows_always_advance(seed):
    # Random mixes of short lines, long lines, blank lines and headings. Each
    # chunk must start and end past the one before it, so none repeats lines
    # that are all in its neighbor; every non-blank line must still land in
    # a chunk, with its exact text.
    rng = random.Random(seed)
    lines = []
    for _ in range(rng.randint(5, 80)):
        roll = rng.random()
        if roll < 0.08:
            lines.append(f"## heading {len(lines)}")
        elif roll < 0.25:
            lines.append("")
        elif roll < 0.4:
            lines.append("x " * rng.randint(15, 90))   # may not fit any window
        else:
            lines.append(" ".join(["w"] * rng.randint(1, 10)))
    chunks = common.chunk_text("\n".join(lines), 40, 15)
    for (_, s1, e1), (_, s2, e2) in zip(chunks, chunks[1:]):
        assert s2 > s1 and e2 > e1
    for text, s, e in chunks:
        assert text == "\n".join(lines[s - 1:e])
    covered = {n for _, s, e in chunks for n in range(s, e + 1)}
    assert all(n in covered for n, line in enumerate(lines, 1) if line.strip())


def test_chunk_never_exceeds_budget_except_single_lines():
    chunks = common.chunk_text(SAMPLE, 60, 10)
    for chunk, _, _ in chunks:
        if len(chunk.splitlines()) > 1:
            assert common.count_tokens(chunk) <= 60 + 10  # small slack for joins


def test_count_tokens():
    assert common.count_tokens("") == 0
    assert common.count_tokens("hello world") >= 2


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "NEWVAR=abc\n"
        "QUOTED='xyz'\n"
        "EXISTING=file-value\n"
        "not a kv line\n"
    )
    monkeypatch.delenv("NEWVAR", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.setenv("EXISTING", "env-value")
    common.load_dotenv(env)
    import os
    assert os.environ["NEWVAR"] == "abc"
    assert os.environ["QUOTED"] == "xyz"       # quotes stripped
    assert os.environ["EXISTING"] == "env-value"  # existing env wins


def test_load_config_explicit_path(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("chunk:\n  size_tokens: 123\n")
    common.load_config.cache_clear()
    loaded = common.load_config(str(cfg))
    assert loaded["chunk"]["size_tokens"] == 123
    common.load_config.cache_clear()


def test_load_config_missing_path_fails(tmp_path):
    common.load_config.cache_clear()
    with pytest.raises(FileNotFoundError):
        common.load_config(str(tmp_path / "nope.yaml"))
    common.load_config.cache_clear()


def test_load_config_index_path_override(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("index:\n  path: ./index\n")
    common.load_config.cache_clear()
    monkeypatch.setenv("BIDS_INDEX_PATH", "index.staging")
    loaded = common.load_config(str(cfg))
    assert loaded["index"]["path"] == "index.staging"   # env wins (refresh staging)
    common.load_config.cache_clear()
    monkeypatch.delenv("BIDS_INDEX_PATH")
    assert common.load_config(str(cfg))["index"]["path"] == "./index"
    common.load_config.cache_clear()


@pytest.mark.parametrize("seed", range(40))
def test_stitch_chunks_puts_the_document_back(seed):
    # The same random mixes as above, with every line distinct so no stretch of
    # the document repeats another: stitching the chunks gives back exactly the
    # original's lines, give or take blank ones where two chunks met.
    rng = random.Random(seed)
    lines = []
    for n in range(rng.randint(5, 80)):
        roll = rng.random()
        if roll < 0.08:
            lines.append(f"## heading {n}")
        elif roll < 0.25:
            lines.append("")
        elif roll < 0.4:
            lines.append(f"long{n} " + "x " * rng.randint(15, 90))
        else:
            lines.append(f"l{n} " + " ".join(["w"] * rng.randint(1, 10)))
    text = "\n".join(lines).strip("\n")
    chunks = [c for c, _, _ in common.chunk_text(text, 40, 15)]
    stitched = common.stitch_chunks(chunks, 15)
    assert [ln for ln in stitched.split("\n") if ln.strip()] \
        == [ln for ln in text.split("\n") if ln.strip()]


def test_stitch_chunks_is_exact_when_blank_lines_are_single():
    text = "\n\n".join(f"paragraph {i}: " + "word " * 25 for i in range(10))
    chunks = [c for c, _, _ in common.chunk_text(text, 60, 10)]
    assert len(chunks) > 3
    assert common.stitch_chunks(chunks, 10) == text


def test_stitch_chunks_does_not_eat_repeated_lines():
    # A log that repeats one line: where two chunks meet, the overlap is as
    # long as it really is, not as long as the run of identical lines.
    text = "\n".join(["start"] + ["same line again"] * 30 + ["end"])
    chunks = [c for c, _, _ in common.chunk_text(text, 40, 10)]
    assert common.stitch_chunks(chunks, 10) == text


def test_scope_includes_neighbors():
    cfg = {"apps": {"qsiprep": {"neighbors": ["qsirecon"]},
                    "qsirecon": {"neighbors": ["qsiprep"]},
                    "aslprep": {"neighbors": []}}}
    assert common.scope(cfg, "qsiprep") == ["qsiprep", "qsirecon"]
    assert common.scope(cfg, "aslprep") == ["aslprep"]          # no neighbors
    assert common.scope(cfg, "cubids") == ["cubids"]            # app not in config


def test_chunk_ids_follow_the_document_key():
    rec = {"app": "qsiprep", "source": "issues", "gh_issue": 703}
    assert common.doc_key(rec) == "qsiprep:issues:#703"
    assert common.doc_key({"app": "qsiprep", "source": "neurostars",
                           "ns_topic_id": 9}) == "qsiprep:neurostars:ns:9"
    assert common.doc_key({"app": "qsiprep", "source": "docs",
                           "gh_path": "docs/usage.rst"}) == "qsiprep:docs:docs/usage.rst"
    # ids already in built indexes: changing them would orphan every chunk
    assert common.chunk_id("qsiprep:issues:#703", 0) \
        == "214640d474766a89bf67ff603a259045d3ae3f8e"
