"""Score the assistant against the held-out set.

    python -m eval.run_eval                     # retrieval only (fast, local)
    python -m eval.run_eval --answers 12        # + LLM-judged answers (costs API)

Retrieval: for each held-out case, query with the opening post and check
whether the gold thread's URL lands in the top-k — reported for hybrid, and for
vector-only and BM25-only so fusion's contribution is visible. Answer eval:
route + answer a sample, judge each against the historical resolution, per path.
The scorecard is the regression gate for any retrieval/prompt/model change.
"""
import argparse
import json
import random
from pathlib import Path

from src import common
from src import answer as answer_mod
from src import router as router_mod
from src.store import Store


def _urls(records: list[dict]) -> list[str]:
    return [r.get("url", "").split("#")[0] for r in records]  # drop doc line anchors


def _reciprocal_rank(gold: str, urls: list[str]) -> float:
    for i, u in enumerate(urls, 1):
        if u == gold:
            return 1.0 / i
    return 0.0


def retrieval_scores(store, cases: list[dict], k: int) -> dict:
    """hit@k and MRR for hybrid / vector-only / bm25-only, overall + per source."""
    methods = ("hybrid", "vector", "bm25")
    agg = {m: {"rr": [], "hit": []} for m in methods}
    per_source: dict = {}

    for c in cases:
        where = {"app": c["app"]}
        hy = store.hybrid_query(c["query"], k=k, where=where)
        vec = store._hydrate(store._vector_ids(c["query"], k, where))
        vec_ranked = list(vec.values())
        bm = store._hydrate(store._bm25_ids(c["query"], k, where))
        bm_ranked = list(bm.values())
        ranked = {"hybrid": hy, "vector": vec_ranked, "bm25": bm_ranked}

        for m in methods:
            rr = _reciprocal_rank(c["gold_url"], _urls(ranked[m]))
            agg[m]["rr"].append(rr)
            agg[m]["hit"].append(1.0 if rr > 0 else 0.0)
            per_source.setdefault(c["source"], {m: [] for m in methods})
            if m == "hybrid":
                per_source[c["source"]][m].append(1.0 if rr > 0 else 0.0)
            else:
                per_source[c["source"]][m].append(1.0 if rr > 0 else 0.0)

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "n": len(cases),
        "overall": {m: {"hit_rate": mean(agg[m]["hit"]), "mrr": mean(agg[m]["rr"])}
                    for m in methods},
        "per_source": {s: {m: mean(v[m]) for m in methods}
                       for s, v in per_source.items()},
    }


JUDGE_SYS = (
    "You judge whether a candidate answer to a user's question is consistent "
    "with the historical resolution of that same question. Reply with a JSON "
    "object: {\"verdict\": \"pass\"|\"fail\", \"reason\": \"<one sentence>\"}. "
    "Pass if the candidate reaches substantially the same resolution or correct "
    "actionable guidance as the reference, even if worded differently. Fail if "
    "it contradicts the reference, misses the actual fix, or is empty."
)


def judge_answer(question: str, reference: str, candidate: str, config: dict,
                 client) -> dict:
    content = (f"Question:\n{question[:1500]}\n\n"
               f"Historical resolution (reference):\n{reference[:2500]}\n\n"
               f"Candidate answer:\n{candidate[:2500]}")
    msg = client.chat.completions.create(
        model=config["llm"]["oneshot_model"],
        messages=[{"role": "system", "content": JUDGE_SYS},
                  {"role": "user", "content": content}],
        max_completion_tokens=300,
    ).choices[0].message
    try:
        return json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return {"verdict": "fail", "reason": "unparseable judge output"}


def answer_scores(store, cases: list[dict], config: dict, sample: int) -> dict:
    rng = random.Random(20260720)
    picked = rng.sample(cases, min(sample, len(cases)))
    client = answer_mod._client()
    by_path: dict = {}
    details = []
    for c in picked:
        decision = router_mod.route(c["query"], store, config, c["app"])
        if decision.path == "oneshot":
            cand = answer_mod.answer_oneshot(c["query"], decision.chunks,
                                             c["app"], config, client=client)
        else:
            cand = answer_mod.answer_agent(c["query"], c["app"], config, store,
                                           client=client).answer
        verdict = judge_answer(c["query"], c["reference"], cand, config, client)
        by_path.setdefault(decision.path, []).append(verdict["verdict"] == "pass")
        details.append({"case": f"{c['source']}#{c['case_id']}",
                        "path": decision.path, "verdict": verdict["verdict"],
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

    print(f"== retrieval (n={len(cases)}, k={k}) ==")
    r = retrieval_scores(store, cases, k)
    for m, s in r["overall"].items():
        print(f"  {m:7s}  hit@{k}={s['hit_rate']:.0%}  MRR={s['mrr']:.3f}")
    print("  per source (hit@k):")
    for src, ms in r["per_source"].items():
        print(f"    {src:11s} " + "  ".join(f"{m}={v:.0%}" for m, v in ms.items()))

    if args.answers:
        print(f"\n== answers (judged, sample={args.answers}) ==")
        a = answer_scores(store, cases, config, args.answers)
        for path, s in a["by_path"].items():
            print(f"  {path:8s} pass={s['pass_rate']:.0%} (n={s['n']})")
        for d in a["details"]:
            print(f"    [{d['verdict']:4s}] {d['case']} ({d['path']}): {d['reason']}")
    store.close()


if __name__ == "__main__":
    main()
