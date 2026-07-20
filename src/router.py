"""Triage a question to one of two answer paths.

  one-shot : FAQ-shaped — hybrid retrieval already surfaced a high-confidence
             near-duplicate (a solved thread or a docs section both retrieval
             halves agree on). Answer from it in one cheap call.
  agent    : novel traceback / code question — needs the tool loop (grep the
             version the user ran, read the raising code). This is the default
             for anything not clearly FAQ-shaped.

Heuristics are intentionally cheap and legible; Stage 6 eval calibrates them.
"""
import re
from dataclasses import dataclass

TRACEBACK_RE = re.compile(r'Traceback \(most recent call last\)')
PYFRAME_RE = re.compile(r'^\s*File ".*", line \d+', re.M)
ERROR_RE = re.compile(r'\b[A-Z]\w*(?:Error|Exception)\b')

LONG_PASTE_LINES = 12
LONG_PASTE_CHARS = 1200


def looks_like_traceback(text: str) -> bool:
    if TRACEBACK_RE.search(text) or PYFRAME_RE.search(text):
        return True
    # a named exception sitting in a multi-line paste reads as pasted output
    return bool(ERROR_RE.search(text) and "\n" in text)


def is_long_paste(text: str) -> bool:
    return text.count("\n") >= LONG_PASTE_LINES or len(text) >= LONG_PASTE_CHARS


@dataclass
class Decision:
    path: str            # "oneshot" | "agent"
    chunks: list         # retrieved chunks (reused by the oneshot path)
    reason: str


def route(question: str, store, config: dict, app: str) -> Decision:
    """Pick a path. Pasted tracebacks and long dumps always go agentic; short
    questions go one-shot only on a high-confidence FAQ match."""
    if looks_like_traceback(question) or is_long_paste(question):
        return Decision("agent", [], "contains a traceback / long paste")

    chunks = store.hybrid_query(
        question, k=config["retrieval"]["top_k"], where={"app": app})
    if chunks:
        top = chunks[0]
        both_agree = top.get("in_vector") and top.get("in_bm25")
        faq = both_agree and (top.get("gh_solved") or top.get("ns_solved")
                              or top.get("source") == "docs")
        if faq:
            return Decision("oneshot", chunks,
                            "high-confidence near-duplicate in the index")
    return Decision("agent", chunks, "no strong FAQ match; using tools")
