"""Lint the systemd units in deploy/ for mistakes systemd accepts silently.
Pure text checks (no systemd needed); `systemd-analyze verify` on the server is
the real thing — see DEPLOY.md §7."""
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = sorted(DEPLOY.glob("*.service")) + sorted(DEPLOY.glob("*.timer"))
REFRESH = DEPLOY / "bids-assistant-refresh.service"
HELPER = "/home/ubuntu/linc-bids-llm/scripts/hc_ping.sh"


def _settings(path):
    """(lineno, key, value) per Key=Value line, continuation lines skipped.
    systemd only treats lines that START with # or ; as comments — anything
    after a value is part of it."""
    cont = False
    for n, line in enumerate(path.read_text().splitlines(), 1):
        s = line.strip()
        if s and s[0] not in "#;[" and not cont:
            key, _, value = s.partition("=")
            yield n, key, value
        cont = bool(s) and s[0] not in "#;" and s.endswith("\\")


def test_units_have_no_trailing_comments():
    assert UNITS
    bad = [f"{u.name}:{n} {key}=" for u in UNITS for n, key, value in _settings(u)
           if " #" in value or "\t#" in value]
    assert bad == []   # e.g. "Persistent=true  # ..." fails to parse and is ignored


def test_refresh_timer_settings():
    timer = {k: v for _, k, v in _settings(DEPLOY / "bids-assistant-refresh.timer")}
    assert timer["OnCalendar"] == "*-*-* 03:30:00 UTC"   # healthchecks.io mirrors this
    assert timer["Persistent"] == "true"
    assert timer["RandomizedDelaySec"] == "600"          # grace time is derived from it


def test_refresh_service_pings_around_the_refresh():
    lines = [(k, v) for _, k, v in _settings(REFRESH)]
    assert [(k, v) for k, v in lines if k.startswith("Exec")] == [
        ("ExecStartPre", f"-{HELPER} start"),       # "-": a failed ping never blocks the run
        ("ExecStart", "/home/ubuntu/linc-bids-llm/scripts/refresh.sh"),
        ("ExecStopPost", f"-{HELPER} result"),      # runs after success, failure, timeout, kill
    ]
    assert ("Type", "oneshot") in lines and ("TimeoutStartSec", "3600") in lines


def test_no_ping_url_is_committed():
    for path in UNITS + [DEPLOY.parent / ".env.example"]:
        live = [v for _, _, v in _settings(path)]
        assert not any(re.search(r"hc-ping\.com/[0-9a-f]{8}-", v) for v in live), path.name
    assert not any(k == "EnvironmentFile" for _, k, _ in _settings(REFRESH))  # .env stays out of the unit
