# Deploying bids-assistant to AWS Lightsail

A single always-on instance running Streamlit behind Caddy (auto-HTTPS), with
auth in front and an in-app daily spend ceiling. GitHub stays the source of
truth — the server is just a checkout that pulls. Rationale, cost analysis, and
the EC2 alternative are in [ROADMAP.md](ROADMAP.md) §2.

Estimated ~$12/mo infra + token usage (guarded by `llm.daily_budget_usd`).

## Before you share the URL (pre-public gate)

Do not hand out the link until all of these are true:

- [ ] **Auth is on** (step 4). Until then the URL is open to anyone, and every
      question spends tokens. This is the #1 gate.
- [ ] **`llm.daily_budget_usd` is set intentionally.** It's $300 for the
      internal test phase — lower it (e.g. $20–50) before a wider audience. It
      is the only hard spend cap; no provider-side cap exists.
- [ ] **`llm.pricing` matches the current OpenAI pricing page** (short-context
      tier). Verified 2026-08: mini $0.75/$4.50, terra $2.00/$12.00. Re-check if
      you change models or OpenAI changes rates — the ceiling is only as
      accurate as these numbers. (The estimate is conservative: it ignores
      prompt caching, so real spend runs a bit under what the sidebar shows.)
- [ ] **The app actually runs on the box** — `systemctl status bids-assistant`
      is active and a test question answers. On a CPU-only instance confirm the
      CPU torch install worked (see step 2's note); a GPU-sized torch wheel can
      blow the disk or fail to import.
- [ ] **`.feedback/` is being backed up** (step "What survives a redeploy") or
      you'll lose the signal the whole test exists to collect.

## 1. Provision the instance (AWS console)

1. **Lightsail** → Create instance → Linux/Unix → **Ubuntu 22.04 LTS**.
2. Plan: **2 GB RAM / 2 vCPU / 60 GB SSD** (~$12/mo).
3. Create, then **Networking → attach a static IP**.
4. **Networking → firewall**: allow 22 (SSH), 80 (HTTP), 443 (HTTPS). Restrict
   22 to your IP if you can.
5. Point your DNS `A` record at the static IP (in Route 53, Cloudflare, or Penn
   DNS). If you'll use Cloudflare Access for auth, put the domain on Cloudflare.

## 2. One-time server setup (SSH in)

```bash
# system deps
sudo apt update && sudo apt install -y git ripgrep curl

# miniforge + the project env
curl -fsSL -o mf.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash mf.sh -b -p "$HOME/miniforge3" && rm mf.sh
"$HOME/miniforge3/bin/mamba" create -y -n linc-bids-llm python=3.12

# the app
git clone https://github.com/PennLINC/linc-bids-llm && cd linc-bids-llm
"$HOME/miniforge3/envs/linc-bids-llm/bin/pip" install -r requirements.txt

cp config.example.yaml config.yaml     # set contact_email; review daily_budget_usd
printf 'OPENAI_API_KEY=sk-...\n' > .env && chmod 600 .env

scripts/fetch_index.sh                 # public repo -> curl, no gh needed
"$HOME/miniforge3/envs/linc-bids-llm/bin/python" -m src.checkouts   # ~2 min
```

> On a **CPU** box use a CPU-only torch wheel to keep the env small — sentence-
> transformers pulls torch, and the default build is GPU-sized. Verify the app
> imports before wiring the service.

## 3. Run it as a service

```bash
sudo cp deploy/bids-assistant.service /etc/systemd/system/
# edit User/paths in the unit if you didn't use `bids` + the paths above
sudo systemctl daemon-reload
sudo systemctl enable --now bids-assistant
sudo systemctl status bids-assistant       # confirm it's running on :8501
```

## 4. HTTPS + auth

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/bids-assistant.example.edu/YOUR.DOMAIN/' /etc/caddy/Caddyfile
sudo systemctl reload caddy                # provisions a Let's Encrypt cert
```

**Auth (do this before sharing the URL).** Cheapest and no app change:
**Cloudflare Access** in front of the domain (free up to ~50 users), restricted
to an email list or your org. Alternatively, uncomment `basic_auth` in the
Caddyfile for a quick shared gate, or wire Streamlit OIDC to Penn SSO.

## 5. Cost guardrails (required)

There is **no provider-side hard cap** (OpenAI key limits unavailable; project
caps are soft/email-only), so the app's ceiling is the only hard stop:

- `llm.daily_budget_usd` in `config.yaml` — currently **$300/day** for the
  internal test phase. **Lower it** before wider release. The sidebar shows
  "Spend today (UTC)"; the app refuses once the day's estimated spend crosses it.
- Keep `llm.pricing` current with the provider's published rates, or the
  estimate drifts.
- Auth (step 4) is the other half — it stops anonymous strangers spending tokens.

## 6. Updating

```bash
scripts/deploy.sh                 # git pull + deps + restart
REFRESH_INDEX=1 scripts/deploy.sh # also re-fetch the prebuilt index after a re-ingest
```

## 7. Refreshing the index

Rebuild elsewhere (or on the box) and publish, then refresh on the server:

```bash
# maintainer machine
python -m src.ingest && scripts/package_index.sh --upload
# server
REFRESH_INDEX=1 scripts/deploy.sh
```

`fetch_index.sh` swaps the index atomically (moves the old aside), and the
restart picks up the new one — the app never reads a half-written index.

## What survives a redeploy, what doesn't

- `index/`, `checkouts/` — rebuildable; no backup needed.
- `.feedback/`, `.chats/`, `.state/` (daily spend) — **local to the instance.**
  Back `.feedback/` up (periodic `aws s3 sync` to a bucket) or you lose the
  tuning signal when the instance is replaced. Feedback submitted via
  `scripts/submit_feedback.sh` is already safe in git.
