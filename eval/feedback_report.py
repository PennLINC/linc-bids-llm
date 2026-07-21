"""Aggregate collected maintainer feedback into a triage scorecard.

Reads every eval/feedback/*.jsonl (submitted via scripts/submit_feedback.sh),
or your own local log with --local. Reports up/down rates overall and per path,
category counts, and a ranked list of failures with their links — so the lead
can see where the assistant is weak before a wider release.

    python -m eval.feedback_report
    python -m eval.feedback_report --local    # include .feedback/feedback.jsonl
"""
import argparse
import json
from pathlib import Path

FEEDBACK_DIR = Path("eval/feedback")
LOCAL_LOG = Path(".feedback/feedback.jsonl")


def load_entries(paths: list[Path]) -> list[dict]:
    entries = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def summarize(entries: list[dict]) -> dict:
    total = len(entries)
    up = sum(1 for e in entries if e.get("rating") == "up")
    down = sum(1 for e in entries if e.get("rating") == "down")
    by_path: dict = {}
    by_model: dict = {}
    by_category: dict = {}
    for e in entries:
        p = e.get("path", "?")
        slot = by_path.setdefault(p, {"up": 0, "down": 0, "total": 0})
        slot["total"] += 1
        if e.get("rating") in ("up", "down"):
            slot[e["rating"]] += 1
        # Model breakdown: what makes an open-vs-closed comparison readable.
        # Older entries predate provenance capture and land under "(unknown)".
        m = e.get("model") or "(unknown)"
        mslot = by_model.setdefault(m, {"up": 0, "down": 0, "total": 0})
        mslot["total"] += 1
        if e.get("rating") in ("up", "down"):
            mslot[e["rating"]] += 1
        cat = e.get("category")
        if cat:
            by_category[cat] = by_category.get(cat, 0) + 1
    failures = [e for e in entries if e.get("rating") == "down"]
    return {
        "total": total, "up": up, "down": down,
        "by_path": by_path, "by_model": by_model, "by_category": by_category,
        "failures": failures,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="also include your own .feedback/feedback.jsonl")
    args = ap.parse_args()

    paths = sorted(FEEDBACK_DIR.glob("*.jsonl"))
    if args.local:
        paths.append(LOCAL_LOG)
    entries = load_entries(paths)
    if not entries:
        print(f"no feedback found in {FEEDBACK_DIR}/"
              + (" or .feedback/" if args.local else "")
              + " — maintainers submit with scripts/submit_feedback.sh")
        return

    s = summarize(entries)
    rated = s["up"] + s["down"]
    print(f"== feedback ({s['total']} entries, {rated} rated) ==")
    if rated:
        print(f"  thumbs up: {s['up']} ({s['up'] / rated:.0%})   "
              f"down: {s['down']} ({s['down'] / rated:.0%})")
    print("  by path:")
    for p, v in sorted(s["by_path"].items()):
        r = v["up"] + v["down"]
        rate = f"{v['up'] / r:.0%} up" if r else "unrated"
        print(f"    {p:8s} {v['total']:3d} total, {rate}")
    print("  by model:")
    for m, v in sorted(s["by_model"].items()):
        r = v["up"] + v["down"]
        rate = f"{v['up'] / r:.0%} up" if r else "unrated"
        print(f"    {m:22s} {v['total']:3d} total, {rate}")
    if s["by_category"]:
        print("  problem categories:")
        for c, n in sorted(s["by_category"].items(), key=lambda x: -x[1]):
            print(f"    {n:3d}  {c}")
    if s["failures"]:
        print(f"\n  failures ({len(s['failures'])}):")
        for e in s["failures"]:
            q = " ".join((e.get("question") or "").split())[:80]
            print(f"    [{e.get('path','?')}] {q}")
            if e.get("category"):
                print(f"        category: {e['category']}")
            if e.get("correct_url"):
                print(f"        correct:  {e['correct_url']}")
            if e.get("comment"):
                print(f"        note:     {e['comment'][:120]}")


if __name__ == "__main__":
    main()
