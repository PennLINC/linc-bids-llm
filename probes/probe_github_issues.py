"""Probe GitHub issues per configured app: counts that calibrate the plan.

Per app: issue count (PRs excluded), comment total, closed / closed-as-completed
(the future `gh_solved` signal), date range — then a `since=` sanity check that
incremental listing works with the token.

    python probes/probe_github_issues.py
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import common  # noqa: E402  (also runs the truststore inject)

API = "https://api.github.com"


def _headers(config) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": common.user_agent(config),
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("note: GITHUB_TOKEN not set — unauthenticated (60 req/hr limit); "
              "a full issue walk will hit it. Set it in .env.", file=sys.stderr)
    return headers


def _get(path: str, headers: dict, **params):
    for attempt in range(4):
        try:
            resp = requests.get(f"{API}{path}", headers=headers,
                                params=params or None, timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        time.sleep(0.05)  # be gentle
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()


def walk_issues(repo: str, headers: dict, since: str | None = None) -> list[dict]:
    """Every real issue (PRs excluded) in the repo, oldest data GitHub has."""
    issues, page = [], 1
    while True:
        params = {"state": "all", "per_page": 100, "page": page}
        if since:
            params["since"] = since
        batch = _get(f"/repos/{repo}/issues", headers, **params)
        if not batch:
            return issues
        issues.extend(o for o in batch if "pull_request" not in o)
        page += 1


def probe_app(app: str, app_cfg: dict, headers: dict) -> None:
    repo = app_cfg["github_repo"]
    print(f"\n== {app} ({repo}) ==")
    issues = walk_issues(repo, headers)
    if not issues:
        print("  !! no issues returned — check the repo name")
        return
    closed = [i for i in issues if i["state"] == "closed"]
    completed = [i for i in closed if i.get("state_reason") == "completed"]
    comments = sum(i.get("comments", 0) for i in issues)
    created = sorted(i["created_at"] for i in issues)
    print(f"  issues: {len(issues)}  (open {len(issues) - len(closed)}, "
          f"closed {len(closed)}, closed-as-completed {len(completed)})")
    print(f"  solved rate (completed/closed): {len(completed) / len(closed):.0%}"
          if closed else "  solved rate: n/a")
    print(f"  comments across issues: {comments}")
    print(f"  created: {created[0][:10]} .. {created[-1][:10]}")

    # since= sanity: the incremental hook Stage 1/2 will rely on.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    recent = walk_issues(repo, headers, since=cutoff)
    stale = [i["number"] for i in recent if i["updated_at"] < cutoff]
    print(f"  since={cutoff[:10]}: {len(recent)} issue(s) updated in the last 30d"
          + (f"  !! {len(stale)} returned despite older updated_at: {stale}" if stale else "  (all pass the updated_at check)"))


def main():
    config = common.load_config()
    headers = _headers(config)
    for app, app_cfg in config["apps"].items():
        probe_app(app, app_cfg, headers)


if __name__ == "__main__":
    main()
