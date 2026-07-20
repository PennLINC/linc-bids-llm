"""Probe NeuroStars (Discourse) tag coverage per configured app.

Per tag: topic count, solved rate (accepted-answer plugin), reply volume,
newest/oldest activity — and a flag for tags that return nothing, which would
mean falling back to the `neurostars_search` terms.

    python probes/probe_neurostars.py
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import common  # noqa: E402  (also runs the truststore inject)

BASE = "https://neurostars.org"
SLEEP = 1.0  # Discourse etiquette: ~1 req/s


def _get(path: str, headers: dict, **params) -> dict | None:
    """GET a Discourse JSON endpoint; None on 404 (tag doesn't exist)."""
    for attempt in range(4):
        try:
            resp = requests.get(f"{BASE}{path}", headers=headers,
                                params=params or None, timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        time.sleep(SLEEP)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "30"))
            print(f"  429; honoring Retry-After: {retry}s", file=sys.stderr)
            time.sleep(retry)
            continue
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()


def walk_tag(tag: str, headers: dict) -> list[dict] | None:
    """All topic summaries for a tag (the paged 'latest' listing), or None."""
    topics, page = [], 0
    while True:
        data = _get(f"/tag/{tag}/l/latest.json", headers, page=page)
        if data is None:
            return None if page == 0 else topics
        batch = data.get("topic_list", {}).get("topics", [])
        if not batch:
            return topics
        topics.extend(batch)
        page += 1


def probe_tag(tag: str, headers: dict) -> None:
    topics = walk_tag(tag, headers)
    if not topics:
        print(f"  tag '{tag}': !! NOTHING RETURNED — fall back to neurostars_search terms")
        return
    solved = [t for t in topics if t.get("has_accepted_answer")]
    replies = sum(max(t.get("posts_count", 1) - 1, 0) for t in topics)
    created = sorted(t["created_at"] for t in topics if t.get("created_at"))
    bumped = sorted(t["bumped_at"] for t in topics if t.get("bumped_at"))
    print(f"  tag '{tag}': {len(topics)} topics, "
          f"{len(solved)} solved ({len(solved) / len(topics):.0%}), "
          f"{replies} replies")
    print(f"    oldest created {created[0][:10]}, newest activity {bumped[-1][:10]}")


def probe_search(term: str, headers: dict) -> None:
    data = _get("/search.json", headers, q=term)
    n = len((data or {}).get("topics", []))
    print(f"  search '{term}': {n} topic(s) on first page"
          + ("  !! nothing found" if n == 0 else ""))


def main():
    config = common.load_config()
    headers = {"User-Agent": common.user_agent(config)}
    for app, app_cfg in config["apps"].items():
        print(f"\n== {app} ==")
        for tag in app_cfg.get("neurostars_tags", []):
            probe_tag(tag, headers)
        for term in app_cfg.get("neurostars_search", []):
            probe_search(term, headers)


if __name__ == "__main__":
    main()
