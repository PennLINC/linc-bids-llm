"""Score the assistant against the held-out set.

    python -m eval.run_eval                     # retrieval only (fast, local)
    python -m eval.run_eval --answers 12        # + LLM-judged answers (costs API)
    python -m eval.run_eval --heldout eval/regression.json   # feedback cases

Retrieval: for each held-out case, query with the opening post and check
whether the gold thread's URL lands in the top-k — reported for hybrid, and for
vector-only and BM25-only so fusion's contribution is visible. Answer eval:
route + answer a sample, judge each against the historical resolution, per path.
The scorecard is the regression gate for any retrieval/prompt/model change.

Feedback cases (eval/feedback_to_cases.py) run through the same scorers: the
tester's correct URL is the gold, and their note on the flagged answer stands in
for the historical resolution. Every retrieval miss is listed with its cause.
"""
import argparse
import json
import random
from pathlib import Path

from src import common
from src import answer as answer_mod
from src import router as router_mod
from src.store import Store

from eval.urls import canon_url


def _urls(records: list[dict]) -> list[str]:
    return [r.get("url", "").split("#")[0] for r in records]  # drop doc line anchors


def _reciprocal_rank(gold: str, urls: list[str]) -> float:
    for i, u in enumerate(urls, 1):
        if u == gold:
            return 1.0 / i
    return 0.0


def _ranked(store, ids: list[str]) -> list[dict]:
    """Records for `ids` in ranked order — `_hydrate` hands them back in
    Chroma's storage order, which would turn MRR into noise."""
    records = store._hydrate(ids)
    return [records[i] for i in ids if i in records]


def retrieval_scores(store, cases: list[dict], k: int,
                     config: dict | None = None) -> dict:
    """hit@k and MRR for hybrid / vector-only / bm25-only, overall + per source.

    Cases without a gold_url (e.g. answer-only feedback cases) are skipped for
    retrieval so they don't deflate the hit-rate. With `config`, each query is
    scoped the way the app scopes it (the app plus its pipeline neighbors);
    without it, to the case's app alone. URLs are compared by canonical key, so
    a gold URL pasted in another form of the same thread still counts."""
    methods = ("hybrid", "vector", "bm25")
    agg = {m: {"rr": [], "hit": []} for m in methods}
    per_source: dict = {}
    per_case = []

    cases = [c for c in cases if c.get("gold_url")]
    for c in cases:
        scope = router_mod.scope(config, c["app"]) if config else c["app"]
        where = {"app": scope}
        gold = canon_url(c["gold_url"])
        ranked = {
            "hybrid": store.hybrid_query(c["query"], k=k, where=where),
            "vector": _ranked(store, store._vector_ids(c["query"], k, where)),
            "bm25": _ranked(store, store._bm25_ids(c["query"], k, where)),
        }

        rr = {m: _reciprocal_rank(gold, [canon_url(u) for u in _urls(ranked[m])])
              for m in methods}
        for m in methods:
            agg[m]["rr"].append(rr[m])
            agg[m]["hit"].append(1.0 if rr[m] > 0 else 0.0)
            per_source.setdefault(c["source"], {m: [] for m in methods})
            per_source[c["source"]][m].append(1.0 if rr[m] > 0 else 0.0)
        per_case.append({
            "case": f"{c['source']}#{c.get('case_id', '?')}", "query": c["query"],
            "gold_url": c["gold_url"], "scope": scope,
            "rank": round(1 / rr["hybrid"]) if rr["hybrid"] else None,
        })

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "n": len(cases),
        "overall": {m: {"hit_rate": mean(agg[m]["hit"]), "mrr": mean(agg[m]["rr"])}
                    for m in methods},
        "per_source": {s: {m: mean(v[m]) for m in methods}
                       for s, v in per_source.items()},
        "cases": per_case,
    }


def _index_apps(store) -> dict[str, set]:
    """Canonical key of every indexed URL -> the apps it is filed under."""
    apps: dict[str, set] = {}
    for meta in store.collection.get(include=["metadatas"])["metadatas"]:
        if meta and meta.get("url"):
            apps.setdefault(canon_url(meta["url"]), set()).add(meta.get("app") or "?")
    return apps


def explain_miss(case: dict, index_apps: dict[str, set]) -> str:
    """Why a gold URL missed the top-k. Only the last is a ranking problem; the
    first two can't be fixed by tuning retrieval at all."""
    filed = index_apps.get(canon_url(case["gold_url"]))
    if not filed:
        return "not in the index (corpus gap, or not a thread/docs URL)"
    scope = case["scope"] if isinstance(case["scope"], list) else [case["scope"]]
    if not filed & set(scope):
        return (f"indexed under {', '.join(sorted(filed))} — outside this "
                f"app's scope ({', '.join(scope)})")
    return "indexed and in scope, but ranked below the top-k"


JUDGE_SYS = (
    "You judge whether a candidate answer to a user's question is consistent "
    "with the historical resolution of that same question. Reply with a JSON "
    "object: {\"verdict\": \"pass\"|\"fail\", \"reason\": \"<one sentence>\"}. "
    "Pass if the candidate reaches substantially the same resolution or correct "
    "actionable guidance as the reference, even if worded differently. Fail if "
    "it contradicts the reference, misses the actual fix, or is empty."
)


# Feedback cases don't carry a resolution to match: the reference is a
# maintainer's note on an answer they thumbed down ("it invented a flag").
JUDGE_SYS_FEEDBACK = (
    "A maintainer rated an earlier answer to a user's question as bad and left "
    "a note on what was wrong or what it should have said. You judge whether a "
    "NEW candidate answer fixes that. Reply with a JSON object: "
    "{\"verdict\": \"pass\"|\"fail\", \"reason\": \"<one sentence>\"}. "
    "Treat the note as ground truth, and read it against the earlier turns of "
    "the chat when they are given: the complaint may be about something said "
    "or asked there. Pass only if the candidate avoids the problem the note "
    "describes and agrees with what it says the answer should be; correct "
    "detail beyond the note is fine. Fail if it repeats the flagged problem "
    "anywhere in the answer, gives substantially the same response as the "
    "flagged answer, contradicts the note, or is empty. If the note is too "
    "vague to tell whether the problem is fixed, fail and say so."
)

# A long agent answer often ends on the offending line ("tell me your version
# and I can..."), so the judge reads answers whole rather than their opening.
ANSWER_CHARS = 12000
TURN_CHARS = 1500


def _clip(text: str, limit: int) -> str:
    """`text` if it fits, else its head and tail around a marked gap."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n"
            f"{text[-half:]}")


def judge_messages(case: dict, candidate: str) -> list[dict]:
    candidate = _clip(candidate, ANSWER_CHARS)
    if case.get("source") == "feedback":
        flagged = _clip(case.get("flagged_answer") or "(not recorded)", ANSWER_CHARS)
        # a follow-up is judged in the chat it was asked in, as it was answered
        # (clipped as a whole too: the opening turn and the latest ones survive)
        earlier = _clip("".join(f"[{m['role']}] {_clip(m['content'], TURN_CHARS)}\n"
                                for m in case.get("history") or []), ANSWER_CHARS)
        return [{"role": "system", "content": JUDGE_SYS_FEEDBACK},
                {"role": "user", "content": (
                    (f"Earlier turns of the chat:\n{earlier}\n" if earlier else "")
                    + f"Question:\n{case['query'][:1500]}\n\n"
                    f"Problem category: {case.get('category') or '(none given)'}\n\n"
                    f"Maintainer's note:\n{case['reference'][:2500]}\n\n"
                    f"Earlier answer that was flagged:\n{flagged}\n\n"
                    f"New candidate answer:\n{candidate}")}]
    return [{"role": "system", "content": JUDGE_SYS},
            {"role": "user", "content": (
                f"Question:\n{case['query'][:1500]}\n\n"
                f"Historical resolution (reference):\n{case['reference'][:2500]}\n\n"
                f"Candidate answer:\n{candidate}")}]


JUDGE_RUNS = 5


def judge_answer(case: dict, candidate: str, config: dict, client) -> dict:
    """Pass only if every one of JUDGE_RUNS verdicts is a pass.

    On a borderline answer (a vague note, a partial fix) a single verdict is
    close to a coin flip, and a gate that flips between runs reports
    regressions that aren't there. A split is failed and labelled as one, so
    it reads as "look at this case yourself" rather than as a result.

    The runs are sampled at the model's default temperature on purpose: at
    temperature 0 they agree with each other and the split never shows, while
    the verdict can still differ the next time the eval is run."""
    messages = judge_messages(case, candidate)
    verdicts = []
    for _ in range(JUDGE_RUNS):
        msg = client.chat.completions.create(
            model=config["llm"]["oneshot_model"], messages=messages,
            max_completion_tokens=300,
        ).choices[0].message
        try:
            verdicts.append(json.loads(msg.content))
        except (json.JSONDecodeError, TypeError):
            verdicts.append({"verdict": "fail", "reason": "unparseable judge output"})
    passes = sum(1 for v in verdicts if v.get("verdict") == "pass")
    if passes == len(verdicts):
        return verdicts[0]
    dissent = next(v for v in verdicts if v.get("verdict") != "pass")
    split = f"[judge split: {passes} of {len(verdicts)} runs passed] " if passes else ""
    return {"verdict": "fail", "reason": split + dissent.get("reason", "")}


def answer_scores(store, cases: list[dict], config: dict, sample: int) -> dict:
    rng = random.Random(20260720)
    # An empty reference gives the judge nothing to compare against.
    cases = [c for c in cases if (c.get("reference") or "").strip()]
    picked = rng.sample(cases, min(sample, len(cases)))
    client = answer_mod._client()
    by_path: dict = {}
    details = []
    for c in picked:
        # a follow-up case routes as it did in the app: with its chat history
        decision = router_mod.route(c["query"], store, config, c["app"],
                                    history=c.get("history"))
        if decision.path == "oneshot":
            cand = answer_mod.answer_oneshot(c["query"], decision.chunks,
                                             c["app"], config, client=client)
        else:
            # a follow-up turn is replayed with the chat it was asked in
            cand = answer_mod.answer_agent(c["query"], c["app"], config, store,
                                           history=c.get("history") or None,
                                           client=client).answer
        verdict = judge_answer(c, cand, config, client)
        by_path.setdefault(decision.path, []).append(verdict["verdict"] == "pass")
        path = decision.path
        if c.get("rated_path") not in (None, path):
            # replayed on a different path than the one the tester rated
            # (the router changed its mind, or they had forced a mode)
            path += f"; rated on {c['rated_path']}"
        details.append({"case": f"{c['source']}#{c['case_id']}",
                        "path": path, "verdict": verdict["verdict"],
                        "reason": verdict["reason"]})

    return {
        "n": len(picked),
        "by_path": {p: {"n": len(v), "pass_rate": sum(v) / len(v)}
                    for p, v in by_path.items()},
        "details": details,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heldout", default="eval/heldout.json")
    ap.add_argument("--answers", type=int, default=0,
                    help="also judge answers for N sampled cases (costs API)")
    args = ap.parse_args()

    config = common.load_config()
    k = config["retrieval"]["top_k"]
    cases = json.loads(Path(args.heldout).read_text())
    store = Store(config)

    r = retrieval_scores(store, cases, k, config)
    skipped = len(cases) - r["n"]
    print(f"== retrieval (n={r['n']}, k={k}) =="
          + (f"  [{skipped} case(s) have no gold URL — not scored]" if skipped else ""))
    if r["n"]:
        for m, s in r["overall"].items():
            print(f"  {m:7s}  hit@{k}={s['hit_rate']:.0%}  MRR={s['mrr']:.3f}")
        print("  per source (hit@k):")
        for src, ms in r["per_source"].items():
            print(f"    {src:11s} " + "  ".join(f"{m}={v:.0%}" for m, v in ms.items()))
        misses = [c for c in r["cases"] if c["rank"] is None]
        if misses:
            # a regression gate has to name the cases that fail it
            index_apps = _index_apps(store)
            print(f"  misses (hybrid, {len(misses)}):")
            for c in misses:
                print(f"    {c['case']}  {' '.join(c['query'].split())[:60]}")
                print(f"        gold: {c['gold_url']}")
                print(f"        why:  {explain_miss(c, index_apps)}")
    else:
        print("  nothing to score — retrieval needs cases with a gold URL")

    if args.answers:
        print(f"\n== answers (judged, sample={args.answers}) ==")
        a = answer_scores(store, cases, config, args.answers)
        if not a["n"]:
            print("  nothing to judge — no case carries a reference")
        for path, s in a["by_path"].items():
            print(f"  {path:8s} pass={s['pass_rate']:.0%} (n={s['n']})")
        for d in a["details"]:
            print(f"    [{d['verdict']:4s}] {d['case']} ({d['path']}): {d['reason']}")
    store.close()


if __name__ == "__main__":
    main()
