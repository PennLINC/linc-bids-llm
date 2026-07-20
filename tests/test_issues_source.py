from src.sources import issues_source


def make_issue(**over):
    issue = {
        "number": 42,
        "title": "CUDA out of memory during eddy",
        "state": "closed",
        "state_reason": "completed",
        "created_at": "2023-05-01T10:00:00Z",
        "updated_at": "2023-05-03T10:00:00Z",
        "html_url": "https://github.com/PennLINC/qsiprep/issues/42",
        "user": {"login": "someuser", "type": "User"},
        "labels": [{"name": "bug"}, {"name": "eddy"}],
        "body": "Traceback:\n```\nRuntimeError: CUDA out of memory\n```",
        "comments": 2,
    }
    issue.update(over)
    return issue


COMMENTS = [
    {"user": {"login": "maintainer", "type": "User"},
     "created_at": "2023-05-02T10:00:00Z",
     "body": "Use --mem-mb or run on CPU."},
    {"user": {"login": "codecov[bot]", "type": "Bot"},
     "created_at": "2023-05-02T11:00:00Z",
     "body": "Coverage report blah"},
    {"user": {"login": "someuser", "type": "User"},
     "created_at": "2023-05-03T10:00:00Z",
     "body": ""},
]


def test_thread_text_shape():
    text = issues_source.thread_text(make_issue(), COMMENTS)
    assert text.startswith("# CUDA out of memory during eddy")
    assert "someuser opened this issue on 2023-05-01 (status: closed as completed)" in text
    assert "RuntimeError: CUDA out of memory" in text  # code block verbatim
    assert "maintainer commented on 2023-05-02" in text
    assert "codecov" not in text          # bots dropped
    assert text.count("---") == 1         # empty comment dropped


def test_thread_text_truncates_walls_of_text():
    issue = make_issue(body="x" * 10_000)
    text = issues_source.thread_text(issue, [])
    assert "[... truncated]" in text
    assert len(text) < 8_000


def test_thread_record_solved_flag():
    rec = issues_source.thread_record("qsiprep", "PennLINC/qsiprep",
                                      make_issue(), COMMENTS)
    assert rec["gh_solved"] is True
    assert rec["app"] == "qsiprep"
    assert rec["source"] == "issues"
    assert rec["gh_labels"] == "bug,eddy"

    not_planned = make_issue(state_reason="not_planned")
    rec = issues_source.thread_record("qsiprep", "PennLINC/qsiprep",
                                      not_planned, [])
    assert rec["gh_solved"] is False

    still_open = make_issue(state="open", state_reason=None)
    rec = issues_source.thread_record("qsiprep", "PennLINC/qsiprep",
                                      still_open, [])
    assert rec["gh_solved"] is False
    assert rec["gh_state"] == "open"


def test_list_issues_excludes_prs(monkeypatch):
    pages = [[make_issue(number=1),
              {**make_issue(number=2), "pull_request": {"url": "..."}}],
             []]
    calls = []

    def fake_get(path, **params):
        calls.append(params)
        return pages[params["page"] - 1]

    monkeypatch.setattr(issues_source.github_api, "get", fake_get)
    issues = issues_source.list_issues("PennLINC/qsiprep")
    assert [i["number"] for i in issues] == [1]

    issues_source.list_issues("PennLINC/qsiprep", since="2024-01-01T00:00:00Z")
    assert calls[-2]["since"] == "2024-01-01T00:00:00Z"
