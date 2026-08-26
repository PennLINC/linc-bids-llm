"""Shared plumbing: TLS fix, config + secrets loading, embeddings, chunking.

Import this module before any network library is used (ingest.py, ask.py and
the probes import it first) so the truststore injection below runs early.

Ported from linc-llm; the chunker is unchanged (exact 1-indexed line ranges
per chunk are what make tag-pinned #L.. permalinks possible).
"""
import os
import re
import functools
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

HEADING_RE = re.compile(r"^#{1,6}\s")


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
    longer than size_tokens becomes its own oversized chunk.
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
            if chunk:
                chunks.append((chunk, start + k0 + 1, start + k1))
            if j >= len(sec_lines):
                break
            # Step back over ~overlap_tokens worth of lines for the next window.
            back, overlap = j, 0
            while back > i + 1 and overlap + line_tokens[back - 1] <= overlap_tokens:
                back -= 1
                overlap += line_tokens[back]
            i = back
    return chunks


if __name__ == "__main__":
    import json

    print(json.dumps(load_config(), indent=2))
