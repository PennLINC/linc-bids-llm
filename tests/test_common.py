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
