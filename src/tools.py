"""Agent tools exposed to the model via OpenAI tool calling.

Four read-only moves, the way a maintainer works a bug:
  search_kb    — "did someone already hit this?" (hybrid index of issues/threads/docs)
  read_thread  — read a thread search_kb found: the question and how it ended
  grep_code    — ripgrep the source at the version the user ran
  read_file    — read the raising lines (or a docs page) + a tag-pinned permalink

Guardrails: no shell, no network beyond these declared calls, output bounded,
and read_file cannot escape the checkout directory.
"""
import subprocess
from pathlib import Path

from . import checkouts, common
from .urls import canon_url

# Output bounds — a tool result must stay a snippet, not a file dump.
GREP_MAX_LINES = 80
GREP_PER_FILE = 15
READ_MAX_LINES = 400
SNIPPET_CHARS = 240
# read_thread: a thread longer than this shows its opening and its end, about
# 4K tokens in all. Most fit whole: issues average ~1,600 tokens.
THREAD_MAX_CHARS = 16000
THREAD_HEAD_CHARS = 9000     # the question, and NeuroStars' accepted answer after it


def _snippet(rec: dict) -> str:
    """A result's text, opening past the `# title` line its title already shows
    (chunk 0 of a thread opens with it, later chunks have it prepended)."""
    text = rec["text"].removeprefix(f"# {rec.get('title', '')}")
    return " ".join(text.split())[:SNIPPET_CHARS]


def _take(lines: list[str], budget: int) -> int:
    """How many of `lines` fit in `budget` characters (at least one)."""
    n, used = 0, 0
    for line in lines:
        used += len(line) + 1
        if n and used > budget:
            break
        n += 1
    return n


class Toolbox:
    def __init__(self, config: dict, store, app: str):
        self.config = config
        self.store = store
        self.app = app
        self.repo = config["apps"][app]["github_repo"]

    # --- search_kb ---------------------------------------------------------

    def search_kb(self, query: str, source_filter: str | None = None) -> str:
        where = {"app": common.scope(self.config, self.app)}   # app + pipeline neighbors
        if source_filter:
            where["source"] = source_filter
        # One result per thread or docs file: the best-matching part stands for
        # it, and read_thread / read_file open the rest.
        results = self.store.hybrid_query(
            query, k=self.config["retrieval"]["top_k"], where=where, per_doc=1)
        if not results:
            return "No matches in the knowledge base."
        lines = []
        for i, r in enumerate(results, 1):
            tags = []
            if r.get("gh_solved") or r.get("ns_solved"):
                tags.append("solved")
            if r.get("doc_hits", 1) > 1:
                tags.append(f"{r['doc_hits']} matching parts")
            tag_str = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"[{i}] {r.get('title', '?')} — {r['source']}{tag_str}\n"
                         f"    {r.get('url', '')}\n    {_snippet(r)}")
        return "\n".join(lines)

    # --- read_thread -------------------------------------------------------

    def _thread_chunks(self, url: str) -> list[dict] | str:
        """The indexed chunks of the issue or NeuroStars thread at `url`, or
        why there are none."""
        key = canon_url(url)
        apps = self.config["apps"]
        in_scope = common.scope(self.config, self.app)
        if key.startswith("gh:"):
            repo, number = key[3:].rsplit("#", 1)
            wheres = [{"app": a, "source": "issues", "gh_issue": int(number)}
                      for a, cfg in apps.items()
                      if (cfg or {}).get("github_repo", "").lower() == repo]
        elif key.startswith("ns:"):
            # a thread tagged for two apps is indexed under each; the copies match
            order = in_scope + [a for a in apps if a not in in_scope]
            wheres = [{"app": a, "source": "neurostars", "ns_topic_id": int(key[3:])}
                      for a in order]
        elif key.startswith("ghfile:"):
            return (f"{url} is a docs page: read it with read_file, passing the "
                    "path after the tag (e.g. docs/usage.rst) and the user's version.")
        else:
            return (f"read_thread reads GitHub issues and NeuroStars threads; "
                    f"{url!r} is neither.")
        for where in wheres:
            if chunks := self.store.doc_chunks(where):
                return chunks
        return f"{url} is not in the knowledge base."

    def read_thread(self, url: str, start: int | None = None) -> str:
        chunks = self._thread_chunks(url)
        if isinstance(chunks, str):
            return chunks
        first = chunks[0]
        title = first.get("title", "")
        # chunks after the first repeat the title on top (ingest.chunk_record)
        texts = [c["text"] if i == 0 else c["text"].removeprefix(f"# {title}\n\n")
                 for i, c in enumerate(chunks)]
        lines = common.stitch_chunks(
            texts, self.config["chunk"]["overlap_tokens"]).split("\n")
        n = len(lines)
        solved = " (solved)" if first.get("gh_solved") or first.get("ns_solved") else ""
        header = f"{title} — {first['source']}{solved}\n{first.get('url', url)}"

        def span(a: int, b: int) -> str:       # lines a..b, 1-indexed, inclusive
            return "\n".join(lines[a - 1:b])

        # Paging through the middle of a long thread. The model tends to pass
        # start=1 on a first read; that means the default view, not page 1.
        if start is not None and start > 1:
            a = min(start, n)
            b = a - 1 + _take(lines[a - 1:], THREAD_MAX_CHARS)
            rest = (f"\n\n[... lines {b + 1}-{n} follow: read_thread with "
                    f"start={b + 1} ...]") if b < n else ""
            return f"{header}\n(lines {a}-{b} of {n})\n\n{span(a, b)}{rest}"

        # Too long to show whole: the opening (the question, and on NeuroStars
        # the accepted answer, which comes second) and the end (where an issue
        # is usually resolved), with the line numbers to read the middle.
        a = _take(lines, THREAD_HEAD_CHARS)
        b = n + 1 - _take(lines[a:][::-1], THREAD_MAX_CHARS - THREAD_HEAD_CHARS)
        if len("\n".join(lines)) <= THREAD_MAX_CHARS or b == a + 1:
            return f"{header}\n(all {n} lines)\n\n{span(1, n)}"
        return (f"{header}\n(lines 1-{a} and {b}-{n} of {n})\n\n{span(1, a)}\n\n"
                f"[... lines {a + 1}-{b - 1} omitted: read_thread with "
                f"start={a + 1} reads on from there ...]\n\n{span(b, n)}")

    # --- grep_code ---------------------------------------------------------

    def grep_code(self, pattern: str, version: str | None = None,
                  regex: bool = False) -> str:
        tag, note = checkouts.resolve_version(self.config, self.app, version)
        path = checkouts.ensure_checkout(self.config, self.app, self.repo, tag)
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never",
               "--smart-case", "--max-columns", "300",
               "--max-count", str(GREP_PER_FILE)]
        if not regex:
            cmd.append("--fixed-strings")
        cmd += ["--", pattern, str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 1:
            return f"(searched {self.repo}@{tag}: {note})\nNo matches for {pattern!r}."
        if proc.returncode not in (0,):
            return f"grep error: {proc.stderr.strip()[:300]}"
        root = str(path) + "/"
        out = [ln.replace(root, "") for ln in proc.stdout.splitlines()]
        header = f"(matches in {self.repo}@{tag}: {note})"
        if len(out) > GREP_MAX_LINES:
            shown = out[:GREP_MAX_LINES]
            return (header + "\n" + "\n".join(shown)
                    + f"\n... {len(out) - GREP_MAX_LINES} more match(es) truncated; "
                    "narrow the pattern.")
        return header + "\n" + "\n".join(out)

    # --- read_file ---------------------------------------------------------

    def _permalink_ref(self, tag: str, checkout: Path) -> str:
        """Tag name for release checkouts (stable, readable); resolved HEAD sha
        for main (which moves)."""
        if tag != checkouts.MAIN:
            return tag
        proc = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        return proc.stdout.strip() or tag

    def read_file(self, path: str, version: str | None = None,
                  start: int | None = None, end: int | None = None) -> str:
        tag, note = checkouts.resolve_version(self.config, self.app, version)
        root = checkouts.ensure_checkout(self.config, self.app, self.repo, tag).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            return "refused: path escapes the checkout."
        if not target.is_file():
            return f"no such file in {self.repo}@{tag}: {path}"

        lines = target.read_text(errors="replace").splitlines()
        n = len(lines)
        s = max(start or 1, 1)
        e = min(end or n, n)
        if e - s + 1 > READ_MAX_LINES:
            e = s + READ_MAX_LINES - 1
        rel = str(target.relative_to(root))
        ref = self._permalink_ref(tag, root)
        url = (f"https://github.com/{self.repo}/blob/{ref}/{rel}"
               f"?plain=1#L{s}-L{e}")
        width = len(str(e))
        body = "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(s, e + 1))
        return f"{rel} @ {self.repo}@{tag} (lines {s}-{e}; {note})\n{url}\n\n{body}"

    # --- dispatch ----------------------------------------------------------

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "search_kb":
                return self.search_kb(args["query"], args.get("source_filter"))
            if name == "read_thread":
                return self.read_thread(args["url"], args.get("start"))
            if name == "grep_code":
                return self.grep_code(args["pattern"], args.get("version"),
                                      bool(args.get("regex", False)))
            if name == "read_file":
                return self.read_file(args["path"], args.get("version"),
                                      args.get("start"), args.get("end"))
        except Exception as e:
            return f"{name} failed: {type(e).__name__}: {e}"
        return f"unknown tool: {name}"


def describe_call(name: str, args: dict) -> str:
    """A tool call in a few words, for the UI's progress line."""
    def short(text, limit=60) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    at = f" at {args['version']}" if args.get("version") else ""
    if name == "search_kb":
        where = {"issues": "GitHub issues", "neurostars": "NeuroStars",
                 "docs": "the docs"}.get(args.get("source_filter"),
                                         "past issues, threads and docs")
        return f"Searching {where} for “{short(args.get('query'))}”"
    if name == "read_thread":
        return f"Reading {short(str(args.get('url', '')).split('://')[-1], 80)}"
    if name == "grep_code":
        return f"Searching the code{at} for “{short(args.get('pattern'))}”"
    if name == "read_file":
        lines = f", lines {args['start']}–{args.get('end') or 'end'}" if args.get("start") else ""
        return f"Reading {short(args.get('path'), 80)}{lines}{at}"
    return name


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_kb",
        "description": (
            "Search the knowledge base of past GitHub issues, solved NeuroStars "
            "threads, and docs for this app. Try this FIRST for any error or "
            "question — like a maintainer asking 'did someone already hit this?'. "
            "Returns one result per thread or docs page: title, URL, and a "
            "snippet of the part that matched best."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "Error string, symptom, or question."},
            "source_filter": {"type": "string", "enum": ["issues", "neurostars", "docs"],
                              "description": "Optional: restrict to one source."},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_thread",
        "description": (
            "Read a GitHub issue or NeuroStars thread from search_kb results: "
            "the question, the replies, and how it was resolved. A search "
            "snippet is only the start of one part, so read a thread before "
            "relying on it. Long threads show their opening and their end; "
            "pass start to read the lines in between. For docs pages use "
            "read_file."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string",
                    "description": "The thread's URL, as search_kb gave it."},
            "start": {"type": "integer",
                      "description": "Only to read the middle of a long thread: "
                      "the first line to show, from the line numbers it gave. "
                      "Leave it out otherwise."},
        }, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "grep_code",
        "description": (
            "Ripgrep the app's source code at the version the user ran. Use to "
            "find where an error string is raised or a function is defined. "
            "Output is bounded; narrow the pattern if truncated."),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string",
                        "description": "Literal text by default (e.g. an error "
                        "message). Set regex=true to use a regular expression."},
            "version": {"type": "string",
                        "description": "The app version the user ran (e.g. "
                        "'26.0.0'). Omit to search the newest checkout."},
            "regex": {"type": "boolean",
                      "description": "Treat pattern as a regex (default false = "
                      "literal fixed-string match)."},
        }, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": (
            "Read numbered source lines at the user's version and get a "
            "GitHub permalink that opens exactly those lines at that tag. Use "
            "after grep_code to inspect the code around a match."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Repo-relative path, e.g. 'qsiprep/cli/run.py'."},
            "version": {"type": "string",
                        "description": "The app version the user ran. Omit for newest."},
            "start": {"type": "integer", "description": "First line (1-indexed)."},
            "end": {"type": "integer", "description": "Last line (inclusive)."},
        }, "required": ["path"]}}},
]
