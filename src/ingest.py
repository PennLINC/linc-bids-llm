"""Build or refresh the hybrid index: harvest -> chunk -> embed + FTS -> manifest.

    python -m src.ingest            # incremental: skip unchanged docs/threads
    python -m src.ingest --full     # from-scratch rebuild

Incremental sync per source:
  docs        — compare the app's latest-release-tag commit sha
  issues      — GitHub `since=` on updated_at; changed threads replaced
  neurostars  — per-topic bumped_at; changed topics replaced, gone ones pruned

A full rebuild is forced when the manifest is missing, predates the current
schema, or the embedding model / chunking config changed (mismatched
embeddings silently wreck retrieval). Run --full occasionally regardless:
Chroma's sqlite accumulates slack across many increments.
"""
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import common  # first import: runs the truststore inject
from .sources import docs_source, issues_source, neurostars_source
from .store import Store


def _chunk_key(rec: dict) -> str:
    """Stable per-document key, namespaced by app + source (multi-app safe)."""
    app, source = rec["app"], rec["source"]
    if source == "docs":
        tail = rec["gh_path"]           # app is one repo; path is unique within it
    elif source == "issues":
        tail = f'#{rec["gh_issue"]}'    # issue numbers unique within the app's repo
    else:  # neurostars
        tail = f'ns:{rec["ns_topic_id"]}'
    return f"{app}:{source}:{tail}"


def chunk_record(rec: dict, size: int, overlap: int) -> list[dict]:
    """Split one document/thread Record into chunk records with stable ids.

    Docs get line-anchored permalinks + gh_line_* metadata. Thread chunks keep
    the thread URL and carry the title on every chunk (chunk 0 already opens
    with it; later chunks get it prepended so each is self-describing).
    """
    key = _chunk_key(rec)
    meta = {k: v for k, v in rec.items() if k != "text"}
    is_docs = rec["source"] == "docs"
    chunks = []
    for i, (text, line_start, line_end) in enumerate(
            common.chunk_text(rec["text"], size, overlap)):
        chunk = dict(meta)
        chunk["id"] = hashlib.sha1(f"{key}:{i}".encode()).hexdigest()
        if is_docs:
            chunk["text"] = text
            chunk["gh_line_start"] = line_start
            chunk["gh_line_end"] = line_end
            chunk["url"] = f'{rec["url"]}#L{line_start}-L{line_end}'
        else:
            title = rec.get("title", "")
            chunk["text"] = text if i == 0 else f"# {title}\n\n{text}"
        chunks.append(chunk)
    return chunks


def fresh_index_dir(path: Path) -> None:
    """Wipe the index directory for a clean rebuild, guarding against a
    misconfigured index.path pointed at something precious."""
    if path.exists():
        looks_like_index = ((path / "chroma.sqlite3").exists()
                            or (path / "fts.sqlite").exists()
                            or not any(path.iterdir()))
        if not looks_like_index:
            sys.exit(f"{path} exists but doesn't look like a bids-assistant "
                     "index; refusing to delete it. Check index.path in config.")
        shutil.rmtree(path)
    path.mkdir(parents=True)


def read_manifest(config: dict) -> dict | None:
    path = Path(config["index"]["path"]) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _chunk_config(config: dict) -> dict:
    return {k: config["chunk"][k] for k in ("size_tokens", "overlap_tokens")}


def needs_full(config: dict, manifest: dict | None) -> str | None:
    """The reason a full rebuild is required, or None if syncing is safe."""
    if manifest is None:
        return "no previous manifest"
    if manifest.get("embedding_model") != config["retrieval"]["embed_model"]:
        return "embedding model changed"
    if manifest.get("chunk_config") != _chunk_config(config):
        return "chunking config changed"
    for key in ("docs_shas", "issues_since", "ns_bumped"):
        if key not in manifest:
            return "previous build predates the current sync schema"
    return None


def _add_chunks(store, records, size, overlap) -> int:
    chunks = [c for r in records for c in chunk_record(r, size, overlap)]
    store.add(chunks)
    return len(chunks)


def sync_docs(config, store, old_shas: dict, full: bool) -> tuple[dict, dict]:
    """Docs are replaced whole-app whenever the release-tag sha moves."""
    size, overlap = config["chunk"]["size_tokens"], config["chunk"]["overlap_tokens"]
    new_shas, new_tags = {}, {}
    for app, app_cfg in config["apps"].items():
        repo = app_cfg["github_repo"]
        tag = docs_source.latest_release_tag(repo)
        sha = docs_source.tag_sha(repo, tag)
        new_shas[app], new_tags[app] = sha, tag
        if not full and old_shas.get(app) == sha:
            print(f"{app} docs @ {tag}: unchanged")
            continue
        records = docs_source.fetch_app(app, app_cfg, tag=tag)
        if not full:
            store.delete({"app": app, "source": "docs"})
        n = _add_chunks(store, records, size, overlap)
        print(f"{app} docs @ {tag}: {len(records)} file(s) -> {n} chunk(s)")
    return new_shas, new_tags


def sync_issues(config, store, old_since: dict, full: bool) -> dict:
    """Fetch threads updated since last harvest; replace each changed thread."""
    size, overlap = config["chunk"]["size_tokens"], config["chunk"]["overlap_tokens"]
    new_since = {}
    for app, app_cfg in config["apps"].items():
        since = None if full else old_since.get(app)
        records = issues_source.fetch_app(app, app_cfg, since=since)
        n = 0
        for rec in records:
            if not full:
                store.delete({"app": app, "source": "issues",
                              "gh_issue": rec["gh_issue"]})
            n += _add_chunks(store, [rec], size, overlap)
        seen = [r["gh_updated"] for r in records] + \
               ([old_since[app]] if app in old_since else [])
        new_since[app] = max(seen) if seen else \
            datetime.now(timezone.utc).isoformat()
        print(f"{app} issues: {len(records)} changed thread(s) -> {n} chunk(s)")
    return new_since


def sync_neurostars(config, store, old_bumped: dict, full: bool) -> dict:
    """Per-topic bumped_at sync: replace changed topics, prune removed ones."""
    size, overlap = config["chunk"]["size_tokens"], config["chunk"]["overlap_tokens"]
    new_bumped = {}
    for app, app_cfg in config["apps"].items():
        prev = {} if full else old_bumped.get(app, {})
        current: dict = {}
        changed = 0
        seen_ids = set()
        for tag in app_cfg.get("neurostars_tags", []):
            for topic in neurostars_source.list_topics(tag):
                tid = topic["id"]
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                bumped = topic.get("bumped_at", "")
                current[str(tid)] = bumped
                if not full and prev.get(str(tid)) == bumped:
                    continue  # unchanged
                try:
                    posts = neurostars_source.fetch_posts(tid)
                except Exception as e:
                    print(f"  [skip] topic {tid}: {e}")
                    continue
                if not posts:
                    continue
                rec = neurostars_source.topic_record(app, topic, posts)
                if not full:
                    store.delete({"app": app, "source": "neurostars",
                                  "ns_topic_id": tid})
                _add_chunks(store, [rec], size, overlap)
                changed += 1
        if not full:
            for gone in set(prev) - set(current):
                store.delete({"app": app, "source": "neurostars",
                              "ns_topic_id": int(gone)})
                print(f"  [gone] neurostars topic {gone}: chunks removed")
        new_bumped[app] = current
        print(f"{app} neurostars: {changed} changed of {len(current)} topic(s)")
    return new_bumped


def source_counts(store) -> dict:
    metas = store.collection.get(include=["metadatas"])["metadatas"] or []
    counts: dict = {}
    for m in metas:
        s = m.get("source", "?")
        counts[s] = counts.get(s, 0) + 1
    return counts


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    full_flag = "--full" in argv
    config = common.load_config()

    manifest_old = read_manifest(config)
    reason = "--full" if full_flag else needs_full(config, manifest_old)
    full = reason is not None
    if full:
        print(f"== full rebuild ({reason}) ==")
        fresh_index_dir(Path(config["index"]["path"]))
        manifest_old = {}
    else:
        print("== incremental sync ==")

    store = Store(config)
    print("== docs ==")
    docs_shas, docs_tags = sync_docs(config, store,
                                     (manifest_old or {}).get("docs_shas", {}), full)
    print("== issues ==")
    issues_since = sync_issues(config, store,
                               (manifest_old or {}).get("issues_since", {}), full)
    print("== neurostars ==")
    ns_bumped = sync_neurostars(config, store,
                                (manifest_old or {}).get("ns_bumped", {}), full)

    counts = source_counts(store)
    dim = (manifest_old or {}).get("embedding_dim")
    if not dim:
        model = common.get_embedding_model()
        dim = (model.get_sentence_embedding_dimension()
               if hasattr(model, "get_sentence_embedding_dimension")
               else len(model.encode(["x"])[0]))
    manifest = {
        "embedding_model": config["retrieval"]["embed_model"],
        "embedding_dim": dim,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunk_config": _chunk_config(config),
        "apps": list(config["apps"].keys()),
        "docs_shas": docs_shas,
        "docs_tags": docs_tags,
        "issues_since": issues_since,
        "ns_bumped": ns_bumped,
        "chunks": counts,
    }
    manifest_path = Path(config["index"]["path"]) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    store.close()
    print(f"total chunks: {counts} (sum {sum(counts.values())})")
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
