"""Promote thumbs-down feedback into regression eval cases.

Each failure a maintainer flagged becomes a case in eval/regression.json, in the
same schema as the held-out set, so `python -m eval.run_eval --heldout
eval/regression.json` scores whether later changes fix it (and nothing else
regresses). Unlike the Stage 6 held-out set — which judges against stale
historical fixes — these references are maintainer-verified current truth.

    python -m eval.feedback_to_cases            # merge into eval/regression.json
    python -m eval.feedback_to_cases --local    # include your local .feedback log
    python -m eval.feedback_to_cases --log .feedback/server.jsonl   # hosted app's log

A case needs a query and either a correct_url (for retrieval scoring) or a
comment/answer reference (for answer scoring); entries with neither are skipped.
"""
import argparse
import hashlib
import json
from pathlib import Path

from eval.feedback_report import add_source_args, collect
from eval.urls import canon_url

REGRESSION = Path("eval/regression.json")


def _case_id(entry: dict) -> str:
    # canonical URL, so the same thread pasted in two forms is still one case;
    # app, so the same question on two tools' cards is two
    key = "|".join((entry.get("app") or "", entry.get("question", ""),
                    canon_url(entry.get("correct_url") or ""))).encode()
    return "fb-" + hashlib.sha1(key).hexdigest()[:12]


def entries_to_cases(entries: list[dict]) -> list[dict]:
    """down-rated entries with a usable target -> eval cases (deduped)."""
    cases: dict[str, dict] = {}
    for e in entries:
        if e.get("rating") != "down":
            continue
        query = (e.get("question") or "").strip()
        reference = (e.get("comment") or "").strip()
        gold = (e.get("correct_url") or "").strip()
        if not query or (not gold and not reference):
            continue  # nothing to score against
        cid = _case_id(e)
        cases[cid] = {
            "app": e.get("app", "qsiprep"),
            "source": "feedback",
            "case_id": cid,
            "gold_url": gold,
            "created": e.get("ts", ""),
            "title": query[:60],
            "query": query,
            "reference": reference,
            "category": e.get("category"),
            # what the judge needs to tell "fixed" from "same mistake again",
            # and what a follow-up turn needs to be replayed in context
            "rated_path": e.get("path"),
            "flagged_answer": e.get("answer") or "",
            "history": e.get("history") or [],
        }
    return list(cases.values())


def merge_cases(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union by case_id; newer feedback overwrites an earlier case."""
    by_id = {c["case_id"]: c for c in existing}
    for c in new:
        by_id[c["case_id"]] = c
    return sorted(by_id.values(), key=lambda c: c.get("created", ""))


def main():
    ap = argparse.ArgumentParser()
    add_source_args(ap)
    ap.add_argument("--out", default=str(REGRESSION))
    args = ap.parse_args()

    new = entries_to_cases(collect(args))

    out = Path(args.out)
    existing = json.loads(out.read_text()) if out.exists() else []
    merged = merge_cases(existing, new)
    out.write_text(json.dumps(merged, indent=2))

    with_url = sum(1 for c in merged if c["gold_url"])
    print(f"wrote {len(merged)} case(s) to {out} "
          f"(+{len(merged) - len(existing)} new; {with_url} retrieval-scorable, "
          f"{len(merged) - with_url} answer-only)")
    print("score with: python -m eval.run_eval --heldout " + str(out))


if __name__ == "__main__":
    main()
