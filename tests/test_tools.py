"""Tool tests: search_kb against a fake store; grep_code/read_file against a
real on-disk checkout with real ripgrep (rg is a Stage 3 system dependency)."""
import shutil

import pytest

from src import checkouts
from src.tools import Toolbox

pytestmark = pytest.mark.skipif(shutil.which("rg") is None,
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
    def hybrid_query(self, query, k, where=None):
        self.where = where
        return [
            {"id": "a", "title": "CUDA out of memory", "source": "issues",
             "url": "https://github.com/PennLINC/qsiprep/issues/42",
             "gh_solved": True, "text": "RuntimeError: CUDA out of memory ..."},
            {"id": "b", "title": "Usage", "source": "docs",
             "url": "https://github.com/PennLINC/qsiprep/blob/26.0.0/docs/usage.rst",
             "text": "Run qsiprep with --output-resolution ..."},
        ]


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
    assert "[1] CUDA out of memory — issues (solved)" in out
    assert "https://github.com/PennLINC/qsiprep/issues/42" in out
    assert "[2] Usage — docs" in out

    toolbox.search_kb("x", source_filter="neurostars")
    assert toolbox.store.where == {"app": ["qsiprep", "qsirecon"],
                                   "source": "neurostars"}


def test_grep_code_literal_match(toolbox):
    out = toolbox.grep_code('must contain "cnr_maps"', version="26.0.0")
    assert "TestOrg/qsiprep@26.0.0" in out
    assert "qsiprep/utils/misc.py:8:" in out          # path made repo-relative
    assert "checkouts" not in out                     # absolute prefix stripped


def test_grep_code_no_match_reports_cleanly(toolbox):
    out = toolbox.grep_code("this string is not present anywhere", version="26.0.0")
    assert "No matches" in out


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
