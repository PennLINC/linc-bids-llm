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
- [ ] Stage 2 — hybrid index (chroma + FTS5, incremental ingest)
- [ ] Stage 3 — version-pinned checkouts + agent tools
- [ ] Stage 4 — router + one-shot / agentic answer paths
- [ ] Stage 5 — Streamlit chat UI
- [ ] Stage 6 — eval harness
