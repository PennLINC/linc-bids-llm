"""Feedback plumbing for the UI: local JSONL log + prefilled GitHub issue URL.

Kept separate from app.py so it's importable (and testable) without Streamlit.
Feedback is the tuning signal for Stage 6 — it's wired before anything else
touches the app.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

FEEDBACK_DIR = Path(".feedback")


def log_feedback(entry: dict, directory: Path = FEEDBACK_DIR) -> Path:
    """Append one feedback entry to a local JSONL file. Returns the file path.

    Entries stay on the member's machine (.feedback/ is gitignored); the
    maintainer collects them ad hoc. Each line is self-contained JSON.
    """
    directory.mkdir(exist_ok=True)
    path = directory / "feedback.jsonl"
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return path


def issue_url(repo: str, question: str, answer_md: str, context: str) -> str:
    """Prefilled new-issue URL for 'this answer was wrong' reports.

    Keeps the body well under browser URL limits by truncating the answer.
    `context` records the app + which path answered (e.g. "qsiprep / agent").
    """
    title = f"[answer report] {question[:80]}"
    body = (
        f"**Context:** {context}\n\n"
        f"**Question:**\n{question}\n\n"
        f"**Answer (truncated):**\n\n{answer_md[:1500]}\n\n"
        "**What's wrong / what did you expect?**\n\n"
    )
    return f"https://github.com/{repo}/issues/new?" + urlencode(
        {"title": title, "body": body})
