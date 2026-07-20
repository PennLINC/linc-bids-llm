# bids-assistant

A troubleshooting assistant for the lab's BIDS Apps (v0: QSIPrep only). It
answers questions and diagnoses errors the way a maintainer does: check whether
someone already hit this (GitHub issues / NeuroStars), read the docs, and when
necessary grep the actual code at the version the user ran — with links back to
every source.

Full design + staged plan: [BIDS_ASSISTANT_BUILD_PLAN.md](BIDS_ASSISTANT_BUILD_PLAN.md).
Infrastructure (chunking, permalinks, incremental sync, chat UI) is ported from
the lab's internal linc-llm project.

## Setup

```bash
mamba create -n linc-bids-llm python=3.12
mamba activate linc-bids-llm
pip install -r requirements.txt      # requirements.txt mirrors this env exactly
cp config.example.yaml config.yaml   # set contact_email
cp .env.example .env                 # set GITHUB_TOKEN (harvest), OPENAI_API_KEY
```

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
- [ ] Stage 6 — eval harness
