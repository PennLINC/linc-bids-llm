# bids-assistant — v0 Build Plan (QSIPrep only, local proof of concept)

A troubleshooting assistant for the lab's BIDS Apps. It answers user questions
and diagnoses errors the way a maintainer does: check whether someone already
hit this (issues / NeuroStars), read the docs, and when necessary grep the
actual code at the version the user ran — with links back to every source.

**v0 scope**: QSIPrep only, runs locally (`streamlit run` / CLI), no hosting.
Hosting is TBD (PMACS public-VM question + PennChat API gateway question are
outstanding); nothing in v0 may depend on the answer. The architecture must
generalize to the other apps (CuBIDS, BABS, ASLPrep, QSIRecon, XCP-D, BDT) by
**configuration, not code** — every record carries an `app` field from day one.

This file seeds a fresh repo. Build in the numbered stages below; each stage is
independently runnable with a "Done when" check. Reuse linc-llm patterns
(https://github.com/<lab>/linc-llm) wherever they fit — chunking with exact
line ranges, commit-pinned `?plain=1#L..` permalinks, manifest + incremental
sync, resilient crawls, scoped Streamlit chat, feedback logging.

---

## Design principles (decided in discussion — don't relitigate casually)

1. **Never embed code.** Code questions are answered agentically: grep + read
   over version-pinned checkouts, maintainer-style. Embedding-RAG loses on
   exact-match error strings and can't follow structure; checkouts-at-tags make
   version handling trivial.
2. **Do index prose.** Closed issues and solved NeuroStars threads are a
   labeled support corpus — the FAQ-shaped majority of questions should be
   answered from them in one cheap shot, not re-derived agentically each time.
3. **Hybrid retrieval, not vector-only.** Error strings and identifiers are
   exact-match creatures: BM25 (SQLite FTS5) alongside embeddings, results
   fused. This matters more than context size.
4. **Two answer paths behind a router.** FAQ-shaped → one-shot RAG (~seconds,
   ~cents). Novel traceback / code question → agentic tool loop (~a minute,
   ~tens of cents). The agentic path checks the index first, like a maintainer
   asking "didn't someone hit this?"
5. **Version awareness is core, not polish.** Users run old containers. The
   assistant asks for (or parses) the version and greps that tag's checkout.
   Answers note when a fix landed in a later release.
6. **Diagnostic, not just grounded.** Unlike linc-llm's "answer only from
   context", this assistant may reason beyond retrieved text — but must label
   speculation, cite what it used, and ask clarifying questions (version,
   command, full traceback) when the input is insufficient.
7. **Every answer links back.** Issues/threads by URL; docs and code by
   commit-or-tag-pinned line-anchored permalink (linc-llm's URL scheme).
8. **Escalation is a feature.** When stuck, produce a well-formed issue draft
   (version, command, traceback, what was tried, suspected code path) and check
   for duplicate open issues. Failures should still reduce maintainer load.

---

## Stack

- Python 3.12, `requests`, `pyyaml`, `truststore>=0.10` (lab VPN), `tiktoken`
- `chromadb>=1.0,<2` + `sentence-transformers` (BAAI/bge-small-en-v1.5, pinned)
  — vector half of retrieval (linc-llm's store, plus an `app` filter)
- `sqlite3` stdlib FTS5 — keyword half of retrieval (no new dependency)
- `ripgrep` (`rg`) — the grep tool over checkouts (subprocess; brew/conda)
- `openai` SDK with tool calling — answer generation + agent loop
- `streamlit` — chat UI
- `pytest` — mirror linc-llm's mocked-transport test style from the start

Models (config-toggled): one-shot path `gpt-5.4-mini`-class; agentic path a
mid-tier model (`gpt-5.6-terra` / `gpt-5.4`-class — tool loops over tracebacks
justify it). Output budget generous (max_tokens ≥ 2000); multi-turn chat.

---

## Repo layout (target)

```
bids-assistant/
  BUILD_PLAN.md              # this file
  README.md
  requirements.txt
  config.example.yaml        # apps, sources, models; config.yaml gitignored
  .env.example               # GITHUB_TOKEN (harvest), OPENAI_API_KEY (answer)
  probes/
    probe_neurostars.py      # tag coverage + counts + solved rates per app
    probe_github_issues.py   # issue/comment counts, since-param sanity
  checkouts/                 # gitignored; shallow clones per app per tag
  index/                     # gitignored; chroma + fts.sqlite + manifest.json
  src/
    common.py                # config, chunking, tokens (port from linc-llm)
    store.py                 # chroma wrapper + FTS5 mirror + hybrid query
    sources/
      docs_source.py         # docs from the app repo (port github_source)
      issues_source.py       # GitHub issues+comments -> thread Records
      neurostars_source.py   # Discourse tag walk -> thread Records
    checkouts.py             # clone/update tags; resolve version -> path
    tools.py                 # grep_code, read_file, search_kb (agent tools)
    router.py                # traceback/FAQ triage
    answer.py                # one-shot path, agentic loop, system prompts
    ingest.py                # harvest -> chunk -> index (incremental, manifest)
    ask.py                   # CLI
  app.py                     # streamlit chat (port linc-llm UI + feedback)
  tests/
  eval/
    harvest_eval.py          # build held-out set from solved history
    run_eval.py              # retrieval hit-rate + judged answers
```

---

## Config sketch

```yaml
apps:
  qsiprep:                       # v0: this is the only entry
    github_repo: PennLINC/qsiprep
    docs_paths: [docs/]          # within the repo; rendered RTD not needed
    neurostars_tags: [qsiprep]
    neurostars_search: []        # extra search terms if tagging is spotty
    checkout_tags: 3             # latest N release tags + main
    neighbors: []                # v1: qsirecon etc. for pipeline-boundary Qs

retrieval:
  top_k: 8                       # wider than linc-llm; answers run longer
  candidates: 40                 # hybrid pool before fusion
  embed_model: BAAI/bge-small-en-v1.5

llm:
  oneshot_model: gpt-5.4-mini
  agent_model: gpt-5.6-terra
  max_output_tokens: 2500
  max_tool_iterations: 12
```

---

## The Record schema (extends linc-llm's)

Common: `id`, `text`, `app` ("qsiprep"), `source`
("docs" | "issues" | "neurostars"), `title`, `url`.

- docs chunks: `gh_repo`, `gh_path`, `gh_sha`, `gh_line_start/end` (linc-llm
  scheme, pinned to the release tag, `?plain=1#L..` anchors)
- issue threads: `gh_issue` (number), `gh_state`, `gh_solved` (closed as
  completed), `gh_created`, `gh_updated`, `gh_labels` (comma-joined)
- neurostars threads: `ns_topic_id`, `ns_solved`, `ns_created`, `ns_bumped`,
  `ns_replies`, `ns_views`

Thread records are question-first, accepted/most-liked answer second, other
substantive replies condensed; code blocks preserved verbatim (they hold the
tracebacks that make threads findable). Thread title prefixed onto every chunk.

---

## Build stages

### Stage 0 — Probes + scaffold
Port `common.py` (chunker, config, tokens) from linc-llm. Write the two probes:
`probe_neurostars.py` prints, per configured tag: topic count, solved rate,
newest/oldest activity — and flags tags that return nothing.
`probe_github_issues.py` prints issue + comment counts and checks `since=`
incremental listing works with the token.
**Done when:** both probes run green for qsiprep and print real counts (these
numbers calibrate the rest of the plan).

### Stage 1 — Harvesters
`docs_source.py`: qsiprep repo docs at the latest release tag (not main —
answers should match what users ran; the tag is the pin in permalinks).
`issues_source.py`: full issue list + comments, `since`-param incremental,
thread-shaping per the schema. `neurostars_source.py`: tag walk + topic fetch +
post-stream drain, HTML→text preserving code blocks, `bumped_at` incremental.
All three: linc-llm-grade resilience (timeouts, backoff, per-object skip,
polite User-Agent with contact email; Discourse ≈1 req/s, GitHub 0.05s sleep).
**Done when:** each source dry-runs solo printing records with titles + URLs,
and spot-clicked URLs open the right issue/thread/doc lines.

### Stage 2 — Hybrid index
`store.py`: chroma collection (embeddings, cosine, query-prefix) + an FTS5
table mirroring id/text/metadata, written together at ingest. `hybrid_query
(text, k, where)` runs both, fuses by reciprocal rank, returns top candidates.
`ingest.py`: harvest → chunk → index, with manifest + incremental sync ported
from linc-llm (issues via `updated_at`, neurostars via `bumped_at`, docs via
release tag). `--full` escape hatch; model/chunk-config mismatch forces full.
**Done when:** `python -m src.ingest` builds the index; a paste of a literal
error string retrieves the issue that contains it (BM25 catching what vectors
miss — test with a real qsiprep traceback from a closed issue); no-op re-ingest
touches nothing.

### Stage 3 — Checkouts + tools
`checkouts.py`: shallow-clone qsiprep at the latest N release tags + main under
`checkouts/qsiprep/<tag>/`; map a user-reported version to the nearest tag.
`tools.py`, exposed to the model via OpenAI tool calling:
- `search_kb(query, source_filter?)` → hybrid_query results (title/url/snippet)
- `grep_code(pattern, version)` → ripgrep over that tag's checkout (bounded output)
- `read_file(path, version, start, end)` → numbered lines + the tag-pinned permalink
**Done when:** a scripted tool sequence reproduces a maintainer move: grep a
real error string from a qsiprep issue → find the raising line → read_file
returns the code and a permalink that opens those lines on GitHub at that tag.

### Stage 4 — Router + two answer paths
`router.py`: heuristic triage — contains-traceback / long-paste detection, plus
"did hybrid retrieval return a high-confidence near-duplicate?" FAQ-shaped →
one-shot path; otherwise agentic. `answer.py`:
- one-shot: retrieved chunks + diagnostic system prompt → answer with bracket
  citations (linc-llm style, but longer and allowed to reason-with-labels)
- agentic: tool loop (search_kb first, then code tools), iteration-capped,
  version-aware (asks if unknown), same citation discipline
- both: refuse-to-guess posture inherited from linc-llm, but "the docs don't
  cover this; based on the code at <permalink>, likely X" is in-bounds
`ask.py` CLI for both paths (`--agent` to force the loop).
**Done when:** three acceptance cases pass: (a) a FAQ question answers in one
shot citing a solved thread; (b) a pasted traceback from a real closed issue
routes to the agent, which finds the raising code and the historical fix;
(c) an unanswerable question yields a structured issue draft, not a guess.

### Stage 5 — Chat UI
Port linc-llm's `app.py`: multi-turn chat (the agent path needs conversation
memory), sidebar showing index freshness + checkout tags, per-chat local
history (`.chats/`), thumbs+comment feedback log (`.feedback/`) — feedback is
the tuning signal, wire it before anyone else touches the app. Show the routing
decision and tool calls in an expander (maintainers will want to see its work).
**Done when:** a lab member who has never cloned qsiprep pastes a real error
into the local app and gets a useful, linked answer; the transcript shows which
path ran and every source consulted.

### Stage 6 — Eval harness (before tuning anything)
`eval/harvest_eval.py`: hold out ~50 solved cases (closed qsiprep issues +
solved NeuroStars threads, stratified old/new); strip each to its opening post.
`eval/run_eval.py`: for each case, (1) retrieval hit rate — does the known
resolution's thread/URL appear in the hybrid top-k? (2) optional LLM-judged
answer match against the historical fix. Report per-path.
**Done when:** one command prints the scorecard; the numbers become the
regression gate for any retrieval/prompt/model change (including "was the
agentic path worth it" — compare paths on the same cases).

---

## v0 exit criteria

The PoC is done when Stage 4's three acceptance cases and Stage 6's scorecard
exist, and two real questions from lab members (not maintainers) got useful
answers. Then decide: expand apps (config entries + re-ingest) vs. host first.

## Deferred (not v0)

- **Hosting + embeds** — **PMACS is ruled out** (2026-07): its VMs are only
  reachable on the Penn Medicine wifi/VPN, which defeats external testers, an
  RTD embed, and non-Penn-Med collaborators. AWS is now the working assumption;
  see [ROADMAP.md](ROADMAP.md) §2 for the architecture, auth, and cost analysis
  (including hosted-open-model vs. OpenAI economics, and why self-hosting a GPU
  doesn't pay at lab scale). Nothing in v0 assumes an answer.
- **The other six apps** — config entries + `neighbors` wiring for
  pipeline-boundary questions (QSIPrep→QSIRecon→XCP-D); no new code expected.
- **Issue-draft-to-GitHub integration** (v0 prints the draft; filing it and
  duplicate-checking open issues via API comes with hosting/auth).
- **Rate limiting / auth / cost caps** — meaningless until hosted.
- **BDT validator execution or any tool that runs user data** — out of scope;
  tools stay read-only.

## Guardrails

- Public content only; polite crawling (User-Agent with contact email; honor
  Retry-After; Discourse ~1 req/s). We are members of these communities.
- Tools are read-only over checkouts; no shell, no network from the agent
  beyond the declared tools; bound grep/read output sizes.
- Never answer version-specific questions from `main` when the user's version
  is known; say "fixed in vX.Y" when the code diverges.
- Secrets in `.env` (gitignored); GITHUB_TOKEN is harvest-only; members of the
  PoC need only OPENAI_API_KEY.
- Don't over-engineer: no framework, no queue, plain scripts + one Streamlit
  app, same as linc-llm.
