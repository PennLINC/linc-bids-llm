import json
from urllib.parse import parse_qs, urlparse

from src.feedback import issue_url, log_feedback


def test_log_feedback_appends_jsonl(tmp_path):
    log_feedback({"rating": "up", "comment": "nice"}, directory=tmp_path)
    path = log_feedback({"rating": "down", "comment": "wrong"}, directory=tmp_path)
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["rating"] == "up"
    assert lines[1]["comment"] == "wrong"
    assert all("ts" in l for l in lines)


def test_issue_url_prefills_and_truncates():
    url = issue_url("Org/repo", "why?" * 100, "answer " * 1000, "qsiprep / agent")
    parsed = urlparse(url)
    assert parsed.netloc == "github.com"
    assert parsed.path == "/Org/repo/issues/new"
    q = parse_qs(parsed.query)
    assert q["title"][0].startswith("[answer report] why?")
    assert len(q["title"][0]) <= 100
    assert "qsiprep / agent" in q["body"][0]
    assert len(q["body"][0]) < 2500  # answer truncated, URL stays reasonable
