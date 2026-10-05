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
- [ ] **The nightly refresh is monitored** (§7) — otherwise an expired GitHub
      token silently freezes the corpus.
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

`deploy.sh` does not install systemd units. After a pull that changes anything
in `deploy/`, re-install them:

```bash
sudo cp deploy/bids-assistant-refresh.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/bids-assistant-refresh.{service,timer}   # prints nothing when clean
```

`daemon-reload` re-reads the timer and re-arms it with the new settings, so the
timer itself needs no restart (on a systemd older than 255.4-1ubuntu8.15, a
restart would start a refresh at once).

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
sudo systemctl start --no-block bids-assistant-refresh.service   # run once now (optional)

systemctl list-timers bids-assistant-refresh.timer     # confirm it's scheduled
systemctl is-enabled bids-assistant-refresh.timer                 # "enabled" = survives reboots
systemctl show bids-assistant-refresh.timer -p Persistent -p RandomizedDelayUSec   # expect yes / 10min
journalctl -u bids-assistant-refresh.service -n 40     # read the last run's log
```

The timer fires at 03:30 UTC plus up to 10 min of jitter, and catches up at the
next boot if the box was off at that time. Adjust the cadence in the `.timer`
(`OnCalendar=`); nightly is cheap since ingest is incremental. Runs are logged
to the journal. A refresh that fails before the swap (checkouts, ingest,
validation) leaves the live index untouched — it only swaps a validated staging
build. Exit status **3** means the new index is live but the app restart or the
asset publish failed.

**Monitoring — an e-mail when the nightly refresh fails or stops running.**
Optional; inert until configured. The unit reports every run to
[healthchecks.io](https://healthchecks.io) (free plan) through
`scripts/hc_ping.sh`: a start ping, then the outcome with the tail of that run's
journal (values from `.env` and token-shaped strings are redacted before it
leaves the box). A failed step, a timeout or an OOM kill alerts at once; a run
that never happens (timer off, box down) alerts when no ping arrives in time.
A failed asset publish or app restart is reported too (exit status 3: the new
index is live, but the downloadable copy or the running app is stale). One
limit: only runs started through systemd are reported (a hand-run
`scripts/refresh.sh` is not).

1. **Create the check** at healthchecks.io: schedule **Cron** `30 3 * * *`, time
   zone **UTC** (it mirrors the timer's `OnCalendar=`; change both together),
   grace time **1 h 30 min** (10 min jitter + the 60 min `TimeoutStartSec` +
   margin). Turn the e-mail integration on (press its **Test** button), and
   under *Account → Email Reports* ask for daily reminders while a check is down.
2. **Give the server the ping URL.** It is a secret — whoever holds it can ping
   the check — so it lives only in `.env`, never in the unit file:

   ```bash
   printf '\nHEALTHCHECK_URL=https://hc-ping.com/<uuid>\n' >> .env && chmod 600 .env
   grep -c '^HEALTHCHECK_URL=' .env   # must print 1
   ```

   The leading `\n` keeps the line from being glued onto the last key if `.env`
   has no final newline. The first `HEALTHCHECK_URL` line wins, so to change the
   URL later edit that line in place rather than appending another.

3. **Install the updated units** (§6), then prove it end to end:

   ```bash
   sudo systemctl start --no-block bids-assistant-refresh.service
   journalctl -fu bids-assistant-refresh.service   # until "[hc_ping] sent / (success)"
   ```

   The check's **Events** list should now show a start, then a success with the
   log as its body. The ping URL answers 200 even for a wrong UUID, so Events is
   the only proof the URL is right.

To test the alert e-mail without breaking anything, `scripts/hc_ping.sh result`
sends a failure and
`SERVICE_RESULT=success EXIT_CODE=exited EXIT_STATUS=0 scripts/hc_ping.sh result`
sends the recovery. To stop monitoring, delete the `HEALTHCHECK_URL` line.

**About the asset re-publish + the two tokens.** Harvesting and publishing use
different tokens, because publishing needs write access and harvesting doesn't:

- `GITHUB_TOKEN` — harvesting (ingest/checkouts). Read-only is fine.
- `GH_PUBLISH_TOKEN` — the release-asset publish. Needs **write** access
  (fine-grained: *Contents read+write* on `PennLINC/linc-bids-llm`, or a classic
  `repo` token). `refresh.sh` uses this for `gh`, falling back to `GITHUB_TOKEN`
  only if it's unset.

So keep your read-only `GITHUB_TOKEN` and **add a second line** to the server's
`.env`: `GH_PUBLISH_TOKEN=...` with the write token. If you skip it, `refresh.sh`
falls back to `GITHUB_TOKEN`; with a read-only token the index is still
refreshed, but the publish fails and the run ends with exit status 3, which the
monitoring reports as a failure. `gh` must be installed on the box for this step
(the tester `fetch_index.sh` path uses plain `curl` and needs no `gh`).

To not publish from this box at all, run
`sudo systemctl edit bids-assistant-refresh.service`, add `Environment=SKIP_PUBLISH=1`
under `[Service]`, and publish from a maintainer machine instead.

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
