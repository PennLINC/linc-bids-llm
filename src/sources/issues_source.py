"""Issues source: GitHub issues + comments shaped into thread Records.

Threads are question-first, then comments chronologically (attributed, bot
comments dropped, extremely long posts truncated). Code blocks are already
markdown and pass through verbatim — they hold the tracebacks that make
threads findable via BM25.

Dry run (fetches the N most recently updated threads):
    python -m src.sources.issues_source [N]
"""
import sys

from .. import common
from . import github_api

MAX_POST_CHARS = 6000  # truncate pathological walls of text, keep the rest


def list_issues(repo: str, since: str | None = None) -> list[dict]:
    """Every real issue (PRs excluded); `since` = ISO timestamp for incremental."""
    params = {"state": "all"}
    if since:
        params["since"] = since
    return [o for o in github_api.paginate(f"/repos/{repo}/issues", **params)
            if "pull_request" not in o]


def issue_comments(repo: str, number: int) -> list[dict]:
    return github_api.paginate(f"/repos/{repo}/issues/{number}/comments")


def _clip(body: str) -> str:
    body = (body or "").strip()
    if len(body) > MAX_POST_CHARS:
        body = body[:MAX_POST_CHARS] + "\n[... truncated]"
    return body


def thread_text(issue: dict, comments: list[dict]) -> str:
    """One markdown document per thread: title, opening post, then comments."""
    login = (issue.get("user") or {}).get("login", "?")
    status = issue["state"]
    if issue.get("state_reason"):
        status += f" as {issue['state_reason']}"
    parts = [f"# {issue['title']}\n\n"
             f"**{login} opened this issue on {issue['created_at'][:10]} "
             f"(status: {status}):**\n\n{_clip(issue.get('body') or '')}"]
    for c in comments:
        user = c.get("user") or {}
        if user.get("type") == "Bot":
            continue  # codecov / CI chatter carries no diagnostic signal
        body = _clip(c.get("body"))
        if not body:
            continue
        parts.append(f"**{user.get('login', '?')} commented on "
                     f"{c['created_at'][:10]}:**\n\n{body}")
    return "\n\n---\n\n".join(parts)


def thread_record(app: str, repo: str, issue: dict, comments: list[dict]) -> dict:
    return {
        "text": thread_text(issue, comments),
        "app": app,
        "source": "issues",
        "title": issue["title"],
        "url": issue["html_url"],
        "gh_issue": issue["number"],
        "gh_state": issue["state"],
        "gh_solved": bool(issue["state"] == "closed"
                          and issue.get("state_reason") == "completed"),
        "gh_created": issue["created_at"],
        "gh_updated": issue["updated_at"],
        "gh_labels": ",".join(l["name"] for l in issue.get("labels", [])),
    }


def fetch_app(app: str, app_cfg: dict, since: str | None = None,
              limit: int | None = None) -> list[dict]:
    """Thread Records for one app, most recently updated first."""
    repo = app_cfg["github_repo"]
    issues = list_issues(repo, since=since)
    if limit:
        issues = issues[:limit]
    records = []
    for issue in issues:
        # One broken issue must not kill a long harvest.
        try:
            comments = (issue_comments(repo, issue["number"])
                        if issue.get("comments") else [])
            records.append(thread_record(app, repo, issue, comments))
        except Exception as e:
            print(f"  [skip] {repo}#{issue['number']}: {e}", file=sys.stderr)
    print(f"{app} issues: {len(records)} thread(s)"
          + (f" (since {since})" if since else ""), file=sys.stderr)
    return records


def fetch(config: dict | None = None, since: str | None = None) -> list[dict]:
    config = config or common.load_config()
    return [r for app, app_cfg in config["apps"].items()
            for r in fetch_app(app, app_cfg, since=since)]


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    config = common.load_config()
    for app, app_cfg in config["apps"].items():
        for r in fetch_app(app, app_cfg, limit=limit):
            solved = "solved" if r["gh_solved"] else r["gh_state"]
            print(f"\n#{r['gh_issue']} [{solved}] {r['title']}")
            print(f"  {r['url']}")
            print(f"  {common.count_tokens(r['text'])} tokens; head: "
                  f"{r['text'][:120].replace(chr(10), ' ')}...")


if __name__ == "__main__":
    main()
