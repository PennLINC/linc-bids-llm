#!/usr/bin/env bash
# Report a run of bids-assistant-refresh.service to healthchecks.io.
#
# Called by the unit (deploy/bids-assistant-refresh.service), never by refresh.sh:
#   ExecStartPre=-.../scripts/hc_ping.sh start     "a run began"
#   ExecStopPost=-.../scripts/hc_ping.sh result    the outcome, as systemd saw it
#
# Why from the unit: ExecStopPost runs however the run ended — non-zero exit,
# start timeout, OOM kill, SIGKILL — and systemd hands over its verdict in
# $SERVICE_RESULT / $EXIT_CODE / $EXIT_STATUS. A trap inside refresh.sh cannot
# report those truthfully. A run that never happens at all (timer off, box down)
# is caught by the schedule + grace time set on the healthchecks.io check.
#
# Opt-in: does nothing unless HEALTHCHECK_URL (https://hc-ping.com/<uuid>) is in
# the environment or in .env. It can never fail or stall the refresh: every
# network call is bounded, it always exits 0, and it never prints the URL (the
# URL is a secret — whoever holds it can ping the check).
#
# By hand:  scripts/hc_ping.sh result     sends a FAILURE (tests the alert e-mail)
#           SERVICE_RESULT=success EXIT_CODE=exited EXIT_STATUS=0 scripts/hc_ping.sh result
set -u
cd "$(dirname "$0")/.." || exit 0

say() { echo "[hc_ping] $*"; }

# One KEY=value from .env; prints nothing when the key or the file is missing.
envval() {
  { grep -m1 -E "^[[:space:]]*$1[[:space:]]*=" .env 2>/dev/null || true; } \
    | cut -d= -f2- | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\'']//' -e 's/["'\'']$//'
}

URL="${HEALTHCHECK_URL:-$(envval HEALTHCHECK_URL)}"
URL="${URL%/}"
[ -n "$URL" ] || exit 0
case "$URL" in
  http://*|https://*) ;;
  *) say "WARNING: HEALTHCHECK_URL is not an http(s) URL; not pinging."; exit 0 ;;
esac

# The log tail is stored by a third party and pasted into the alert e-mail, so
# blank out every value held in .env (tokens, keys, the ping URL) and anything
# token-shaped before it leaves the box. Byte-wise (LC_ALL=C), so stray bytes in
# the log cannot make awk/sed bail.
scrub() {
  LC_ALL=C HC_URL="$URL" awk -v envfile=.env -v sq="'" '
    function redact(s, v,    out, p) {
      out = ""
      while ((p = index(s, v)) > 0) {
        out = out substr(s, 1, p - 1) "[REDACTED]"
        s = substr(s, p + length(v))
      }
      return out s
    }
    BEGIN {
      n = 0
      if (length(ENVIRON["HC_URL"]) >= 8) secret[++n] = ENVIRON["HC_URL"]
      while ((getline line < envfile) > 0) {
        eq = index(line, "=")
        if (line ~ /^[ \t]*#/ || eq == 0) continue
        v = substr(line, eq + 1)
        sub(/^[ \t]+/, "", v); sub(/[ \t\r]+$/, "", v)
        c = substr(v, 1, 1); if (c == "\"" || c == sq) v = substr(v, 2)
        c = substr(v, length(v), 1); if (c == "\"" || c == sq) v = substr(v, 1, length(v) - 1)
        if (length(v) >= 8) secret[++n] = v
      }
    }
    { for (i = 1; i <= n; i++) $0 = redact($0, secret[i]); print }' \
  | LC_ALL=C sed -E \
      -e 's/(github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}/\1[REDACTED]/g' \
      -e 's/sk-[A-Za-z0-9_-]{16,}/sk-[REDACTED]/g' \
      -e 's#(hc-ping\.com|hchk\.io)/[A-Za-z0-9_/-]+#\1/[REDACTED]#g' \
      -e 's#(https?://)[^/@[:space:]]+@#\1[REDACTED]@#g'
}

# What this run logged: its own output plus systemd's lines about it ("start
# operation timed out", "killed by the OOM killer").
journal_tail() {
  id="${INVOCATION_ID:-}"
  case "$id" in ''|*[!0-9a-fA-F]*) return 0 ;; esac
  command -v journalctl >/dev/null 2>&1 || return 0
  sleep 1   # let journald take in the last lines the run wrote
  cap=""    # never let a slow journal read eat into systemd's stop timeout
  if command -v timeout >/dev/null 2>&1; then cap="timeout 20"; fi
  $cap journalctl --no-pager -q -o cat -n 200 \
    "_SYSTEMD_INVOCATION_ID=$id" + "INVOCATION_ID=$id" 2>/dev/null
}

# tail -c can cut a multi-byte character in half; drop any invalid bytes.
utf8() {
  if command -v iconv >/dev/null 2>&1; then
    iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || true
  else
    cat
  fi
}

case "${1:-}" in
  start)
    # One short attempt: this runs before the refresh and must not hold it up.
    suffix="/start"
    curl -fsS -m 10 -o /dev/null --data-raw '' "$URL$suffix"
    rc=$?
    ;;
  result)
    # Success only when systemd says so on all three counts. $EXIT_STATUS alone
    # cannot decide: after a timeout or kill it is a signal NAME (TERM, KILL) —
    # not a valid ping URL — and it can even be 0 for a run systemd timed out.
    res="${SERVICE_RESULT:-unset}"
    code="${EXIT_CODE:-unset}"
    st="${EXIT_STATUS:-unset}"
    suffix="/fail"                    # timeout, signal, oom-kill, vars missing, ...
    if [ "$code" = exited ]; then
      case "$st" in
        *[!0-9]*) ;;
        0) if [ "$res" = success ]; then suffix=""; fi ;;        # bare URL = success
        [1-9]|[1-9][0-9]|[12][0-9][0-9])                          # refresh.sh's own exit status
          if [ "$res" = exit-code ] && [ "$st" -le 255 ]; then suffix="/$st"; fi ;;
      esac
    fi
    # Body: systemd's verdict, then the scrubbed log tail — under 10 kB in all,
    # which is what the alert e-mail shows.
    {
      echo "bids-assistant refresh: result=$res $code=$st"
      journal_tail | scrub | tail -c 8000
    } | utf8 \
      | curl -fsS -m 10 --retry 3 --retry-max-time 30 -o /dev/null \
          --data-binary @- "$URL$suffix"
    rc=$?
    ;;
  *)
    echo "usage: $0 start|result" >&2
    exit 0
    ;;
esac

# Say what was sent (the suffix only, never the URL) so the journal shows it.
if [ "$rc" -eq 0 ]; then
  say "sent ${suffix:-/ (success)}"
else
  say "WARNING: ping ${suffix:-/ (success)} not delivered (curl exit $rc); ignored."
fi
exit 0
