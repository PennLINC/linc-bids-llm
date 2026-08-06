# bids-assistant

A troubleshooting assistant for the lab's BIDS Apps (v0: QSIPrep only). It
answers questions and diagnoses errors the way a maintainer does: check whether
someone already hit this (GitHub issues / NeuroStars), read the docs, and when
necessary grep the actual code at the version the user ran — with links back to
every source.

Full design + staged plan: [BIDS_ASSISTANT_BUILD_PLAN.md](BIDS_ASSISTANT_BUILD_PLAN.md).
Post-v0 work (open-model A/B, AWS deployment + cost analysis):
[ROADMAP.md](ROADMAP.md). Infrastructure (chunking, permalinks, incremental
sync, chat UI) is ported from the lab's internal linc-llm project.

## Setup

```bash
mamba create -n linc-bids-llm python=3.12
mamba activate linc-bids-llm
pip install -r requirements.txt      # requirements.txt mirrors this env exactly
cp config.example.yaml config.yaml   # set contact_email
cp .env.example .env                 # set GITHUB_TOKEN (harvest), OPENAI_API_KEY
```

## Hosted preview

There's a live hosted instance (AWS Lightsail, behind a shared-password gate).
**Colleagues testing it need nothing local** — just the URL + password from a
maintainer, then follow [eval/TESTING.md](eval/TESTING.md). Their feedback lands
centrally on the server. Deployment/ops: [DEPLOY.md](DEPLOY.md).

The sections below are for running your **own** copy locally (or standing up
another host).

## For maintainer-testers (prebuilt index, no harvest)

The `index/` (hybrid store) and `checkouts/` (version-pinned source) are build
artifacts — **gitignored, never committed** (a committed 63 MB index would live
in git history forever; deleting it later wouldn't reclaim the space without a
history rewrite). The index is instead shipped as a **GitHub Release asset**
(~28 MB), which can be replaced or removed anytime without touching history.

**Maintainer — publish the index** (needs `gh auth login`):

```bash
python -m src.ingest              # build index/ (~16 min; once)
scripts/package_index.sh --upload # tar it + attach to the 'index-latest' release
```

Re-running after a re-ingest overwrites the asset in place (`--clobber`).

**Tester — get running without harvesting:**

```bash
git clone https://github.com/PennLINC/linc-bids-llm && cd linc-bids-llm
mamba create -n linc-bids-llm python=3.12 && mamba activate linc-bids-llm
pip install -r requirements.txt
cp config.example.yaml config.yaml            # set contact_email
cp .env.example .env                          # set OPENAI_API_KEY (testers need only this)
scripts/fetch_index.sh                        # download + unpack the prebuilt index/
python -m src.checkouts                       # clone code for the agent path (~2 min)
streamlit run app.py
```

First query downloads the embedding model (BAAI/bge-small-en-v1.5, ~130 MB);
answers need each tester's own `OPENAI_API_KEY`. The one-shot path works without
`checkouts/`; the agent path's `grep_code`/`read_file` need it.

**Battle-testing is methodical — follow [eval/TESTING.md](eval/TESTING.md):**
work the scenario matrix, rate every answer (thumbs + problem category + the
correct source URL when you know it), then `scripts/submit_feedback.sh` to PR
your feedback. The lead aggregates with `python -m eval.feedback_report`,
promotes failures into regression cases with `python -m eval.feedback_to_cases`,
and scores them via `python -m eval.run_eval --heldout eval/regression.json` —
so fixes are measurable and guarded against regression before wider release.

## Status

- [x] Stage 0 — probes + scaffold (`python probes/probe_github_issues.py`,
      `python probes/probe_neurostars.py`)
- [x] Stage 1 — harvesters (dry-run each solo:
      `python -m src.sources.docs_source`,
      `python -m src.sources.issues_source [N]`,
      `python -m src.sources.neurostars_source [N]`)
- [x] Stage 2 — hybrid index (chroma + FTS5, RRF fusion, incremental ingest;
      `python -m src.ingest [--full]`). Note: `since=` is inclusive, so each
      incremental run re-fetches the one boundary issue (idempotent, net-zero).
- [x] Stage 3 — version-pinned checkouts + agent tools
      (`python -m src.checkouts` clones latest N tags + main;
      `src/tools.py` exposes search_kb / grep_code / read_file). Needs `rg`.
- [x] Stage 4 — router + one-shot / agentic answer paths
      (`python -m src.ask [--agent|--oneshot] "..."`). Needs OPENAI_API_KEY.
      Agent loop runs on the Responses API (reasoning model + tools).
- [x] Stage 5 — Streamlit chat UI (`streamlit run app.py`): multi-turn chat,
      Auto/One-shot/Agent modes, routing + tool-call expanders, thumbs+comment
      feedback to `.feedback/`, per-chat history in `.chats/`.
- [x] Stage 6 — eval harness (`python -m eval.harvest_eval` builds the
      held-out set; `python -m eval.run_eval [--answers N]` scores it).

## Eval

Known-item retrieval over 48 held-out solved cases (24 issues + 24 NeuroStars,
stratified old/new): query with each case's opening post, check whether its gold
thread lands in the hybrid top-k. Latest scorecard (k=8):

| method | hit@8 | MRR |
|--------|-------|-----|
| hybrid | 100%  | 0.974 |
| vector-only | 96% | 0.410 |
| bm25-only | 100% | 0.381 |

The hit rates are high because the gold thread is itself indexed; the load-
bearing number is **MRR**. Hybrid (0.974) ranks the gold thread ~1st almost
always, while neither vector (0.41) nor BM25 (0.38) alone does — RRF fusion is
what buys the ranking. Vector-only also misses 8% of issues (exact error
strings) that BM25 catches. This is the regression gate for retrieval/prompt/
model changes.

### Answer eval (`--answers N`) — use with caution

LLM-judged answers against the historical resolution, per path. A first run
(n=8) scored agent 29% / one-shot 0%, but reading the judge's reasons, most
"fails" are the assistant giving a *correct, fuller* answer that doesn't match a
**stale, point-in-time** historical fix (e.g. the reference says "use the
`pennbbl/qsiprep:unstable` image", long gone; the assistant correctly points to
later releases and is marked fail). Passes cluster on *timeless* resolutions
(e.g. "`--combine-all-dwis` is deprecated"). So the current answer-eval is a
**pessimistic proxy**: the reference set skews to version-specific fixes that no
longer apply. Before trusting it as a gate, judge for "correct and actionable"
rather than "matches the historical action", or curate timeless cases. The
retrieval eval above is the reliable regression gate for now.

### On `changes.md`

The qsiprep changelog is 88 of 146 docs chunks (60%). Measured, it is not noise
in practice: in full retrieval it appears 0/8 for general questions (issue and
thread chunks outrank it), and it correctly dominates only version/"what changed"
questions — which serves the version-awareness goal. Kept. It only crowds
`search_kb(source_filter="docs")`, a minor agent sub-path.
