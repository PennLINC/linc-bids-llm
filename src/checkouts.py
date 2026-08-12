"""Version-pinned shallow clones: the code half of retrieval.

Code questions are answered by grepping real source at the tag the user ran,
not from embeddings. This module clones each app at its latest N release tags
(+ main) under checkouts/<app>/<tag>/, and maps a user-reported version to the
nearest tag we actually have.

    python -m src.checkouts            # clone/update every app's tags
    python -m src.checkouts --list     # show what's cloned, no network
"""
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import common
from .sources import github_api

MAIN = "main"


def _norm(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") and tag[1:2].isdigit() else tag


def parse_version(tag: str) -> Version | None:
    try:
        return Version(_norm(tag))
    except InvalidVersion:
        return None


def release_tags(repo: str, n: int) -> list[str]:
    """Latest N stable release tags (drafts + prereleases excluded), newest
    first by version order (not GitHub's created-at order)."""
    rels = github_api.get(f"/repos/{repo}/releases", per_page=100)
    stable = []
    for r in rels:
        if r.get("draft") or r.get("prerelease"):
            continue
        v = parse_version(r["tag_name"])
        if v is not None and not v.is_prerelease:
            stable.append((v, r["tag_name"]))
    stable.sort(key=lambda t: t[0], reverse=True)
    return [tag for _, tag in stable[:n]]


def app_dir(config: dict, app: str) -> Path:
    return Path(config["checkouts"]["path"]) / app


def checkout_path(config: dict, app: str, tag: str) -> Path:
    return app_dir(config, app) / tag


def cloned_tags(config: dict, app: str) -> list[str]:
    """Tags that already have a checkout on disk (a .git dir marks a good one)."""
    base = app_dir(config, app)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir()
                  if (p / ".git").exists())


def ensure_checkout(config: dict, app: str, repo: str, tag: str) -> Path:
    """Shallow-clone repo@tag if not already present; return its path.

    Idempotent: an existing good checkout is left untouched. A partial/broken
    directory (no .git) is wiped and recloned.
    """
    path = checkout_path(config, app, tag)
    if (path / ".git").exists():
        return path
    if path.exists():
        shutil.rmtree(path)  # partial clone from an interrupted run
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    print(f"cloning {repo}@{tag} -> {path}", file=sys.stderr)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", tag,
         "--single-branch", url, str(path)],
        check=True, capture_output=True, text=True)
    return path


def update_main(config: dict, app: str, repo: str) -> Path:
    """Clone main, or refresh it if already present (tags never move; main does,
    so it's the one checkout worth refreshing).

    Uses fetch + hard reset, not `pull --ff-only`: a shallow --depth 1 clone
    can't fast-forward once upstream main advances past its single commit
    (git aborts with 'Not possible to fast-forward', exit 128). A broken/
    unfetchable checkout is wiped and re-cloned."""
    path = checkout_path(config, app, MAIN)
    if (path / ".git").exists():
        fetched = subprocess.run(
            ["git", "-C", str(path), "fetch", "--depth", "1", "origin", MAIN],
            capture_output=True, text=True)
        if fetched.returncode == 0:
            subprocess.run(["git", "-C", str(path), "reset", "--hard", "FETCH_HEAD"],
                           check=True, capture_output=True, text=True)
            return path
        shutil.rmtree(path, ignore_errors=True)  # can't refresh -> reclone fresh
    return ensure_checkout(config, app, repo, MAIN)


def resolve_version(config: dict, app: str, version: str | None) -> tuple[str, str]:
    """Map a user-reported version to the nearest cloned tag.

    Returns (tag, note) where note explains any mismatch for the answer layer:
      - exact match            -> ("26.0.0", "exact")
      - between/older/newer     -> nearest tag + a human note
      - unknown/None            -> newest tag we have
    Only tags with a checkout on disk are candidates (that's what we can grep).
    """
    tags = cloned_tags(config, app)
    if not tags:
        raise FileNotFoundError(
            f"no checkouts for {app}; run `python -m src.checkouts` first")
    versioned = sorted(
        ((parse_version(t), t) for t in tags if parse_version(t)),
        key=lambda x: x[0])
    newest = versioned[-1][1] if versioned else (MAIN if MAIN in tags else tags[-1])

    if not version:
        return newest, "no version given; using newest checkout"
    if version in tags:
        return version, "exact"
    want = parse_version(version)
    if want is None or not versioned:
        return newest, f"could not parse version {version!r}; using {newest}"

    exact = [t for v, t in versioned if v == want]
    if exact:
        return exact[0], "exact"
    older = [t for v, t in versioned if v <= want]
    if older:
        tag = older[-1]  # highest tag at or below the requested version
        return tag, f"no checkout for {version}; using nearest older tag {tag}"
    tag = versioned[0][1]  # requested is older than everything we have
    return tag, (f"{version} predates our checkouts; using oldest available "
                 f"tag {tag} (code may differ from what you ran)")


def setup(config: dict | None = None) -> None:
    config = config or common.load_config()
    for app, app_cfg in config["apps"].items():
        repo = app_cfg["github_repo"]
        n = app_cfg.get("checkout_tags", 3)
        tags = release_tags(repo, n)
        print(f"{app}: {n} release tag(s) {tags} + {MAIN}")
        for tag in tags:
            ensure_checkout(config, app, repo, tag)
        update_main(config, app, repo)


def main():
    config = common.load_config()
    if "--list" in sys.argv[1:]:
        for app in config["apps"]:
            print(f"{app}: {cloned_tags(config, app)}")
        return
    setup(config)
    print("\ncloned:")
    for app in config["apps"]:
        print(f"  {app}: {cloned_tags(config, app)}")


if __name__ == "__main__":
    main()
