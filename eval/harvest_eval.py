"""Build a held-out evaluation set from solved history.

Samples solved cases (closed-as-completed GitHub issues + accepted-answer
NeuroStars threads), stratified old/new so retrieval isn't judged only on
recent phrasing. Each case is stripped to its opening post (the question a user
would actually type); the rest of the thread is kept as the reference the
answer eval judges against, and the thread URL is the gold retrieval target.

    python -m eval.harvest_eval [--per-source 24] [--out eval/heldout.json]

The threads themselves remain in the index — this is known-item retrieval:
"given the question, does the assistant surface the thread that solved it?"
"""
import argparse
import json
import random
from pathlib import Path

from src import common
from src.sources import issues_source, neurostars_source

SEED = 20260720


def _stratified_sample(cases: list[dict], n: int) -> list[dict]:
    """Half from the older half of history, half from the newer — so the set
    isn't dominated by whichever era is more numerous."""
    cases = sorted(cases, key=lambda c: c["created"])
    if len(cases) <= n:
        return cases
    mid = len(cases) // 2
    rng = random.Random(SEED)
    old = rng.sample(cases[:mid], n // 2)
    new = rng.sample(cases[mid:], n - n // 2)
    return old + new


def harvest_issues(app: str, app_cfg: dict, n: int) -> list[dict]:
    repo = app_cfg["github_repo"]
    issues = issues_source.list_issues(repo)
    solved = [i for i in issues
              if i["state"] == "closed" and i.get("state_reason") == "completed"
              and (i.get("body") or "").strip()]
    picked = _stratified_sample(
        [{"issue": i, "created": i["created_at"]} for i in solved], n)
    cases = []
    for item in picked:
        i = item["issue"]
        comments = issues_source.issue_comments(repo, i["number"]) if i.get("comments") else []
        reference = "\n\n".join(
            (c.get("body") or "").strip() for c in comments
            if (c.get("user") or {}).get("type") != "Bot" and (c.get("body") or "").strip())
        cases.append({
            "app": app, "source": "issues", "case_id": i["number"],
            "gold_url": i["html_url"], "created": i["created_at"],
            "title": i["title"],
            "query": issues_source._clip(i.get("body") or ""),
            "reference": reference[:4000],
        })
    return cases


def harvest_neurostars(app: str, app_cfg: dict, n: int) -> list[dict]:
    topics = []
    seen = set()
    for tag in app_cfg.get("neurostars_tags", []):
        for t in neurostars_source.list_topics(tag):
            if t["id"] not in seen and t.get("has_accepted_answer"):
                seen.add(t["id"])
                topics.append(t)
    picked = _stratified_sample(
        [{"topic": t, "created": t.get("created_at", "")} for t in topics], n)
    cases = []
    for item in picked:
        t = item["topic"]
        posts = neurostars_source.fetch_posts(t["id"])
        if not posts:
            continue
        accepted = [p for p in posts[1:] if p.get("accepted_answer")]
        rec = neurostars_source.topic_record(app, t, posts)  # reuse url logic
        cases.append({
            "app": app, "source": "neurostars", "case_id": t["id"],
            "gold_url": rec["url"], "created": t.get("created_at", ""),
            "title": t.get("title", ""),
            "query": neurostars_source.html_to_text(posts[0].get("cooked", "")),
            "reference": "\n\n".join(
                neurostars_source.html_to_text(p.get("cooked", "")) for p in accepted)[:4000],
        })
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=24,
                    help="cases per source per app (default 24)")
    ap.add_argument("--out", default="eval/heldout.json")
    args = ap.parse_args()

    config = common.load_config()
    cases = []
    for app, app_cfg in config["apps"].items():
        print(f"harvesting {app} issues...")
        cases += harvest_issues(app, app_cfg, args.per_source)
        print(f"harvesting {app} neurostars...")
        cases += harvest_neurostars(app, app_cfg, args.per_source)

    cases = [c for c in cases if c["query"].strip()]
    Path(args.out).write_text(json.dumps(cases, indent=2))
    by_source: dict = {}
    for c in cases:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    print(f"\nwrote {len(cases)} cases to {args.out}: {by_source}")


if __name__ == "__main__":
    main()
