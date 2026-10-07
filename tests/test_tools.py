"""Tool tests: search_kb and read_thread against a fake store; grep_code /
read_file against a real on-disk checkout with real ripgrep (rg is a Stage 3
system dependency)."""
import shutil

import pytest

from src import checkouts, ingest
from src.tools import THREAD_MAX_CHARS, Toolbox, describe_call

needs_rg = pytest.mark.skipif(shutil.which("rg") is None,
                              reason="ripgrep not installed")


SOURCE = '''\
import os


def load_eddy_config(eddy_config):
    if not os.path.exists(eddy_config):
        raise ValueError(f'Eddy configuration file {eddy_config} does not exist.')
    if 'cnr_maps' not in eddy_config:
        raise ValueError('Eddy configuration file must contain "cnr_maps" key.')
    return eddy_config
'''


class FakeStore:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)          # chunk records, as the index holds them

    def hybrid_query(self, query, k, where=None, per_doc=None):
        self.where, self.per_doc = where, per_doc
        return [
            {"id": "a", "title": "CUDA out of memory", "source": "issues",
             "url": "https://github.com/PennLINC/qsiprep/issues/42",
             "gh_solved": True, "doc_hits": 3,
             "text": "# CUDA out of memory\n\nRuntimeError: CUDA out of memory ..."},
            {"id": "b", "title": "Usage", "source": "docs", "doc_hits": 1,
             "url": "https://github.com/PennLINC/qsiprep/blob/26.0.0/docs/usage.rst",
             "text": "Run qsiprep with --output-resolution ..."},
        ]

    def doc_chunks(self, where):
        return [c for c in self.chunks if all(c.get(k) == v for k, v in where.items())]


@pytest.fixture
def toolbox(config):
    tag = "26.0.0"
    path = checkouts.checkout_path(config, "qsiprep", tag)
    (path / ".git").mkdir(parents=True)
    (path / "qsiprep" / "utils").mkdir(parents=True)
    (path / "qsiprep" / "utils" / "misc.py").write_text(SOURCE)
    return Toolbox(config, FakeStore(), "qsiprep")


def test_search_kb_formats_and_scopes(toolbox):
    out = toolbox.search_kb("out of memory")
    # scopes to the app plus its pipeline neighbors (qsiprep -> qsirecon)
    assert toolbox.store.where == {"app": ["qsiprep", "qsirecon"]}
    # one result per thread or docs file, saying how much of it matched
    assert toolbox.store.per_doc == 1
    assert "[1] CUDA out of memory — issues (solved, 3 matching parts)" in out
    assert "https://github.com/PennLINC/qsiprep/issues/42" in out
    assert "[2] Usage — docs\n" in out
    # the snippet skips the title line the result already shows
    assert "    RuntimeError: CUDA out of memory" in out

    toolbox.search_kb("x", source_filter="neurostars")
    assert toolbox.store.where == {"app": ["qsiprep", "qsirecon"],
                                   "source": "neurostars"}


@needs_rg
def test_grep_code_literal_match(toolbox):
    out = toolbox.grep_code('must contain "cnr_maps"', version="26.0.0")
    assert "TestOrg/qsiprep@26.0.0" in out
    assert "qsiprep/utils/misc.py:8:" in out          # path made repo-relative
    assert "checkouts" not in out                     # absolute prefix stripped


@needs_rg
def test_grep_code_no_match_reports_cleanly(toolbox):
    out = toolbox.grep_code("this string is not present anywhere", version="26.0.0")
    assert "No matches" in out


@needs_rg
def test_grep_code_literal_vs_regex(toolbox):
    # parens are regex metachars; as a literal this finds the raise lines
    out = toolbox.grep_code("raise ValueError(", version="26.0.0")
    assert "misc.py" in out


def test_read_file_numbers_lines_and_builds_permalink(toolbox):
    out = toolbox.read_file("qsiprep/utils/misc.py", version="26.0.0",
                            start=7, end=8)
    assert "blob/26.0.0/qsiprep/utils/misc.py?plain=1#L7-L8" in out
    assert "7  " in out and "cnr_maps" in out
    # only the requested range, numbered
    assert "import os" not in out


def test_read_file_rejects_path_escape(toolbox):
    out = toolbox.read_file("../../../etc/passwd", version="26.0.0")
    assert "refused" in out.lower()


def test_read_file_missing_file(toolbox):
    out = toolbox.read_file("qsiprep/nope.py", version="26.0.0")
    assert "no such file" in out.lower()


def test_call_dispatch_and_error_capture(toolbox):
    assert "[1]" in toolbox.call("search_kb", {"query": "oom"})
    assert "unknown tool" in toolbox.call("bogus", {})
    # a missing required arg is caught, not raised
    assert "failed" in toolbox.call("read_file", {})


# --- read_thread ----------------------------------------------------------------

def _issue(number, posts, title="eddy fails on b=10 volumes", app="qsiprep"):
    """Chunk records for one issue thread, cut the way ingest cuts them (the
    config fixture's small 100-token windows give a short thread several)."""
    text = f"# {title}\n\n" + "\n\n---\n\n".join(posts)
    rec = {"text": text, "app": app, "source": "issues", "title": title,
           "url": f"https://github.com/TestOrg/qsiprep/issues/{number}",
           "gh_issue": number, "gh_solved": True}
    return text, ingest.chunk_record(rec, 100, 20)


def _posts(n, words=40):
    return [f"**user{i} commented on 2024-01-{i % 28 + 1:02d}:**\n\n"
            + f"reply {i}: " + " ".join(f"w{i}x{j}" for j in range(words))
            for i in range(n)]


def test_read_thread_puts_a_thread_back_together(toolbox):
    text, chunks = _issue(7, _posts(6))
    assert len(chunks) > 2
    toolbox.store.chunks = chunks
    # any form of the URL a browser or the model hands over
    out = toolbox.read_thread("https://github.com/testorg/qsiprep/issues/7#issuecomment-1")
    head, body = out.split("\n\n", 1)
    assert head == ("eddy fails on b=10 volumes — issues (solved)\n"
                    "https://github.com/TestOrg/qsiprep/issues/7\n"
                    f"(all {len(text.splitlines())} lines)")
    # the thread as written: overlap and repeated titles gone, nothing lost
    assert body == text


def test_read_thread_shows_a_long_thread_head_and_tail(toolbox):
    text, chunks = _issue(8, _posts(80))
    assert len(text) > THREAD_MAX_CHARS
    toolbox.store.chunks = chunks
    out = toolbox.read_thread("https://github.com/TestOrg/qsiprep/issues/8")
    assert len(out) < THREAD_MAX_CHARS + 500
    assert "reply 0:" in out and "reply 79:" in out      # the question and the end
    assert "reply 40:" not in out
    marker = next(ln for ln in out.splitlines() if ln.startswith("[... lines "))
    start = int(marker.split("start=")[1].split()[0])
    # paging from the omitted line picks up exactly where the opening stopped
    page = toolbox.read_thread("https://github.com/TestOrg/qsiprep/issues/8", start=start)
    first = page.split("\n\n", 1)[1].splitlines()[0]
    assert first == text.splitlines()[start - 1]
    assert f"(lines {start}-" in page and "follow: read_thread with start=" in page
    # start=1 is a first read, not page 1: the opening and the end again
    assert toolbox.read_thread("https://github.com/TestOrg/qsiprep/issues/8", start=1) == out


def test_read_thread_finds_neurostars_under_a_neighbor_app(toolbox):
    text = "# fieldmaps ignored\n\n**ana asked on 2024-02-01:**\n\nwhy?"
    rec = {"text": text, "app": "qsirecon", "source": "neurostars",
           "title": "fieldmaps ignored", "ns_topic_id": 123, "ns_solved": True,
           "url": "https://neurostars.org/t/fieldmaps-ignored/123"}
    toolbox.store.chunks = ingest.chunk_record(rec, 100, 20)
    out = toolbox.read_thread("https://neurostars.org/t/fieldmaps-ignored/123/4")
    assert out.startswith("fieldmaps ignored — neurostars (solved)\n")
    assert out.endswith(text)


def test_read_thread_explains_what_it_cannot_read(toolbox):
    docs = toolbox.read_thread(
        "https://github.com/TestOrg/qsiprep/blob/26.0.0/docs/usage.rst?plain=1#L3-L9")
    assert "docs page" in docs and "read_file" in docs
    assert "not in the knowledge base" in toolbox.read_thread(
        "https://github.com/TestOrg/qsiprep/issues/999")
    assert "not in the knowledge base" in toolbox.read_thread(   # repo not configured
        "https://github.com/someone/else/issues/1")
    assert "neither" in toolbox.read_thread("https://example.org/page")
    assert "read_thread failed" in toolbox.call("read_thread", {})   # missing url


def test_describe_call_names_the_work():
    assert describe_call("search_kb", {"query": "b=0 missing", "source_filter": "issues"}) \
        == "Searching GitHub issues for “b=0 missing”"
    assert describe_call("read_thread", {"url": "https://neurostars.org/t/x/1"}) \
        == "Reading neurostars.org/t/x/1"
    assert describe_call("grep_code", {"pattern": "b0_threshold", "version": "1.0.1"}) \
        == "Searching the code at 1.0.1 for “b0_threshold”"
    assert describe_call("read_file", {"path": "qsiprep/cli/run.py", "start": 10}) \
        == "Reading qsiprep/cli/run.py, lines 10–end"
    assert describe_call("search_kb", {"query": "x" * 100}).endswith("…”")
