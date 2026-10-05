"""refresh.sh and hc_ping.sh end to end in a throwaway repo: the real scripts
(copied) with python, curl, journalctl, sudo, systemctl and package_index.sh
replaced by one recording stub — no network, no real index, no service restart."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash not installed")

OLD = {"chunks": {"docs": 100, "issues": 200}, "built_at": "old"}
NEW = {"chunks": {"docs": 100, "issues": 205}, "built_at": "new"}
URL = "https://hc.invalid/ping/1234-uuid"     # .invalid: can never reach a real check
OK = {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}
RUN_ID = "0123456789abcdef0123456789abcdef"   # the shape of systemd's $INVOCATION_ID

# One multi-call stub, symlinked under every name the scripts shell out to, that
# appends "<name> <args>" to $STUB_LOG. (One file, not several: macOS spends ~0.4 s
# vetting each brand-new executable on first exec; symlinks to it are free.)
#   envpy             ENV_PY stand-in: -m src.checkouts / -m src.ingest (writes a
#                     manifest into $BIDS_INDEX_PATH) / "- <staging>" (the stdin-fed
#                     validation heredoc, handed to the real interpreter)
#   package_index.sh  records its args and the GH_TOKEN it was given
#   curl              like real curl, reads stdin only when asked to (@-)
#   journalctl        prints $FAKE_JOURNAL
#   sudo, systemctl   record only (nothing is restarted)
#   sleep             returns at once (hc_ping.sh pauses 1 s for journald)
STUB = """\
#!/bin/bash
name="${0##*/}"
case "$name" in
  envpy)
    echo "python $*${BIDS_INDEX_PATH:+ BIDS_INDEX_PATH=$BIDS_INDEX_PATH}" >> "$STUB_LOG"
    case "$1 ${2:-}" in
      "-m src.checkouts") exit "${FAKE_CHECKOUTS_RC:-0}" ;;
      "-m src.ingest")
        echo "ingest: ${FAKE_INGEST_SAYS:-harvesting}"
        [ -z "${FAKE_INGEST_SLEEP:-}" ] || /bin/sleep "$FAKE_INGEST_SLEEP"
        [ "${FAKE_INGEST_RC:-0}" = 0 ] || exit "$FAKE_INGEST_RC"
        printf '%s' "$FAKE_MANIFEST" > "$BIDS_INDEX_PATH/manifest.json" ;;
      "- "*) exec "$REAL_PY" "$@" ;;
      *) echo "fake python: unexpected args: $*" >&2; exit 97 ;;
    esac ;;
  package_index.sh)
    echo "$name $* GH_TOKEN=${GH_TOKEN:-<unset>}" >> "$STUB_LOG"
    exit "${FAKE_PUBLISH_RC:-0}" ;;
  curl)
    echo "$name $*" >> "$STUB_LOG"
    case " $* " in *"@-"*) cat >> "$STUB_LOG.curl-stdin" ;; esac
    [ -z "${FAKE_CURL_SLEEP:-}" ] || /bin/sleep "$FAKE_CURL_SLEEP"
    exit "${FAKE_CURL_RC:-0}" ;;
  journalctl)
    echo "$name $*" >> "$STUB_LOG"
    printf '%s' "${FAKE_JOURNAL:-}" ;;
  sudo)
    echo "$name $*" >> "$STUB_LOG"
    exit "${FAKE_SUDO_RC:-0}" ;;
  sleep) exit 0 ;;
  *) echo "$name $*" >> "$STUB_LOG" ;;
esac
"""


@pytest.fixture(scope="session")
def stub(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stub"
    path.write_text(STUB)
    path.chmod(0o755)
    return path


class Sandbox:
    """A fake repo root: copies of the scripts, a live index/, stubs on PATH."""

    def __init__(self, root: Path, stub: Path):
        self.root, self.repo, self.log = root, root / "repo", root / "calls.log"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        for name in ("refresh.sh", "hc_ping.sh"):
            shutil.copy(SCRIPTS / name, self.repo / "scripts" / name)
        for rel in ("scripts/package_index.sh", "bin/envpy", "bin/curl", "bin/sleep",
                    "bin/journalctl", "bin/sudo", "bin/systemctl"):
            (self.repo / rel).symlink_to(stub)
        (self.repo / "index").mkdir()
        self.manifest.write_text(json.dumps(OLD))
        (self.repo / "index" / "fts.sqlite").write_text("live-db")

    @property
    def manifest(self) -> Path:
        return self.repo / "index" / "manifest.json"

    def dotenv(self, text: str):
        (self.repo / ".env").write_text(text)

    def run(self, script="refresh.sh", *args, **over):
        # A from-scratch env (nothing inherited): the developer's GH_TOKEN /
        # SKIP_* / HEALTHCHECK_URL can't leak in, and the stubs win on PATH.
        env = {
            "PATH": f"{self.repo / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "ENV_PY": str(self.repo / "bin" / "envpy"),
            "REAL_PY": sys.executable,
            "STUB_LOG": str(self.log),
            "FAKE_MANIFEST": json.dumps(NEW),
            "SERVICE": "sandbox-svc",     # if a stub ever lost the PATH race: a no-op
            **{k: str(v) for k, v in over.items()},
        }
        # cwd is deliberately NOT the repo: the scripts must cd there themselves.
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args],
                              env=env, cwd=self.root, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def run_unit(self, **over):
        """What systemd does with the refresh unit: ExecStartPre (start ping),
        ExecStart (refresh.sh), then ExecStopPost (result ping) with the verdict
        variables it would set for that exit status and the run's output as
        the journal."""
        self.run("hc_ping.sh", "start", INVOCATION_ID=RUN_ID, **over)
        r = self.run("refresh.sh", INVOCATION_ID=RUN_ID, **over)
        verdict = dict(OK) if r.returncode == 0 else {
            "SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited",
            "EXIT_STATUS": r.returncode}
        post = self.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID,
                        FAKE_JOURNAL=r.stdout + r.stderr, **verdict, **over)
        assert post.returncode == 0
        return r

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def pings(self) -> list[str]:
        """The URL of every curl call, in order."""
        return [c.split()[-1] for c in self.calls() if c.startswith("curl")]

    def body(self) -> str:
        """What was piped to curl as the ping body."""
        path = Path(f"{self.log}.curl-stdin")
        return path.read_text() if path.exists() else ""

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.repo.iterdir()
                      if p.name.startswith("index."))


@pytest.fixture
def sandbox(tmp_path, stub):
    return Sandbox(tmp_path, stub)


# --- refresh.sh ------------------------------------------------------------

def test_scripts_are_executable():
    assert os.access(SCRIPTS / "refresh.sh", os.X_OK)
    assert os.access(SCRIPTS / "hc_ping.sh", os.X_OK)           # systemd execs both directly


def test_green_run_swaps_restarts_and_publishes(sandbox):
    r = sandbox.run()
    assert r.returncode == 0, r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"   # seeded from live
    assert sandbox.leftovers() == []                            # no index.staging / index.prev.*
    assert sandbox.calls() == [
        "python -m src.checkouts",
        "python -m src.ingest BIDS_INDEX_PATH=index.staging",
        "python - index.staging index",                         # the validation heredoc
        "sudo systemctl restart sandbox-svc",
        "package_index.sh --upload GH_TOKEN=<unset>",
    ]
    assert "staging OK: 305 chunks, built new" in r.stdout
    assert r.stdout.endswith("done.\n")


def test_refresh_never_pings_even_with_a_url(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    r = sandbox.run(HEALTHCHECK_URL=URL)
    assert r.returncode == 0, r.stderr
    assert sandbox.pings() == []                                # pinging is the unit's job


def test_ingest_failure_leaves_live_index_untouched(sandbox):
    r = sandbox.run(FAKE_INGEST_RC=7)
    assert r.returncode == 7                                    # the exit status is the contract
    assert "FAILED: exit 7" in r.stdout                         # the ERR trap says where it died
    assert json.loads(sandbox.manifest.read_text()) == OLD
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"
    assert not any(c.startswith(("sudo", "package_index.sh")) for c in sandbox.calls())
    assert sandbox.leftovers() == ["index.staging"]             # the next run clears it


def test_empty_staging_fails_validation(sandbox):
    r = sandbox.run(FAKE_MANIFEST='{"chunks": {}}')
    assert r.returncode != 0
    assert "staging index is empty" in r.stderr
    assert json.loads(sandbox.manifest.read_text()) == OLD      # never swapped in
    assert not any(c.startswith("sudo") for c in sandbox.calls())


# --- the shrink guard: a source that lost a big share of its chunks is a
#     harvest that failed quietly, and the total alone never shows it ---------

def shrunk(**chunks) -> str:
    return json.dumps({"chunks": {**OLD["chunks"], **chunks}, "built_at": "new"})


@pytest.mark.parametrize("staging", [shrunk(issues=100),                 # 200 -> 100
                                     json.dumps({"chunks": {"docs": 100}})])  # issues gone
def test_a_source_losing_half_its_chunks_fails_validation(sandbox, staging):
    r = sandbox.run(FAKE_MANIFEST=staging)
    assert r.returncode != 0
    assert "staging index shrank" in r.stderr and "ALLOW_SHRINK=1" in r.stderr
    assert "issues 200 -> " in r.stderr                         # names the source + both counts
    assert "staging OK" not in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == OLD      # live index untouched
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"
    assert not any(c.startswith(("sudo", "package_index.sh")) for c in sandbox.calls())
    assert sandbox.leftovers() == ["index.staging"]


def test_an_ordinary_small_decrease_passes(sandbox):
    r = sandbox.run(FAKE_MANIFEST=shrunk(issues=190))           # 200 -> 190: closed threads
    assert r.returncode == 0, r.stderr
    assert "staging OK: 290 chunks, built new" in r.stdout
    assert json.loads(sandbox.manifest.read_text())["chunks"]["issues"] == 190


def test_allow_shrink_lets_the_big_drop_through(sandbox):
    r = sandbox.run(FAKE_MANIFEST=shrunk(issues=100), ALLOW_SHRINK=1)
    assert r.returncode == 0, r.stderr
    assert "issues: 200 -> 100 chunks (50% lost)" in r.stderr   # still said out loud
    assert "WARNING: ALLOW_SHRINK=1" in r.stdout
    assert "staging OK: 200 chunks, built new" in r.stdout
    assert json.loads(sandbox.manifest.read_text())["chunks"]["issues"] == 100
    assert any(c.startswith("sudo") for c in sandbox.calls())  # swapped + restarted


def test_a_small_source_may_shrink_to_nothing(sandbox):
    live = {"chunks": {**OLD["chunks"], "neurostars": 2}, "built_at": "old"}   # cubids-sized
    sandbox.manifest.write_text(json.dumps(live))
    for staging in (shrunk(neurostars=0), shrunk()):            # 2 -> 0, and 2 -> absent
        r = sandbox.run(FAKE_MANIFEST=staging)
        assert r.returncode == 0, r.stderr
        assert "shrank" not in r.stderr and "lost" not in r.stderr
        sandbox.manifest.write_text(json.dumps(live))


def test_skip_knobs(sandbox):
    r = sandbox.run(SKIP_CHECKOUTS=1, SKIP_RESTART=1, SKIP_PUBLISH=1)
    assert r.returncode == 0, r.stderr
    assert sandbox.calls() == ["python -m src.ingest BIDS_INDEX_PATH=index.staging",
                               "python - index.staging index"]
    assert "skipping service restart" in r.stdout


def test_no_live_index_errors_out(sandbox):
    sandbox.manifest.unlink()
    r = sandbox.run()
    assert r.returncode == 1 and "no live index" in r.stderr
    assert sandbox.calls() == []                                # stopped before doing anything


def test_publish_failure_is_only_a_warning(sandbox):
    r = sandbox.run(FAKE_PUBLISH_RC=1)
    assert r.returncode == 0                                    # today's contract: non-fatal
    assert "WARNING: asset publish failed" in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == NEW      # the live index is current


def test_publish_token_prefers_gh_publish_token(sandbox):
    sandbox.dotenv("# comment\nGITHUB_TOKEN=read\nGH_PUBLISH_TOKEN='pub'\n")
    assert sandbox.run().returncode == 0
    assert "package_index.sh --upload GH_TOKEN=pub" in sandbox.calls()   # quotes stripped


def test_publish_token_falls_back_to_github_token(sandbox):
    sandbox.dotenv("GITHUB_TOKEN = read\r\n")                   # no GH_PUBLISH_TOKEN; spaces, CR
    r = sandbox.run()
    assert r.returncode == 0, r.stdout + r.stderr               # used to abort: exit 1, no output
    assert "package_index.sh --upload GH_TOKEN=read" in sandbox.calls()
    assert b"GH_TOKEN=read\n" in sandbox.log.read_bytes()       # and no stray CR on it


def test_dotenv_without_any_token_still_runs(sandbox):
    sandbox.dotenv("OPENAI_API_KEY=sk-test\n")
    assert sandbox.run().returncode == 0
    assert "package_index.sh --upload GH_TOKEN=<unset>" in sandbox.calls()


def test_no_dotenv_still_runs(sandbox):
    assert not (sandbox.repo / ".env").exists()
    assert sandbox.run().returncode == 0


def test_exported_gh_token_wins_over_dotenv(sandbox):
    sandbox.dotenv("GH_PUBLISH_TOKEN=pub\n")
    assert sandbox.run(GH_TOKEN="shell").returncode == 0
    assert "package_index.sh --upload GH_TOKEN=shell" in sandbox.calls()


# --- hc_ping.sh ------------------------------------------------------------

def test_ping_is_inert_without_a_url(sandbox):
    assert sandbox.run("hc_ping.sh", "start").returncode == 0   # no .env at all
    sandbox.dotenv("GITHUB_TOKEN=read\n")                       # .env without the key
    assert sandbox.run("hc_ping.sh", "start").returncode == 0
    r = sandbox.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID, **OK)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert sandbox.calls() == []                                # no curl, no journalctl


def test_start_ping(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    r = sandbox.run("hc_ping.sh", "start")
    assert r.returncode == 0 and "sent /start" in r.stdout
    assert sandbox.calls() == [f"curl -gfsS -m 10 -o /dev/null --data-raw  {URL}/start"]


def test_success_ping_carries_the_journal_tail(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL = '{URL}/'\r\n")           # quotes, spaces, slash, CR
    r = sandbox.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID,
                    FAKE_JOURNAL="[refresh] done.\n", **OK)
    assert r.returncode == 0
    assert sandbox.pings() == [URL]                             # bare URL = success
    assert sorted(sandbox.calls()) == [                         # (pipeline: order varies)
        f"curl -gfsS -m 10 --retry 3 --retry-max-time 30 -o /dev/null --data-binary @- {URL}",
        f"journalctl --no-pager -q -o cat -n 200 _SYSTEMD_INVOCATION_ID={RUN_ID}"
        f" + INVOCATION_ID={RUN_ID}",                           # this run's lines only
    ]
    assert sandbox.body() == ("bids-assistant refresh: result=success exited=0\n"
                              "[refresh] done.\n")


def test_exit_status_is_carried_in_the_failure_ping(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    for status in (1, 3, 75, 203, 255):
        sandbox.run("hc_ping.sh", "result", SERVICE_RESULT="exit-code",
                    EXIT_CODE="exited", EXIT_STATUS=status)
    assert sandbox.pings() == [f"{URL}/{n}" for n in (1, 3, 75, 203, 255)]


def test_every_other_outcome_is_a_plain_failure_ping(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    outcomes = [
        {"SERVICE_RESULT": "timeout", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"},
        {"SERVICE_RESULT": "timeout", "EXIT_CODE": "exited", "EXIT_STATUS": "0"},
        {"SERVICE_RESULT": "oom-kill", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"},
        {"SERVICE_RESULT": "oom-kill", "EXIT_CODE": "exited", "EXIT_STATUS": "0"},
        {"SERVICE_RESULT": "oom-kill", "EXIT_CODE": "exited", "EXIT_STATUS": "137"},
        {"SERVICE_RESULT": "signal", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"},
        {"SERVICE_RESULT": "success", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"},
        {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "256"},
        {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": ""},
        {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "0"},
        {},                                                     # run by hand / vars missing
    ]
    for env in outcomes:
        assert sandbox.run("hc_ping.sh", "result", **env).returncode == 0
    assert sandbox.pings() == [f"{URL}/fail"] * len(outcomes)   # never /TERM, never success
    assert "result=timeout killed=TERM\n" in sandbox.body()
    assert "result=unset unset=unset\n" in sandbox.body()


def test_ping_body_never_contains_secrets(sandbox):
    sandbox.dotenv(f"GITHUB_TOKEN=github_pat_AAAA1111\nOPENAI_API_KEY=\"sk-BBBB2222\"\n"
                   f"HEALTHCHECK_URL={URL}\nSHORT=abc\n")
    journal = ("401 for token github_pat_AAAA1111 (github_pat_AAAA1111)\n"
               f"key sk-BBBB2222 url {URL}/fail abc\n"
               "not in .env: ghp_CCCCCCCCCCCCCCCC3333 sk-DDDDDDDDDDDDDDDD4444\n"
               "clone https://user:hunter2hunter2@github.com/x.git "
               "https://hc-ping.com/5555-other-uuid/fail\n")
    sandbox.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID, FAKE_JOURNAL=journal)
    body = sandbox.body()
    for secret in ("AAAA1111", "BBBB2222", "1234-uuid", "3333", "4444", "hunter2", "5555"):
        assert secret not in body
    assert "401 for token [REDACTED] ([REDACTED])" in body
    assert "url [REDACTED]/fail abc" in body                    # short values are left alone


def test_a_secret_cut_by_the_size_limit_is_still_scrubbed(sandbox):
    token = "github_pat_" + "A1b2C3d4" * 8                      # fake; a .env value
    other = "ghp_" + "E5f6G7h8" * 5                             # token-shaped, not in .env
    sandbox.dotenv(f"GITHUB_TOKEN={token}\nHEALTHCHECK_URL={URL}\n")
    # 7970 bytes of filler: the last 8000 bytes of the RAW journal start 30 bytes
    # before the end of the secret line, inside the token. Truncating before
    # scrubbing would leave a prefix-less token tail that no rule recognises.
    filler = (("x" * 79 + "\n") * 100)[30:]
    for line in (f"Bearer {token}\n", f"bad credentials {other}\n"):
        sandbox.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID,
                    FAKE_JOURNAL=line + filler)
    body = sandbox.body()
    for tok in (token, other):
        assert not any(tok[i:i + 8] in body for i in range(len(tok) - 7))


def test_ping_body_is_small_valid_utf8_and_keeps_the_end(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    journal = "日" * 4000 + "\nFAILED: exit 1 at line 99\n"
    assert journal.encode()[-8000] & 0xC0 == 0x80               # the cut lands mid-character
    sandbox.run("hc_ping.sh", "result", INVOCATION_ID=RUN_ID, FAKE_JOURNAL=journal)
    raw = Path(f"{sandbox.log}.curl-stdin").read_bytes()
    assert len(raw) < 10_000                                    # whole in the alert e-mail
    text = raw.decode("utf-8")                                  # no torn character
    assert text.startswith("bids-assistant refresh: result=unset")
    assert text.endswith("FAILED: exit 1 at line 99\n")


def test_no_journal_without_a_systemd_invocation_id(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    sandbox.run("hc_ping.sh", "result", FAKE_JOURNAL="x\n", **OK)
    sandbox.run("hc_ping.sh", "result", INVOCATION_ID="not hex; rm -rf", **OK)
    assert not any(c.startswith("journalctl") for c in sandbox.calls())
    assert sandbox.body() == "bids-assistant refresh: result=success exited=0\n" * 2


def test_ping_failure_never_fails_the_caller(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    for args, env in ((("start",), {}), (("result",), OK)):
        r = sandbox.run("hc_ping.sh", *args, FAKE_CURL_RC=22, **env)
        assert r.returncode == 0 and "not delivered (curl exit 22)" in r.stdout
        assert URL not in r.stdout + r.stderr                   # the URL is a secret


def test_ping_url_from_environment_wins(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    sandbox.run("hc_ping.sh", "start", HEALTHCHECK_URL="https://hc.invalid/other")
    assert sandbox.pings() == ["https://hc.invalid/other/start"]


def test_ping_works_without_a_dotenv_file(sandbox):
    r = sandbox.run("hc_ping.sh", "result", HEALTHCHECK_URL=URL, INVOCATION_ID=RUN_ID,
                    FAKE_JOURNAL=f"posting to {URL}\n", **OK)
    assert r.returncode == 0 and r.stderr == ""
    assert sandbox.pings() == [URL] and "posting to [REDACTED]" in sandbox.body()


def test_ping_refuses_a_non_http_url_and_bad_usage(sandbox):
    r = sandbox.run("hc_ping.sh", "start", HEALTHCHECK_URL="file:///etc/passwd")
    assert r.returncode == 0 and "not an http(s) URL" in r.stdout
    assert sandbox.run("hc_ping.sh", "bogus", HEALTHCHECK_URL=URL).returncode == 0
    assert sandbox.run("hc_ping.sh", HEALTHCHECK_URL=URL).returncode == 0
    assert sandbox.calls() == []


# --- the unit, simulated: ExecStartPre -> ExecStart -> ExecStopPost --------

def test_unit_green_run_pings_start_then_success(sandbox):
    sandbox.dotenv(f"GITHUB_TOKEN=read\nHEALTHCHECK_URL={URL}\n")
    r = sandbox.run_unit()
    assert r.returncode == 0, r.stderr
    assert sandbox.pings() == [f"{URL}/start", URL]
    assert "result=success exited=0" in sandbox.body() and "done." in sandbox.body()
    assert json.loads(sandbox.manifest.read_text()) == NEW


def test_unit_ingest_failure_pings_its_exit_status(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    r = sandbox.run_unit(FAKE_INGEST_RC=7, FAKE_INGEST_SAYS="Traceback: boom")
    assert r.returncode == 7
    assert sandbox.pings() == [f"{URL}/start", f"{URL}/7"]
    assert "Traceback: boom" in sandbox.body() and "FAILED: exit 7" in sandbox.body()
    assert json.loads(sandbox.manifest.read_text()) == OLD      # live index untouched


def test_unit_without_a_url_refreshes_and_pings_nothing(sandbox):
    for dotenv in (None, "GITHUB_TOKEN=read\n"):                # no .env / key absent
        if dotenv:
            sandbox.dotenv(dotenv)
        assert sandbox.run_unit().returncode == 0
    assert sandbox.pings() == []


def test_unit_broken_or_slow_curl_cannot_hurt_the_refresh(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    assert sandbox.run_unit(FAKE_CURL_RC=7).returncode == 0     # hc-ping unreachable
    t0 = time.monotonic()
    assert sandbox.run_unit(FAKE_CURL_SLEEP=1).returncode == 0  # slow endpoint
    assert time.monotonic() - t0 < 20                           # two pings, ~1 s each
    assert json.loads(sandbox.manifest.read_text()) == NEW
