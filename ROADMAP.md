# Roadmap (post-v0)

v0 is functionally complete (Stages 0–6; see [README](README.md)). This file
holds the work that was deliberately deferred, with enough detail to pick up
cold. Nothing here blocks maintainer battle-testing.

---

## 1. Open-model support + A/B evaluation

**Goal:** run the answer paths against open-weight models (hosted or local) and
measure the quality/cost trade-off with the existing eval harness, rather than
guessing from public benchmarks.

### Why it's worth doing

Half the stack is already open and free: bge embeddings and the Chroma+FTS5
hybrid index run locally at zero marginal cost. Only *generation* is billed, so
this touches `src/answer.py` alone. Hosted open-weight models price roughly
4–5× cheaper on output than the current agent model, and output dominates the
agent path's cost (see §2 economics).

### Implementation sketch

1. **Config** — add to `llm:`
   ```yaml
   llm:
     api_base: null            # e.g. https://openrouter.ai/api/v1, or http://localhost:11434/v1
     api_key_env: OPENAI_API_KEY
     tool_protocol: responses  # "responses" (OpenAI reasoning models) | "chat" (everything else)
   ```
2. **`src/answer.py::_client()`** — pass `base_url=config["llm"]["api_base"]`
   and read the key from `api_key_env`. One-shot needs nothing else; it already
   uses `chat.completions`, which every OpenAI-compatible server speaks.
3. **`answer_agent`** — add a `chat.completions` tool-loop branch, selected when
   `tool_protocol: chat`. **This is the crux:** the agent path currently runs on
   the OpenAI **Responses API** because OpenAI rejects function tools combined
   with reasoning on `chat.completions`. That restriction is OpenAI-specific —
   open-model servers (vLLM, Ollama, llama.cpp) expose `chat.completions` with
   tools and no such conflict. `TOOL_SCHEMAS` in `src/tools.py` is already in
   chat.completions shape (`_responses_tools()` converts *to* Responses), so the
   chat loop can use it directly. Keep both paths; do **not** delete the
   Responses branch (see the git history for why).
4. **`eval/run_eval.py`** — accept `--config` (or `--model`) so the same
   held-out + regression cases can be scored per model, emitting a comparison
   table. This is the actual deliverable: your own numbers on your own corpus.
5. **Provenance** — already done: `src/feedback.py::run_context()` records the
   answering model, both configured models, embed model, `api_base`, and commit;
   `eval/feedback_report.py` breaks results down by model.

### Candidate models (as of July 2026 — re-check before implementing)

| Model | License | Notes |
|---|---|---|
| GLM-5.2 | MIT | ~744B MoE, 1M ctx, weights on HF (`zai-org`); strong SWE/agentic scores |
| DeepSeek V4 Pro | MIT | 1.6T total / 49B active, 1M ctx |
| Kimi K3 | Modified MIT | Reportedly tops tool-calling benchmarks; weights slated ~2026-07-27 |
| Qwen3-Coder 30B | open | Local-capable; code-shaped tool work matches our grep/read tools |
| Gemma 4 27B | open | Best value on a 24 GB card |

**Local-model caveat:** tool-call reliability is the binding constraint —
sub-7B models and quantization below Q4_K_M emit malformed tool calls
regardless of harness. And the agent path fires 5–13 model turns per question,
so local latency compounds badly (minutes, not seconds). Local models are
plausible for the **one-shot** path; the agent path wants a hosted/strong model.

### Do *not* distribute local models to testers

Varying model *and* hardware across testers confounds the feedback — you can't
separate a bad answer from a bad model from a slow machine. Run the A/B
centrally via `run_eval`; keep battle-testers on one consistent configuration.

---

## 2. Deployment to AWS

**PMACS is ruled out:** its VMs are reachable only on the Penn Medicine
wifi/VPN, which defeats external testers, a Read-the-Docs embed, and any
non-Penn-Med collaborator. AWS is the working assumption.

### What the app actually needs

- A **long-lived process** — Streamlit is websocket-based, so this is not a
  Lambda/serverless fit.
- **~10–20 GB disk**: index (63 MB) + checkouts (279 MB) + model cache and the
  Python env (torch dominates — use a CPU-only torch wheel to slim it).
- **CPU only.** Query-time embedding of a single question with bge-small is
  trivial; there is no GPU need unless self-hosting a model (see economics).
- **~4 GB RAM** is comfortable.
- **Secrets**: the LLM API key.
- **A periodic ingest job** (~16 min, network-bound, weekly is plenty).

### Recommended architecture (start simple)

Single small instance, mirroring the local setup:

- **Compute**: one `t4g.medium` EC2 (ARM/Graviton, 2 vCPU / 4 GB) — or
  **Lightsail** if fixed-price billing is easier to reason about for the lab.
- **Disk**: 20 GB gp3 EBS.
- **TLS/domain**: Caddy or nginx in front of Streamlit with Let's Encrypt
  (free), or ALB + ACM if you want AWS-managed certs.
- **Secrets**: SSM Parameter Store `SecureString` (free at standard tier) —
  cheaper than Secrets Manager for one key.
- **Ingest**: a systemd timer / cron running `python -m src.ingest` weekly.
  **Operational gotcha:** the app reads the index while ingest writes it. Build
  into a staging directory and swap atomically, then restart the service —
  don't ingest in place under a live app.
- **Persistence**: `index/` and `checkouts/` are both rebuildable, so they need
  no backup. `.feedback/` and `.chats/` **do** — sync them to S3 (or move
  feedback to a small database) or they die with the instance.

Skip ECS/Fargate/EKS unless there's a reason; at this scale they add moving
parts without benefit.

### Provisioning + deploy workflow

**Lightsail over EC2 for the v0 hosted preview.** Both are available on the
account. Lightsail wins here: fixed bundled price (compute + storage + transfer
in one predictable monthly number the lab can budget), a simpler console with
less to misconfigure, and a static IP + firewall + snapshots built in. A ~$12/mo
plan (2 GB RAM, 2 vCPU, 60 GB SSD) fits the footprint. Move to **EC2** only when
we need something Lightsail lacks — WAF/ALB, IAM instance roles for SSM,
autoscaling, or tight VPC integration — none of which v0 needs.

**GitHub stays the source of truth; the server is just a checkout that pulls.**
Development workflow is unchanged (branch → PR → merge to main). Deployment is
"the box pulls main and restarts." One-time setup on the instance:

1. `git clone` the repo; install miniforge; create the `linc-bids-llm` env;
   `pip install -r requirements.txt`.
2. `.env` with `OPENAI_API_KEY` (chmod 600), `config.yaml` from the example.
3. `scripts/fetch_index.sh` — on a **public** repo the release asset is
   curl-downloadable, so no `gh` auth needed on the server (simplify the script
   to a plain `curl -L` of the asset URL for the server case).
4. `python -m src.checkouts` (~2 min).
5. Run Streamlit as a **systemd service** (not a bare `streamlit run` in a
   shell — it must survive logout/reboot), bound to localhost:8501.
6. **Caddy** in front as reverse proxy → automatic Let's Encrypt HTTPS on the
   domain; or a Lightsail load balancer for managed certs.
7. Auth per the section below (Cloudflare Access is the low-friction pick).

**Updating:** `git pull && sudo systemctl restart bids-assistant`. Wrap it in a
`scripts/deploy.sh`, or trigger from a GitHub Action over SSH later. Re-run
`fetch_index.sh` after a fresh ingest; restart to pick up the swapped index.

### Auth and cost control (required before public exposure)

Every question costs money, so an unauthenticated public endpoint is a cost
risk, not just a security one.

**No provider-side hard cap is available (confirmed 2026-07 with UPenn admins):**
OpenAI key-level limits can't be set on our account, and project-level spend
caps are now *soft* — they email an alert at the threshold but do **not** stop
spending. So the only hard backstop is the one we build into the app.

**1. Authentication — the first-order control.** Turn "anyone on the internet
can spend our tokens" into "a known person misbehaves." Cheapest first:
- **Cloudflare Access** in front (free tier ~50 users) — no app changes, and it
  does edge rate-limiting too. Works over Lightsail or EC2.
- **Streamlit native OIDC** (`st.login`) against Penn SSO or Google.
- ALB + Cognito, if fully inside AWS is preferred.

**2. An in-app daily spend ceiling — the required hard backstop.** Since no
provider cap will stop a runaway bill (a user looping, a bug, an agent question
that fans out), the app must stop itself:
- Each OpenAI/Responses call returns token `usage`; convert to a dollar estimate
  with the configured model's rates and accumulate it in a small persisted
  counter keyed by UTC date (SQLite row or a JSON file — the app is single-node).
- Before answering, check the running daily total against a configured
  `llm.daily_budget_usd`; once exceeded, refuse with "daily budget reached" and
  log it. ~30 lines in `answer.py` + a config key. Covers self-inflicted
  runaway cost that no network-layer rule would see.
- Optional: a per-user/session rate limit (N questions/hour) on top.

**Why not throttle by IP at the AWS layer.** AWS WAF rate-based rules exist but
are a poor fit here: (a) WAF attaches to CloudFront/ALB/API Gateway, not bare
EC2 or Lightsail, so it means adding a load balancer; (b) Streamlit is
websocket-based — questions ride one long-lived connection, so HTTP-request
rate limiting barely correlates with token spend; (c) IP is leaky — a campus NAT
is one IP (throttles real users together) while an abuser rotates IPs. Fine
against volumetric scraping; useless as a token-cost cap. The app-level ceiling
is the control that actually maps to cost, because it counts questions/tokens.

### Cost estimate (approximate — verify current AWS rates)

| Item | ~Monthly |
|---|---|
| t4g.medium EC2 (on-demand; ~⅓ less with a 1-yr Savings Plan) | ~$24 |
| 20 GB gp3 EBS | ~$2 |
| Route53 hosted zone + egress | ~$1.50 |
| SSM Parameter Store (standard) | $0 |
| **Infrastructure subtotal** | **~$28** |
| Tokens (see below) | ~$20–60 |
| **Total** | **~$50–90/mo** |

### The open-model economics (the part worth internalizing)

Measured against observed traffic, at the actual OpenAI rates (verified 2026-08:
mini $0.75/$4.50, terra $2.00/$12.00 per 1M in/out, short-context): a one-shot
answer costs **~$0.005**; an agent answer costs **~$0.12** (5 model turns, 13
tool calls, ~35k cumulative input + ~4k output, reasoning billed as output) —
less in practice, since prompt caching on the threaded context isn't credited
here. At ~30 questions/day with 40% routed to the agent, that's **~$45/mo** in
tokens.

**Self-hosting an open model on an AWS GPU is not cost-effective at lab scale.**
A `g6.xlarge` (L4, 24 GB — enough for a 27–32B Q4 model) runs roughly
$0.80/hr ≈ **$590/mo** always-on. Break-even against OpenAI is on the order of
**~4,000 agentic questions/month (~130/day)**. A lab tool will not approach
that. The conclusion is robust even if these rates are off by ±30%.

So the open-model avenue on AWS should be **hosted open-weight APIs**, not
self-hosted GPUs:

- GLM-5.2 / DeepSeek V4 Pro via OpenRouter/Fireworks/Together — roughly **4–5×
  cheaper output** than the current agent model, taking tokens from ~$57 to
  perhaps ~$20/mo at the same volume.
- **AWS Bedrock** deserves a look *because* you're going AWS: it bills through
  your AWS account (often easier procurement than a card on OpenAI) and hosts
  open-weight families. Note Bedrock does **not** host OpenAI models, so this
  means changing provider — which is precisely what the §1 abstraction enables.
  Bedrock's tool-use API differs from both branches, so budget a third adapter.

**Bigger lever than model choice:** the router already sends the FAQ-majority to
one-shot at ~1/30th the cost of an agent answer. Tuning routing (and verifying
quality with the eval) moves the bill more than swapping models does.

### Suggested order

1. Stand up the single instance with auth + a spend cap; keep OpenAI.
2. Sync `.feedback/`/`.chats/` to S3 so signal survives redeploys.
3. Land §1's provider abstraction; A/B open models with `run_eval`.
4. Switch only if the eval says quality holds.

---

## 3. Other deferred items

- **The other six apps** (CuBIDS, BABS, ASLPrep, QSIRecon, XCP-D, BDT) — config
  entries plus `neighbors` wiring for pipeline-boundary questions. No new code
  expected; this is the "configuration, not code" claim being cashed in.
- **Issue-draft-to-GitHub** — v0 prints the draft; filing it and duplicate-
  checking open issues needs auth, so it rides with hosting.
- **Answer-eval methodology** — the current LLM judge scores against stale
  point-in-time fixes and is pessimistic; judge for "correct and actionable"
  instead, or lean on the feedback-derived regression set, which is
  maintainer-verified current truth.
- **RTD embed** — pass `READTHEDOCS_DATA` version into the app so docs-embedded
  questions are version-aware.
