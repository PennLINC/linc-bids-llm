"""Aggregate collected maintainer feedback into a triage scorecard.

Reads every eval/feedback/*.jsonl (submitted via scripts/submit_feedback.sh),
your own local log with --local, and any other log with --log (the hosted app's
feedback.jsonl, pulled off the server). Reports up/down rates overall and per path,
category counts, and a ranked list of failures with their links — so the lead
can see where the assistant is weak before a wider release.

    python -m eval.feedback_report
    python -m eval.feedback_report --local    # include .feedback/feedback.jsonl
    python -m eval.feedback_report --log .feedback/server.jsonl   # hosted app's log
"""
import argparse
import json
import sys
from pathlib import Path

FEEDBACK_DIR = Path("eval/feedback")
LOCAL_LOG = Path(".feedback/feedback.jsonl")


def load_entries(paths: list[Path]) -> list[dict]:
    entries = []
    for path in paths:
        if not path.exists():
            continue
        bad = 0
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
        if bad:  # e.g. a log copy-pasted out of a terminal that wrapped its lines
            print(f"warning: skipped {bad} unparseable line(s) in {path}",
                  file=sys.stderr)
    return entries


def prepare(entries: list[dict]) -> list[dict]:
    """Clean a raw log before it is counted or promoted to cases.

    - One entry per rated answer: a second "Log feedback" click on the same
      answer (a double-click, or the tester revising their rating) supersedes
      the first.
    - No thumb but a problem category picked is a thumbs-down in all but name;
      it is counted as one (marked `implied`) instead of silently dropped.
    - No thumb and nothing else touched is a thumbs-up by the same token: the
      form's default category reads "looked good". (Both predate the app
      requiring a thumb before it will log.)
    """
    latest: dict = {}
    for i, e in enumerate(entries):
        e = dict(e)
        if e.get("rating") is None:
            if e.get("category"):
                e["rating"], e["implied"] = "down", True
            elif not (e.get("comment") or e.get("correct_url")):
                e["rating"], e["implied"] = "up", True
        if e.get("chat_id"):        # pins the exact answer, per tester session
            key = (e.get("session"), e["chat_id"], e.get("turn"))
        elif e.get("question") and e.get("answer"):   # entries before chat_id
            key = (e.get("app"), e["question"], e["answer"])
        else:
            key = i                 # nothing to match on; keep it
        latest.pop(key, None)       # re-insert so order follows the last click
        latest[key] = e
    return list(latest.values())


def collect(args) -> list[dict]:
    """Entries from eval/feedback/*.jsonl plus whatever --local/--log add."""
    paths = sorted(FEEDBACK_DIR.glob("*.jsonl"))
    if args.local:
        paths.append(LOCAL_LOG)
    for extra in map(Path, args.log):
        if not extra.exists():
            raise SystemExit(f"error: no such log: {extra}")
        paths.append(extra)
    return prepare(load_entries(paths))


def add_source_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--local", action="store_true",
                    help="also include your own .feedback/feedback.jsonl")
    ap.add_argument("--log", action="append", default=[], metavar="PATH",
                    help="also include this log, e.g. a copy of the hosted "
                         "app's feedback.jsonl (repeatable)")


def summarize(entries: list[dict]) -> dict:
    total = len(entries)
    up = sum(1 for e in entries if e.get("rating") == "up")
    down = sum(1 for e in entries if e.get("rating") == "down")
    by_app: dict = {}
    by_path: dict = {}
    by_model: dict = {}
    by_category: dict = {}

    def tally(bucket: dict, key, rating):
        slot = bucket.setdefault(key, {"up": 0, "down": 0, "total": 0})
        slot["total"] += 1
        if rating in ("up", "down"):
            slot[rating] += 1

    for e in entries:
        rating = e.get("rating")
        tally(by_app, e.get("app", "?"), rating)
        tally(by_path, e.get("path", "?"), rating)
        # Older entries predate provenance capture and land under "(unknown)".
        tally(by_model, e.get("model") or "(unknown)", rating)
        cat = e.get("category")
        if cat:
            by_category[cat] = by_category.get(cat, 0) + 1
    failures = [e for e in entries if e.get("rating") == "down"]
    # no thumb and no category, but they wrote something: don't lose it
    notes = [e for e in entries if e.get("rating") is None
             and (e.get("comment") or e.get("correct_url"))]
    return {
        "total": total, "up": up, "down": down,
        "implied": sum(1 for e in failures if e.get("implied")),
        "implied_up": sum(1 for e in entries
                          if e.get("implied") and e.get("rating") == "up"),
        "by_app": by_app, "by_path": by_path, "by_model": by_model,
        "by_category": by_category, "failures": failures, "notes": notes,
    }


def _print_entry(e: dict) -> None:
    q = " ".join((e.get("question") or "").split())[:80]
    tags = []
    if e.get("implied"):
        tags.append("no thumb")
    if (e.get("turn") or 1) > 1:
        tags.append(f"follow-up, turn {e['turn']}")
    print(f"    [{e.get('app','?')}/{e.get('path','?')}] {q}"
          + (f"  ({'; '.join(tags)})" if tags else ""))
    if e.get("category"):
        print(f"        category: {e['category']}")
    if e.get("correct_url"):
        print(f"        correct:  {e['correct_url']}")
    if e.get("comment"):
        print(f"        note:     {e['comment'][:120]}")


def main():
    ap = argparse.ArgumentParser()
    add_source_args(ap)
    args = ap.parse_args()

    entries = collect(args)
    if not entries:
        print(f"no feedback found in {FEEDBACK_DIR}/"
              + (" or .feedback/" if args.local else "")
              + " — maintainers submit with scripts/submit_feedback.sh; pass the "
                "hosted app's log with --log")
        return

    s = summarize(entries)
    rated = s["up"] + s["down"]
    print(f"== feedback ({s['total']} entries, {rated} rated) ==")
    if rated:
        print(f"  thumbs up: {s['up']} ({s['up'] / rated:.0%})   "
              f"down: {s['down']} ({s['down'] / rated:.0%})")
    if s["implied"]:
        print(f"  ({s['implied']} of the downs had no thumb, only a problem "
              "category — counted as down)")
    if s["implied_up"]:
        print(f"  ({s['implied_up']} of the ups had no thumb and nothing else "
              "filled in — counted as up)")
    print("  by app:")
    for a, v in sorted(s["by_app"].items()):
        r = v["up"] + v["down"]
        rate = f"{v['up'] / r:.0%} up" if r else "unrated"
        print(f"    {a:10s} {v['total']:3d} total, {rate}")
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
            _print_entry(e)
    if s["notes"]:
        print(f"\n  unrated, but left a note ({len(s['notes'])}):")
        for e in s["notes"]:
            _print_entry(e)


if __name__ == "__main__":
    main()
