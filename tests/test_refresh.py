"""refresh.sh, deploy.sh, fetch_index.sh, package_index.sh and hc_ping.sh end to
end in a throwaway repo: the real scripts (copied) with python, curl, git, gh,
flock, journalctl, sudo, systemctl (and, in the refresh tests, package_index.sh)
replaced by one recording stub — no network, no real index, no service restart."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash not installed")

# The scripts see PATH=<sandbox>/bin:/usr/bin:/bin; flock(1) lives in /usr/bin on
# Linux and does not exist on macOS (where the scripts run unlocked).
REAL_FLOCK = shutil.which("flock", path="/usr/bin:/bin")
needs_flock = pytest.mark.skipif(REAL_FLOCK is None, reason="no flock(1) here (macOS)")
# macOS tar applies a ._<name> entry to the file it precedes as metadata rather
# than unpacking it; the stray-file failure these guard against is GNU tar's
# (the server's, and CI's).
gnu_tar = pytest.mark.skipif(sys.platform == "darwin",
                             reason="macOS tar treats ._ entries as metadata")

OLD = {"chunks": {"docs": 100, "issues": 200}, "built_at": "old"}
NEW = {"chunks": {"docs": 100, "issues": 205}, "built_at": "new"}
URL = "https://hc.invalid/ping/1234-uuid"     # .invalid: can never reach a real check
OK = {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}
RUN_ID = "0123456789abcdef0123456789abcdef"   # the shape of systemd's $INVOCATION_ID

# One multi-call stub, symlinked under every name the scripts shell out to, that
# appends "<name> <args>" to $STUB_LOG. (One file, not several: macOS spends ~0.4 s
# vetting each brand-new executable on first exec; symlinks to it are free.)
#   envpy             ENV_PY stand-in: -m src.checkouts / -m src.ingest (writes a
#                     manifest into $BIDS_INDEX_PATH) / -m pip / "- <staging>" (the
#                     stdin-fed validation heredoc, handed to the real interpreter)
#   package_index.sh  records its args and the GH_TOKEN it was given (refresh.sh
#                     tests; the package_index.sh tests swap in the real script)
#   gh                answers --version like the real CLI (unlogged); release
#                     upload exits $FAKE_GH_RC; release download exits
#                     $FAKE_GH_RC or copies $FAKE_TARBALL to --output
#   tar               (only when a test asks for it) writes a partial archive
#                     and fails
#   curl              like real curl, reads stdin only when asked to (@-); with
#                     -o <file> and $FAKE_TARBALL set, "downloads" that file
#   git               records; answers remote.origin.url and rev-parse
#   flock             exits $FAKE_FLOCK_RC (1 = "someone else holds the lock")
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
      "-m pip") exit 0 ;;
      "- "*) exec "$REAL_PY" "$@" ;;
      *) echo "fake python: unexpected args: $*" >&2; exit 97 ;;
    esac ;;
  package_index.sh)
    echo "$name $* GH_TOKEN=${GH_TOKEN:-<unset>}" >> "$STUB_LOG"
    case "${1:-}" in
      "") exit "${FAKE_PACKAGE_RC:-0}" ;;              # build the tarball
      --upload-only) exit "${FAKE_PUBLISH_RC:-0}" ;;   # publish it
      *) exit 98 ;;
    esac ;;
  gh)
    if [ "${1:-}" = --version ]; then
      echo "gh version 2 (https://github.com/cli/cli/releases)"; exit 0
    fi
    echo "$name $*" >> "$STUB_LOG"
    case "${1:-} ${2:-}" in                                   # view/create always succeed
      "release upload"|"release download") [ "${FAKE_GH_RC:-0}" = 0 ] || exit "$FAKE_GH_RC" ;;
    esac
    out=""; while [ $# -gt 0 ]; do [ "$1" = --output ] && out="${2:-}"; shift; done
    [ -z "$out" ] || cp "$FAKE_TARBALL" "$out" ;;           # download --output: deliver it
  tar)
    echo "truncated archive" > "$2"
    exit 2 ;;
  curl)
    echo "$name $*" >> "$STUB_LOG"
    case " $* " in *"@-"*) cat >> "$STUB_LOG.curl-stdin" ;; esac
    [ -z "${FAKE_CURL_SLEEP:-}" ] || /bin/sleep "$FAKE_CURL_SLEEP"
    [ "${FAKE_CURL_RC:-0}" = 0 ] || exit "$FAKE_CURL_RC"
    out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="${2:-}"; shift; done
    [ -z "$out" ] || [ -z "${FAKE_TARBALL:-}" ] || cp "$FAKE_TARBALL" "$out" ;;
  git)
    echo "$name $*" >> "$STUB_LOG"
    case "$*" in
      "config --get remote.origin.url") echo "git@github.com:PennLINC/linc-bids-llm.git" ;;
      "rev-parse --short HEAD") echo "abc1234" ;;
    esac ;;
  flock)
    echo "$name $*" >> "$STUB_LOG"
    exit "${FAKE_FLOCK_RC:-0}" ;;
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
        self._stub = stub
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        for name in ("refresh.sh", "deploy.sh", "fetch_index.sh", "hc_ping.sh"):
            shutil.copy(SCRIPTS / name, self.repo / "scripts" / name)
        for rel in ("scripts/package_index.sh", "bin/envpy", "bin/curl", "bin/gh",
                    "bin/git", "bin/flock", "bin/sleep", "bin/journalctl", "bin/sudo",
                    "bin/systemctl"):
            (self.repo / rel).symlink_to(stub)
        # python3 (fetch_index.sh / package_index.sh print the manifest with it): real
        (self.repo / "bin" / "python3").symlink_to(sys.executable)
        (self.repo / "index").mkdir()
        self.manifest.write_text(json.dumps(OLD))
        (self.repo / "index" / "fts.sqlite").write_text("live-db")

    @property
    def manifest(self) -> Path:
        return self.repo / "index" / "manifest.json"

    def fake(self, name: str):
        """Put one more stubbed command on PATH."""
        (self.repo / "bin" / name).symlink_to(self._stub)

    @property
    def lockfile(self) -> Path:
        return self.repo / ".refresh.lock"

    def use_real_flock(self):
        """Drop the flock stub so the scripts find /usr/bin/flock (Linux) or
        nothing at all (macOS) — what they meet outside the sandbox."""
        (self.repo / "bin" / "flock").unlink()

    def dotenv(self, text: str):
        (self.repo / ".env").write_text(text)

    def env(self, **over) -> dict:
        # A from-scratch env (nothing inherited): the developer's GH_TOKEN /
        # SKIP_* / HEALTHCHECK_URL can't leak in, and the stubs win on PATH.
        return {
            "PATH": f"{self.repo / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "ENV_PY": str(self.repo / "bin" / "envpy"),
            "REAL_PY": sys.executable,
            "STUB_LOG": str(self.log),
            "FAKE_MANIFEST": json.dumps(NEW),
            "SERVICE": "sandbox-svc",     # if a stub ever lost the PATH race: a no-op
            **{k: str(v) for k, v in over.items()},
        }

    def run(self, script="refresh.sh", *args, **over):
        # cwd is deliberately NOT the repo: the scripts must cd there themselves.
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args],
                              env=self.env(**over), cwd=self.root, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def start(self, script="refresh.sh", *args, **over) -> subprocess.Popen:
        """run(), but in the background (for a script that has to wait on us)."""
        return subprocess.Popen(["bash", str(self.repo / "scripts" / script), *args],
                                env=self.env(**over), cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

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


@pytest.fixture
def held_lock(sandbox):
    """.refresh.lock held from this process with the real flock(2), exactly as a
    concurrent refresh or deploy holds it (the fd is not inherited: subprocess
    closes fds by default). Yields a release(); teardown releases regardless.
    The sandbox uses the real flock(1)."""
    import fcntl
    sandbox.use_real_flock()
    fd = os.open(sandbox.lockfile, os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield lambda: fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


# --- refresh.sh ------------------------------------------------------------

def test_scripts_are_executable():
    assert os.access(SCRIPTS / "refresh.sh", os.X_OK)
    assert os.access(SCRIPTS / "hc_ping.sh", os.X_OK)           # systemd execs both directly


def test_green_run_swaps_packages_restarts_and_publishes(sandbox):
    r = sandbox.run()
    assert r.returncode == 0, r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"   # seeded from live
    assert sandbox.leftovers() == []                            # no index.staging / index.prev.*
    calls, build = sandbox.calls(), "package_index.sh  GH_TOKEN=<unset>"
    assert calls.index(build) == calls.index("sudo systemctl restart sandbox-svc") - 1
    assert [c for c in calls if c != build] == [                # tarball BEFORE the restart
        "flock -n 9",                                           # the lock, before anything else
        "python -m src.checkouts",
        "python -m src.ingest BIDS_INDEX_PATH=index.staging",
        "python - index.staging index",                         # the validation heredoc
        "sudo systemctl restart sandbox-svc",
        "package_index.sh --upload-only GH_TOKEN=<unset>",
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
    assert sandbox.calls() == ["flock -n 9",
                               "python -m src.ingest BIDS_INDEX_PATH=index.staging",
                               "python - index.staging index"]
    assert "skipping service restart" in r.stdout


def test_no_live_index_errors_out(sandbox):
    sandbox.manifest.unlink()
    r = sandbox.run()
    assert r.returncode == 1 and "no live index" in r.stderr
    assert sandbox.calls() == ["flock -n 9"]                    # stopped before doing anything


def test_publish_failure_fails_the_run_after_everything_else(sandbox):
    r = sandbox.run(FAKE_PUBLISH_RC=1)
    assert r.returncode == 3                                    # reported, not swallowed
    assert "ERROR: asset publish failed" in r.stdout
    assert "FAILED after the swap: publish." in r.stdout
    assert "done." not in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == NEW      # the refresh itself landed
    assert "sudo systemctl restart sandbox-svc" in sandbox.calls()


def test_package_failure_still_restarts_and_skips_the_upload(sandbox):
    r = sandbox.run(FAKE_PACKAGE_RC=1)
    assert r.returncode == 3 and "FAILED after the swap: package." in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert sandbox.calls()[-1] == "sudo systemctl restart sandbox-svc"   # no upload


def test_restart_failure_still_publishes_then_fails(sandbox):
    r = sandbox.run(FAKE_SUDO_RC=1)
    assert r.returncode == 3 and "FAILED after the swap: restart." in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == NEW      # no rollback
    assert sandbox.calls()[-1].startswith("package_index.sh --upload-only")


def test_every_step_after_the_swap_can_fail_and_all_are_named(sandbox):
    r = sandbox.run(FAKE_SUDO_RC=1, FAKE_PUBLISH_RC=1)
    assert r.returncode == 3 and "FAILED after the swap: restart publish." in r.stdout


def test_skip_publish_neither_packages_nor_uploads(sandbox):
    r = sandbox.run(SKIP_PUBLISH=1, FAKE_PACKAGE_RC=1, FAKE_PUBLISH_RC=1)
    assert r.returncode == 0, r.stdout                          # the opt-out really opts out
    assert not any(c.startswith("package_index.sh") for c in sandbox.calls())


def test_publish_token_prefers_gh_publish_token(sandbox):
    sandbox.dotenv("# comment\nGITHUB_TOKEN=read\nGH_PUBLISH_TOKEN='pub'\n")
    assert sandbox.run().returncode == 0
    assert "package_index.sh --upload-only GH_TOKEN=pub" in sandbox.calls()   # quotes stripped


def test_publish_token_falls_back_to_github_token(sandbox):
    sandbox.dotenv("GITHUB_TOKEN = read\r\n")                   # no GH_PUBLISH_TOKEN; spaces, CR
    r = sandbox.run()
    assert r.returncode == 0, r.stdout + r.stderr               # used to abort: exit 1, no output
    assert "package_index.sh --upload-only GH_TOKEN=read" in sandbox.calls()
    assert b"GH_TOKEN=read\n" in sandbox.log.read_bytes()       # and no stray CR on it


def test_dotenv_without_any_token_still_runs(sandbox):
    sandbox.dotenv("OPENAI_API_KEY=sk-test\n")
    assert sandbox.run().returncode == 0
    assert "package_index.sh --upload-only GH_TOKEN=<unset>" in sandbox.calls()


def test_no_dotenv_still_runs(sandbox):
    assert not (sandbox.repo / ".env").exists()
    assert sandbox.run().returncode == 0


def test_exported_gh_token_wins_over_dotenv(sandbox):
    sandbox.dotenv("GH_PUBLISH_TOKEN=pub\n")
    assert sandbox.run(GH_TOKEN="shell").returncode == 0
    assert "package_index.sh --upload-only GH_TOKEN=shell" in sandbox.calls()


# --- package_index.sh (the real script; gh stubbed) ------------------------

def _real_package_script(sandbox):
    (sandbox.repo / "scripts" / "package_index.sh").unlink()    # replace the stub
    shutil.copy(SCRIPTS / "package_index.sh", sandbox.repo / "scripts")
    # gh (stub, first on PATH: a real gh is never reached) and python3 are
    # already there — the sandbox puts both on PATH for every script.


def test_plain_package_builds_the_tarball_and_uploads_nothing(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run("package_index.sh")
    assert r.returncode == 0, r.stderr
    assert (sandbox.repo / "dist" / "index.tgz").stat().st_size > 0
    assert not (sandbox.repo / "dist" / "index.tgz.tmp").exists()   # built, then renamed
    assert sandbox.calls() == []                                # gh never called


def test_upload_builds_then_publishes(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run("package_index.sh", "--upload")
    assert r.returncode == 0, r.stderr
    assert f"using GitHub CLI: {sandbox.repo / 'bin' / 'gh'}" in r.stdout   # the stub
    assert (sandbox.repo / "dist" / "index.tgz").stat().st_size > 0
    assert sandbox.calls()[-1] == "gh release upload index-latest dist/index.tgz --clobber"


def test_upload_only_publishes_the_existing_tarball_untouched(sandbox):
    _real_package_script(sandbox)
    (sandbox.repo / "dist").mkdir()
    (sandbox.repo / "dist" / "index.tgz").write_text("snapshot taken before the restart")
    r = sandbox.run("package_index.sh", "--upload-only")
    assert r.returncode == 0, r.stderr
    assert (sandbox.repo / "dist" / "index.tgz").read_text().startswith("snapshot")  # no re-tar
    assert sandbox.calls()[-1] == "gh release upload index-latest dist/index.tgz --clobber"


def test_upload_only_without_a_tarball_fails(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run("package_index.sh", "--upload-only")
    assert r.returncode == 1 and "no dist/index.tgz" in r.stderr
    assert sandbox.calls() == []                                # gh never called


def test_a_failed_upload_fails_the_script(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run("package_index.sh", "--upload", FAKE_GH_RC=1)
    assert r.returncode == 1                                    # what refresh.sh turns into exit 3
    assert sandbox.calls()[-1].startswith("gh release upload")  # it got as far as the upload


def test_a_failed_tar_keeps_the_previous_tarball(sandbox):
    _real_package_script(sandbox)
    sandbox.fake("tar")
    (sandbox.repo / "dist").mkdir()
    (sandbox.repo / "dist" / "index.tgz").write_text("last good snapshot")
    r = sandbox.run("package_index.sh")
    assert r.returncode != 0
    assert (sandbox.repo / "dist" / "index.tgz").read_text() == "last good snapshot"


def test_package_leaves_macos_metadata_out_of_the_tarball(sandbox):
    # macOS tar adds a ._<name> entry for every file with extended attributes,
    # which Linux tar unpacks as real files (see the fetch_index.sh tests).
    # Linux tar adds none, so there the check holds trivially.
    _real_package_script(sandbox)
    if sys.platform == "darwin":
        subprocess.run(["xattr", "-w", "org.pennlinc.test", "1",
                        str(sandbox.repo / "index" / "fts.sqlite")], check=True)
    r = sandbox.run("package_index.sh")
    assert r.returncode == 0, r.stderr
    with tarfile.open(sandbox.repo / "dist" / "index.tgz") as tar:
        names = tar.getnames()
    assert "index/fts.sqlite" in names
    assert not [n for n in names if n.rsplit("/", 1)[-1].startswith("._")]


def test_refresh_with_the_real_package_script_publishes_the_new_index(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = sandbox.calls()
    assert calls[-1] == "gh release upload index-latest dist/index.tgz --clobber"
    restart = calls.index("sudo systemctl restart sandbox-svc")
    assert not any(c.startswith("gh") for c in calls[:restart])   # upload only after it
    with tarfile.open(sandbox.repo / "dist" / "index.tgz") as tar:
        packed = json.load(tar.extractfile("index/manifest.json"))
    assert packed == NEW                                        # the swapped-in index


def test_refresh_with_the_real_package_script_reports_a_failed_upload(sandbox):
    _real_package_script(sandbox)
    r = sandbox.run(FAKE_GH_RC=1)                               # e.g. an expired token
    assert r.returncode == 3 and "FAILED after the swap: publish." in r.stdout
    assert json.loads(sandbox.manifest.read_text()) == NEW


# --- the lock: refresh.sh / deploy.sh / fetch_index.sh never overlap ---------
# Two refreshes at once: B's `rm -rf index.staging` deletes A's in-flight build,
# B swaps, A's swap then fails after `mv index index.prev.<pid>` — no index/ left
# and every later run aborts with "no live index". A deploy under a running
# ingest pip-installs into the env the ingest is using. One lock for all three.

def test_refresh_waits_then_exits_75_when_the_lock_is_held(sandbox):
    r = sandbox.run(FAKE_FLOCK_RC=1)
    assert r.returncode == 75, r.stdout + r.stderr
    assert sandbox.calls() == ["flock -n 9", "flock -w 600 9"]  # tried, waited, nothing else
    assert "another refresh/deploy is running" in r.stdout
    assert "waiting up to 600s" in r.stdout and "exit 75" in r.stdout
    assert "FAILED" not in r.stdout                             # a clean stand-down, not an error
    assert json.loads(sandbox.manifest.read_text()) == OLD
    assert sandbox.leftovers() == []                            # no staging dir was even seeded


def test_lock_wait_knob(sandbox):
    r = sandbox.run(FAKE_FLOCK_RC=1, LOCK_WAIT=7)
    assert r.returncode == 75
    assert sandbox.calls() == ["flock -n 9", "flock -w 7 9"]


def test_deploy_green_run(sandbox):
    r = sandbox.run("deploy.sh")
    assert r.returncode == 0, r.stderr
    assert sandbox.calls() == [
        "flock -n 9",
        "git pull --ff-only",
        "python -m pip install -q -r requirements.txt",
        "sudo systemctl restart sandbox-svc",
        "sudo systemctl --no-pager --lines=0 status sandbox-svc",
        "git rev-parse --short HEAD",
    ]
    assert r.stdout.endswith("deployed abc1234\n")


def test_deploy_exits_75_at_once_when_the_lock_is_held(sandbox):
    r = sandbox.run("deploy.sh")                                # baseline: 0 with the lock free
    assert r.returncode == 0
    sandbox.log.unlink()
    r = sandbox.run("deploy.sh", FAKE_FLOCK_RC=1)
    assert r.returncode == 75
    assert sandbox.calls() == ["flock -n 9"]                    # non-blocking: no -w, no git pull
    assert "a refresh is running — wait for it to finish, then retry" in r.stderr


def test_fetch_exits_75_at_once_when_the_lock_is_held(sandbox):
    r = sandbox.run("fetch_index.sh", FAKE_FLOCK_RC=1)
    assert r.returncode == 75
    assert sandbox.calls() == ["flock -n 9"]
    assert "a refresh or deploy is running" in r.stderr
    assert json.loads(sandbox.manifest.read_text()) == OLD and sandbox.leftovers() == []


def test_lock_file_is_gitignored():
    root = SCRIPTS.parent
    assert ".refresh.lock" in (root / ".gitignore").read_text().split()


@needs_flock
def test_real_flock_refresh_gives_up_after_lock_wait(sandbox, held_lock):
    t0 = time.monotonic()
    r = sandbox.run(LOCK_WAIT=1)
    assert r.returncode == 75, r.stdout + r.stderr
    assert 1 <= time.monotonic() - t0 < 30
    assert "waiting up to 1s" in r.stdout and "giving up" in r.stdout
    assert sandbox.calls() == []                                # not even the checkout update
    assert json.loads(sandbox.manifest.read_text()) == OLD and sandbox.leftovers() == []


@needs_flock
def test_real_flock_refresh_proceeds_once_the_lock_is_released(sandbox, held_lock):
    proc = sandbox.start("refresh.sh", LOCK_WAIT=30)
    first = proc.stdout.readline()                              # blocks until it says so
    assert "waiting up to 30s" in first
    assert sandbox.calls() == []                                # it really is waiting
    held_lock()                                                 # the other run finishes
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, out + err
    assert "lock acquired." in out and out.endswith("done.\n")
    assert json.loads(sandbox.manifest.read_text()) == NEW


@needs_flock
def test_real_flock_deploy_and_fetch_bounce_off_a_held_lock(sandbox, held_lock):
    for script in ("deploy.sh", "fetch_index.sh"):
        t0 = time.monotonic()
        r = sandbox.run(script)
        assert r.returncode == 75, script + r.stderr
        assert time.monotonic() - t0 < 10                       # no waiting
    assert sandbox.calls() == []
    assert json.loads(sandbox.manifest.read_text()) == OLD


# --- fetch_index.sh: the live index/ goes only once the new one checks out ----
# It used to move index/ to index.bak.<epoch> BEFORE downloading: a failed
# download (gh installed but not logged in refuses even a public repo; the asset
# is briefly gone during every re-publish) left no index/, deploy.sh died before
# its restart, and every nightly refresh after that failed with "no live index".

def tarball(path: Path, manifest=NEW, with_manifest=True, appledouble=False) -> Path:
    """A release-asset lookalike: index/manifest.json + index/fts.sqlite.
    `appledouble`: also entries named like the ._<name> files macOS tar adds
    beside each file (and beside index/ itself) unless COPYFILE_DISABLE is set.
    Their payload is plain bytes: Linux tar only sees the names, and macOS tar
    would try to apply anything with the AppleDouble magic as metadata."""
    with tarfile.open(path, "w:gz") as tf:
        def add(name, data: bytes):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if appledouble:
            add("._index", b"macOS metadata")
        if with_manifest:
            text = manifest if isinstance(manifest, str) else json.dumps(manifest)
            if appledouble:
                add("index/._manifest.json", b"macOS metadata")
            add("index/manifest.json", text.encode())
        if appledouble:
            add("index/._fts.sqlite", b"macOS metadata")
        add("index/fts.sqlite", b"new-db")
    return path


@pytest.fixture
def asset(sandbox):
    return tarball(sandbox.root / "index.tgz")


def test_fetch_swaps_a_verified_tarball_in(sandbox, asset):
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "new-db"
    assert sandbox.calls() == ["flock -n 9", "gh release download index-latest"
                               " --pattern index.tgz --output dist/index.tgz.tmp --clobber"]
    assert "downloaded index:" in r.stdout                      # the manifest check ran
    assert "chunks:          {'docs': 100, 'issues': 205}" in r.stdout
    (backup,) = sandbox.leftovers()                              # one backup, nothing else
    assert backup.startswith("index.bak.") and backup[len("index.bak."):].isdigit()
    assert json.loads((sandbox.repo / backup / "manifest.json").read_text()) == OLD
    assert f"kept one backup: {backup}/" in r.stdout
    assert (sandbox.repo / "dist" / "index.tgz").read_bytes() == asset.read_bytes()
    assert not (sandbox.repo / "dist" / "index.tgz.tmp").exists()


def test_fetch_falls_back_to_curl_when_gh_refuses(sandbox, asset):
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset, FAKE_GH_RC=4)   # gh: not logged in
    assert r.returncode == 0, r.stdout + r.stderr
    assert "trying the public URL" in r.stdout
    assert ("curl -fL --retry 3 -o dist/index.tgz.tmp https://github.com/PennLINC/"
            "linc-bids-llm/releases/download/index-latest/index.tgz") in sandbox.calls()
    assert json.loads(sandbox.manifest.read_text()) == NEW


def test_fetch_download_failure_leaves_index_untouched(sandbox, asset):
    (sandbox.repo / "dist").mkdir()
    (sandbox.repo / "dist" / "index.tgz").write_bytes(b"last good tarball")
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset, FAKE_GH_RC=4, FAKE_CURL_RC=22)
    assert r.returncode == 1
    assert "download failed; index/ is untouched" in r.stderr
    assert json.loads(sandbox.manifest.read_text()) == OLD
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"
    assert sandbox.leftovers() == []                            # no index.bak.*, no index.fetch.*
    assert sorted(p.name for p in (sandbox.repo / "dist").iterdir()) == ["index.tgz"]
    assert (sandbox.repo / "dist" / "index.tgz").read_bytes() == b"last good tarball"
    assert [c.split()[0] for c in sandbox.calls()] == ["flock", "gh", "git", "curl"]   # git: the
    assert "downloaded index:" not in r.stdout                  #   slug for the URL; nothing unpacked


@pytest.mark.parametrize("bad", [
    pytest.param({"chunks": {}}, id="empty-chunks"),
    pytest.param({"chunks": {"docs": 0}}, id="zero-chunks"),
    pytest.param({"built_at": "x"}, id="no-chunks-key"),
    pytest.param({"chunks": [1, 2]}, id="chunks-not-a-map"),
    pytest.param("not json", id="not-json"),
    pytest.param(None, id="no-manifest"),
    pytest.param(b"not a tarball at all", id="not-a-tarball"),
])
def test_fetch_rejects_a_tarball_without_a_valid_manifest(sandbox, bad):
    path = sandbox.root / "bad.tgz"
    if isinstance(bad, bytes):
        path.write_bytes(bad)
    else:
        tarball(path, manifest=bad, with_manifest=bad is not None)
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "index/ is untouched" in r.stderr
    assert json.loads(sandbox.manifest.read_text()) == OLD
    assert (sandbox.repo / "index" / "fts.sqlite").read_text() == "live-db"
    assert sandbox.leftovers() == []
    assert not (sandbox.repo / "dist" / "index.tgz.tmp").exists()


def test_fetch_keeps_only_the_newest_backup(sandbox, asset):
    for name in ("index.bak.1000000000", "index.bak.1000000001"):
        (sandbox.repo / name).mkdir()
        (sandbox.repo / name / "manifest.json").write_text("{}")
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset)
    assert r.returncode == 0, r.stdout + r.stderr
    (backup,) = sandbox.leftovers()
    assert backup not in ("index.bak.1000000000", "index.bak.1000000001")
    assert json.loads((sandbox.repo / backup / "manifest.json").read_text()) == OLD
    assert "removed older backup index.bak.1000000000/" in r.stdout
    assert "removed older backup index.bak.1000000001/" in r.stdout


@gnu_tar
def test_fetch_skips_macos_metadata_and_runs_to_the_end(sandbox):
    # A laptop-built asset packed without COPYFILE_DISABLE. Linux tar unpacked
    # its ._index beside index/, the post-swap rmdir of the unpack dir failed,
    # and deploy.sh died before its restart with the new index already live.
    asset = tarball(sandbox.root / "mac.tgz", appledouble=True)
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert sorted(p.name for p in (sandbox.repo / "index").iterdir()) == [
        "fts.sqlite", "manifest.json"]                          # no ._ files
    (backup,) = sandbox.leftovers()                              # no index.fetch.* left
    assert f"kept one backup: {backup}/" in r.stdout             # it ran to the end


@gnu_tar
def test_deploy_restarts_after_fetching_a_macos_built_tarball(sandbox):
    sandbox.use_real_flock()
    asset = tarball(sandbox.root / "mac.tgz", appledouble=True)
    r = sandbox.run("deploy.sh", FAKE_TARBALL=asset, REFRESH_INDEX=1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sudo systemctl restart sandbox-svc" in sandbox.calls()
    assert r.stdout.endswith("deployed abc1234\n")


def test_fetch_onto_a_box_with_no_index_yet(sandbox, asset):
    shutil.rmtree(sandbox.repo / "index")                       # a fresh server
    r = sandbox.run("fetch_index.sh", FAKE_TARBALL=asset)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert sandbox.leftovers() == [] and "kept one backup" not in r.stdout


def test_deploy_refresh_index_fetches_under_the_lock_it_already_holds(sandbox, asset):
    # With flock(1) present (Linux) deploy.sh holds the lock while it runs
    # fetch_index.sh; a child that tried to take it again would exit 75.
    sandbox.use_real_flock()
    r = sandbox.run("deploy.sh", FAKE_TARBALL=asset, REFRESH_INDEX=1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(sandbox.manifest.read_text()) == NEW
    assert [c.split()[0] for c in sandbox.calls()] == ["git", "python", "gh", "sudo", "sudo", "git"]
    assert r.stdout.endswith("deployed abc1234\n")


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


def test_unit_publish_failure_pings_exit_3(sandbox):
    sandbox.dotenv(f"HEALTHCHECK_URL={URL}\n")
    r = sandbox.run_unit(FAKE_PUBLISH_RC=1)
    assert r.returncode == 3
    assert sandbox.pings() == [f"{URL}/start", f"{URL}/3"]      # an alert, not a success
    assert "FAILED after the swap: publish." in sandbox.body()  # the e-mail says which step
    assert json.loads(sandbox.manifest.read_text()) == NEW      # the refresh itself landed


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
