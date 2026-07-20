"""Docs source: an app's repo docs at its *latest release tag* (not main —
answers should match what users ran, and the tag is the pin in permalinks).

Each Record carries `gh_repo`, `gh_path`, `gh_tag`, `gh_sha` and a tag-pinned
blob URL; ingest chunks the text and appends the `#L{start}-L{end}` anchor.

Dry run (no writes; lists files and prints a sample line-anchored permalink):
    python -m src.sources.docs_source
"""
import base64
import sys
from urllib.parse import quote

from .. import common
from . import github_api

DOC_EXTENSIONS = {".md", ".rst", ".txt"}
MAX_FILE_BYTES = 1_000_000  # skip anything bigger; real docs never are


def latest_release_tag(repo: str) -> str:
    """Tag name of the latest published (non-draft, non-prerelease) release."""
    return github_api.get(f"/repos/{repo}/releases/latest")["tag_name"]


def tag_sha(repo: str, tag: str) -> str:
    """Commit sha a tag points at (works through annotated tags)."""
    return github_api.get(f"/repos/{repo}/commits/{quote(tag)}")["sha"]


def _wanted(path: str, docs_paths: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if name.startswith("readme") and "/" not in path:
        return True  # top-level README rides along with the docs
    return (any(path.startswith(p) for p in docs_paths)
            and ext in DOC_EXTENSIONS)


def list_files(repo: str, sha: str, docs_paths: list[str]) -> list[dict]:
    """Tree entries ({path, sha, size}) under docs_paths at the pinned commit."""
    tree = github_api.get(f"/repos/{repo}/git/trees/{sha}", recursive=1)
    if tree.get("truncated"):
        print(f"warning: {repo} tree truncated; some files missed", file=sys.stderr)
    return [e for e in tree["tree"]
            if e["type"] == "blob"
            and e.get("size", 0) <= MAX_FILE_BYTES
            and _wanted(e["path"], docs_paths)]


def file_text(repo: str, blob_sha: str) -> str | None:
    """Decoded file content, or None if it looks binary."""
    raw = base64.b64decode(github_api.get(f"/repos/{repo}/git/blobs/{blob_sha}")["content"])
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def blob_url(repo: str, ref: str, path: str) -> str:
    # ?plain=1 forces the code view: GitHub renders .md/.rst as "Preview" by
    # default, and Preview ignores #L line anchors. No-op for other files.
    return f"https://github.com/{repo}/blob/{quote(ref)}/{quote(path)}?plain=1"


def fetch_app(app: str, app_cfg: dict, tag: str | None = None) -> list[dict]:
    """Document-level Records for one app's docs at a release tag."""
    repo = app_cfg["github_repo"]
    tag = tag or latest_release_tag(repo)
    sha = tag_sha(repo, tag)
    records = []
    for entry in list_files(repo, sha, app_cfg["docs_paths"]):
        text = file_text(repo, entry["sha"])
        if not text or not text.strip():
            continue
        records.append({
            "text": text,
            "app": app,
            "source": "docs",
            "title": f"{entry['path']} ({app} {tag})",
            "url": blob_url(repo, tag, entry["path"]),
            "gh_repo": repo,
            "gh_path": entry["path"],
            "gh_tag": tag,
            "gh_sha": sha,
        })
    print(f"{app} docs @ {tag} ({sha[:10]}): {len(records)} file(s)", file=sys.stderr)
    return records


def fetch(config: dict | None = None) -> list[dict]:
    config = config or common.load_config()
    return [r for app, app_cfg in config["apps"].items()
            for r in fetch_app(app, app_cfg)]


def main():
    config = common.load_config()
    chunk_cfg = config["chunk"]
    for app, app_cfg in config["apps"].items():
        records = fetch_app(app, app_cfg)
        for r in records:
            print(f"  {r['gh_path']}  ({common.count_tokens(r['text'])} tokens)")
        if records:
            sample = records[0]
            chunks = common.chunk_text(sample["text"], chunk_cfg["size_tokens"],
                                       chunk_cfg["overlap_tokens"])
            _, start, end = chunks[0]
            print(f"\n  sample permalink ({sample['gh_path']}, chunk 1 -> lines {start}-{end}):")
            print(f"    {sample['url']}#L{start}-L{end}")


if __name__ == "__main__":
    main()
