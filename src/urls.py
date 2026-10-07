"""Canonical keys for source URLs, so a URL a tester pastes (or the agent
passes to read_thread) can be matched to the one the index stores.

The index holds exactly one form per source: GitHub's `html_url`, a NeuroStars
`/t/<slug>/<id>`, a tag-pinned `blob/<tag>/<path>?plain=1#L..` docs permalink.
A browser address bar rarely hands a tester that form — Discourse appends the
post number as you scroll, GitHub adds `#issuecomment-…`, a PR opens under
`/issues/N` and `/pull/N` alike. Comparing canonical keys instead of raw
strings keeps those from scoring as retrieval misses.
"""
import re
from urllib.parse import unquote, urlsplit

_GH_THREAD = re.compile(r"^/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)")
_GH_BLOB = re.compile(r"^/([^/]+)/([^/]+)/blob/[^/]+/(.+)$")


def canon_url(url: str) -> str:
    """Stable key for a source URL; unrecognized input passes through tidied.

    github issue / PR  -> gh:<owner>/<repo>#<n>      (they share one numbering)
    github blob (docs) -> ghfile:<owner>/<repo>:<path>   (ref dropped: the index
                          pins one tag per app, a tester may link another)
    neurostars topic   -> ns:<topic id>              (slug + post number dropped)
    """
    url = (url or "").strip()
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")

    if host == "github.com":
        if m := _GH_THREAD.match(path):
            return f"gh:{m[1].lower()}/{m[2].lower()}#{m[3]}"
        if m := _GH_BLOB.match(path):
            return f"ghfile:{m[1].lower()}/{m[2].lower()}:{unquote(m[3])}"
    if host == "neurostars.org" and path.startswith("/t/"):
        # /t/<slug>/<id>[/<post>] or /t/<id>[/<post>]; a slug is never all digits
        segs = path.split("/")[2:]
        topic = segs[0] if segs[0].isdigit() else (segs[1] if len(segs) > 1 else "")
        if topic.isdigit():
            return f"ns:{topic}"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")
