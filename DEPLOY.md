# Deploying bids-assistant to AWS Lightsail

A single always-on Lightsail instance running Streamlit behind Caddy
(auto-HTTPS), with a shared password gate and an in-app daily spend ceiling.
GitHub stays the source of truth — the server is a checkout that pulls.
Rationale, cost analysis, the EC2 alternative, and the index-refresh plan are in
[ROADMAP.md](ROADMAP.md) §2.

**Status:** deployed on Lightsail (us-east-1, Ubuntu 24.04, 2 GB plan) as of
2026-08-06, reachable over HTTPS via an `sslip.io` hostname with Caddy
`basic_auth`. ~$12/mo infra + token usage (guarded by `llm.daily_budget_usd`).

> Colleagues don't need any of this — they just get the URL + shared password.
> Local-run instructions (for people who want their own copy) are in the README.

## Before you share the URL (pre-public gate)

- [ ] **Auth is on** (step 4). Until then the URL is open to anyone and every
      question spends tokens. The #1 gate.
- [ ] **`llm.daily_budget_usd` is set intentionally** — $300 for the internal
      round; lower it (e.g. $20–50) before a wider/public audience. It's the
      only hard spend cap (no provider-side cap exists).
- [ ] **`llm.pricing` matches the OpenAI pricing page** (short-context tier).
      Verified 2026-08: mini $0.75/$4.50, terra $2.00/$12.00.
- [ ] **A test question answers in the browser** (not just the CLI) —
      confirms the Streamlit websocket works through Caddy.
- [ ] **`.feedback/` is being backed up** (see the bottom section).
- [ ] **Before going fully public:** add a per-client rate limit so one script
      can't drain the daily budget in minutes (ROADMAP §2). Not needed for a
      password-gated colleague round.

## 1. Provision the instance (Lightsail console)

1. Create instance → **Linux → Ubuntu 24.04 LTS**.
2. Plan: **$12 / 2 GB RAM / 2 vCPU** (512 MB/1 GB OOM on torch — 2 GB is the floor).
3. Create, then **Networking → attach a static IP**.
4. **Networking → IPv4 Firewall**: ensure **HTTPS (443)** is allowed with source
   **Anywhere IPv4** (22 and 80 are there by default). 443 must be open to the
   world — the login + budget cap are the protection, not the firewall.

> **Connecting:** the Lightsail **"Connect using SSH"** browser button works over
> 443 — use it if your network (e.g. a VPN) blocks outbound port 22, which breaks
> terminal SSH. It can't port-forward, but you don't need that: test the app with
> the CLI on the box (below), then reach the UI over HTTPS once Caddy is up.

## 2. One-time server setup

Run in the browser SSH (user is `ubuntu`):

```bash
sudo apt update && sudo apt install -y git ripgrep curl
curl -fsSL -o mf.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash mf.sh -b -p "$HOME/miniforge3" && rm mf.sh
"$HOME/miniforge3/bin/mamba" create -y -n linc-bids-llm python=3.12
git clone https://github.com/PennLINC/linc-bids-llm && cd linc-bids-llm
"$HOME/miniforge3/envs/linc-bids-llm/bin/pip" install -r requirements.txt
cp config.example.yaml config.yaml     # set contact_email; review daily_budget_usd
printf 'OPENAI_API_KEY=sk-...\n' > .env && chmod 600 .env   # real key
scripts/fetch_index.sh                 # public repo -> curl, no gh needed
"$HOME/miniforge3/envs/linc-bids-llm/bin/python" -m src.checkouts   # ~2 min
```

> On x86 Ubuntu, the default `pip` torch wheel is already CPU-only — it just
> needs ~2 GB of disk (the 60 GB SSD is fine). No special index URL required.

**Smoke-test the whole pipeline from the box** (no browser/port needed):

```bash
"$HOME/miniforge3/envs/linc-bids-llm/bin/python" -m src.ask "what does --output-resolution do?"
"$HOME/miniforge3/envs/linc-bids-llm/bin/python" -m src.ask --agent \
  "on qsiprep 26.0.0, where is the eddy cnr_maps check?"
```

Good linked answers = index, checkouts, key, and both paths all work.

## 3. Run it as a service

```bash
sudo cp deploy/bids-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bids-assistant
sudo systemctl status bids-assistant --no-pager   # "active (running)" on :8501
```

The unit is preconfigured for the `ubuntu` user + `/home/ubuntu` paths; edit it
if yours differ.

## 4. HTTPS + a shared-password gate (Caddy)

Install Caddy:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Make a shared login hash (type a password when prompted):

```bash
caddy hash-password           # copy the $2a$... line it prints
```

Write `/etc/caddy/Caddyfile` — replace the whole default file with this, using
your **static IP dashed** as an `sslip.io` hostname (no domain purchase needed;
`sslip.io` resolves the hostname to that IP and Caddy still gets a real cert):

```
<DASHED-IP>.sslip.io {
    basic_auth {
        tester <PASTE_$2a$_HASH>
    }
    reverse_proxy 127.0.0.1:8501
}
```

Then `sudo systemctl reload caddy` and open `https://<dashed-ip>.sslip.io`
(first hit takes ~20 s while the cert issues). Share that URL + the password.

> A real domain + Cloudflare Access (or Streamlit OIDC to Penn SSO) is the
> nicer long-term auth; `sslip.io` + `basic_auth` is the zero-dependency
> preview. Per-user identity (needed for per-user chat history) rides with that.

## 5. Cost guardrails (required)

No provider-side hard cap exists (OpenAI key limits unavailable; project caps
are soft/email-only), so the app's ceiling is the only hard stop:

- `llm.daily_budget_usd` in `config.yaml` — **$300/day** now; lower before wider
  release. Sidebar shows "Spend today (UTC)"; the app refuses once crossed.
- Keep `llm.pricing` current, or the estimate drifts.
- The password gate is the other half — it keeps strangers/bots out.

## 6. Updating the code

```bash
scripts/deploy.sh                 # git pull + deps + restart service
```

## 7. Refreshing the index + checkouts

**Automatic (recommended) — the server self-refreshes nightly.** A `systemd`
timer runs `scripts/refresh.sh`, which updates checkouts (`main` via fetch+reset
and any new release tags), rebuilds the index **incrementally into a staging
dir**, validates it, swaps it in atomically, restarts the service, and
**re-publishes the release asset** so the downloadable index stays current for
local dev. No manual steps, no stale `main`/versions. One-time setup:

```bash
# the server needs a GITHUB_TOKEN for harvesting — add it to .env
printf 'GITHUB_TOKEN=github_pat_...\n' >> .env

# let the refresh restart the app service without a password (scoped sudoers rule)
echo 'ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart bids-assistant' \
  | sudo tee /etc/sudoers.d/bids-assistant-refresh

# install + enable the nightly timer
sudo cp deploy/bids-assistant-refresh.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bids-assistant-refresh.timer
sudo systemctl start  bids-assistant-refresh.service   # run once now (optional)

systemctl list-timers bids-assistant-refresh.timer     # confirm it's scheduled
journalctl -u bids-assistant-refresh.service -n 40     # read the last run's log
```

Adjust the cadence in the `.timer` (`OnCalendar=`); nightly is cheap since ingest
is incremental. Runs are logged to the journal. A failed refresh leaves the live
index untouched (it only swaps a validated staging build).

**About the asset re-publish:** `refresh.sh` runs `package_index.sh --upload`
at the end, authenticating `gh` from the `.env` `GITHUB_TOKEN`. That token must
have **write** access to the repo (`contents:write`, or classic `repo` scope) to
publish a release asset — a **read-only harvest token will skip the publish with
a warning**, and the live index is still current regardless. So either give the
server's token write scope, or set `SKIP_PUBLISH=1` in the service and publish
from a maintainer machine instead. `gh` must also be installed on the box for
this step (the tester `fetch_index.sh` path uses plain `curl` and needs no `gh`).

**Manual (fallback / one-off):** rebuild elsewhere and pull the published asset:

```bash
# maintainer machine
python -m src.ingest && scripts/package_index.sh --upload
# server
REFRESH_INDEX=1 scripts/deploy.sh
```

The GitHub release asset is now just a backup/distribution snapshot — the server
no longer depends on it once the timer is enabled.

## What survives a redeploy, what doesn't

- `index/`, `checkouts/` — rebuildable; no backup needed.
- `.feedback/`, `.chats/`, `.state/` (daily spend) — **local to the instance.**
  Because everyone uses this one hosted app, all colleague feedback and chat
  history accumulate here centrally. **Back `.feedback/` up** (periodic
  `scp`/`aws s3 sync`) or you lose the tuning signal if the instance is replaced.
