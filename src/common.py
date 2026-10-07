"""Shared plumbing: TLS fix, config + secrets loading, embeddings, chunking.

Import this module before any network library is used (ingest.py, ask.py and
the probes import it first) so the truststore injection below runs early.

Ported from linc-llm; the chunker is unchanged (exact 1-indexed line ranges
per chunk are what make tag-pinned #L.. permalinks possible).
"""
import os
import re
import functools
import hashlib
from pathlib import Path

# The lab VPN uses a TLS-inspecting proxy; trust the OS cert store instead of
# certifi's bundle. Off-VPN this is a harmless no-op. Never use verify=False.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import yaml

ROOT = Path(__file__).resolve().parent.parent

# bge-*-en-v1.5 models retrieve better when the *query* (not the documents) is
# prefixed with this instruction. Use it at query time; never at ingest time.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Known gap: this also matches comment lines inside fenced code blocks, which
# splits pasted scripts into tiny chunks (ROADMAP.md §4, "Chunker: comment
# lines inside code blocks count as headings").
HEADING_RE = re.compile(r"^#{1,6}\s")

# Bump when chunk_text cuts the same text differently. The ingest manifest
# records it, and an index built by an older chunker gets a full rebuild:
# incremental syncs only re-chunk what changed, so most threads would keep
# their old chunks forever.
#   2: no more shrinking copies of a window's tail before a long line
CHUNKER_VERSION = 2


def doc_key(rec: dict) -> str:
    """Stable per-document key, namespaced by app + source (multi-app safe).
    Reads the same fields from a harvested Record as from its chunks' metadata."""
    app, source = rec["app"], rec["source"]
    if source == "docs":
        tail = rec["gh_path"]           # app is one repo; path is unique within it
    elif source == "issues":
        tail = f'#{rec["gh_issue"]}'    # issue numbers unique within the app's repo
    else:  # neurostars
        tail = f'ns:{rec["ns_topic_id"]}'
    return f"{app}:{source}:{tail}"


def chunk_id(key: str, i: int) -> str:
    """Id of the i-th chunk of the document `key`: the same on every rebuild,
    and a document's chunks can be put back in order from their ids alone."""
    return hashlib.sha1(f"{key}:{i}".encode()).hexdigest()


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ (existing vars win)."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@functools.lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    """Load config.yaml (falling back to config.example.yaml) and .env.

    `BIDS_INDEX_PATH` in the environment overrides index.path — used by the
    refresh job to build the index in a staging dir without touching the live
    one that the running app has open.
    """
    load_dotenv()
    candidates = [Path(path)] if path else [ROOT / "config.yaml", ROOT / "config.example.yaml"]
    for candidate in candidates:
        if candidate.exists():
            with open(candidate) as f:
                config = yaml.safe_load(f)
            override = os.environ.get("BIDS_INDEX_PATH")
            if override:
                config.setdefault("index", {})["path"] = override
            return config
    raise FileNotFoundError(
        f"No config found (looked for {', '.join(str(c) for c in candidates)}). "
        "Copy config.example.yaml to config.yaml."
    )


def user_agent(config: dict | None = None) -> str:
    """Polite crawler User-Agent with a contact address (guardrail: we are
    members of these communities). Used by probes and harvesters alike."""
    config = config or load_config()
    contact = config.get("contact_email") or "no-contact-configured"
    return f"bids-assistant/0.1 (lab support-bot harvester; contact: {contact})"


def scope(config: dict, app: str) -> list[str]:
    """Apps whose content is in retrieval scope for a question about `app`:
    the app itself plus its configured pipeline neighbors (e.g. qsiprep pulls
    qsirecon for boundary questions). Used as a `where={"app": [...]}` filter."""
    neighbors = ((config.get("apps") or {}).get(app) or {}).get("neighbors", []) or []
    return [app, *neighbors]


@functools.lru_cache(maxsize=1)
def get_embedding_model():
    """Cached sentence-transformers model, per config. Slow on first call."""
    from sentence_transformers import SentenceTransformer  # heavy; import lazily

    return SentenceTransformer(load_config()["retrieval"]["embed_model"])


@functools.lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


def chunk_text(text: str, size_tokens: int, overlap_tokens: int) -> list[tuple[str, int, int]]:
    """Split text into chunks of ~size_tokens with overlap.

    Splits at markdown headings first, then packs lines into token windows,
    so line ranges are exact per chunk (1-indexed, inclusive). A single line
    longer than size_tokens becomes its own oversized chunk. Each chunk starts
    and ends past the one before it, so none repeats lines that are all in its
    neighbor.
    Returns [(chunk, line_start, line_end), ...].
    """
    lines = text.splitlines()

    # Section boundaries at headings: list of (start_line_index, section_lines).
    # A heading starts a new section unless the current one is still all blank.
    sections: list[tuple[int, list[str]]] = []
    for i, line in enumerate(lines):
        current_has_text = bool(sections) and any(l.strip() for l in sections[-1][1])
        if not sections or (HEADING_RE.match(line) and current_has_text):
            sections.append((i, [line]))
        else:
            sections[-1][1].append(line)

    chunks: list[tuple[str, int, int]] = []
    for start, sec_lines in sections:
        line_tokens = [count_tokens(l + "\n") for l in sec_lines]
        i = 0
        while i < len(sec_lines):
            j, total = i, 0
            while j < len(sec_lines) and (total + line_tokens[j] <= size_tokens or j == i):
                total += line_tokens[j]
                j += 1
            # Trim blank boundary lines so line_start/line_end anchor on content.
            k0, k1 = i, j
            while k0 < k1 and not sec_lines[k0].strip():
                k0 += 1
            while k1 > k0 and not sec_lines[k1 - 1].strip():
                k1 -= 1
            chunk = "\n".join(sec_lines[k0:k1])
            first, last = start + k0 + 1, start + k1
            # Skip a window whose lines all sit inside the previous chunk (the
            # trimmed tail of a window that only gained blank lines).
            if chunk and not (chunks and chunks[-1][1] <= first
                              and last <= chunks[-1][2]):
                chunks.append((chunk, first, last))
            if j >= len(sec_lines):
                break
            # Step back over ~overlap_tokens worth of lines for the next window,
            # but only as far as still leaves room for line j: the next window
            # has to get past it. Stepping back further, when line j was too
            # long to share a window with the overlap, re-emitted ever-shorter
            # copies of this window's tail before line j got a chunk of its own.
            # Never step back to this window's first line of text either, or
            # the next chunk would start on the same line as this one.
            back, overlap = j, 0
            while (back > k0 + 1
                   and overlap + line_tokens[back - 1] <= overlap_tokens
                   and overlap + line_tokens[back - 1] + line_tokens[j] <= size_tokens):
                back -= 1
                overlap += line_tokens[back]
            i = back
    return chunks


def stitch_chunks(chunks: list[str], overlap_tokens: int) -> str:
    """Put a document back together from its chunk_text chunks, in order.

    Each chunk opens on the lines that end the one before it (the overlap),
    so each contributes only what follows them. Exact except where two chunks
    meet without overlapping (a heading starts a new section, or a long line
    left no room): the blank lines trimmed from that boundary come back as one.

    The overlap is the longest run that matches, among those that fit in
    `overlap_tokens` the way chunk_text counts them; without that cap a log
    that repeats one line would read as one long overlap and lose copies.
    """
    out: list[str] = []
    prev: list[str] = []
    for chunk in chunks:
        lines = chunk.split("\n")
        # A proper suffix of the previous chunk and a proper prefix of this one:
        # each chunk starts and ends past the one before it.
        k, used = 0, 0
        while k < min(len(prev), len(lines)) - 1:
            used += count_tokens(lines[k] + "\n")
            if used > overlap_tokens:
                break
            k += 1
        while k and prev[-k:] != lines[:k]:
            k -= 1
        if out and not k:
            out.append("")
        out.extend(lines[k:])
        prev = lines
    return "\n".join(out)


if __name__ == "__main__":
    import json

    print(json.dumps(load_config(), indent=2))
