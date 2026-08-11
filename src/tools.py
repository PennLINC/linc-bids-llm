"""Agent tools exposed to the model via OpenAI tool calling.

Three read-only moves, the way a maintainer works a bug:
  search_kb  — "did someone already hit this?" (hybrid index of issues/threads/docs)
  grep_code  — ripgrep the source at the version the user ran
  read_file  — read the raising lines + a tag-pinned GitHub permalink

Guardrails: no shell, no network beyond these declared calls, output bounded,
and read_file cannot escape the checkout directory.
"""
import subprocess
from pathlib import Path

from . import checkouts, common

# Output bounds — a tool result must stay a snippet, not a file dump.
GREP_MAX_LINES = 80
GREP_PER_FILE = 15
READ_MAX_LINES = 400
SNIPPET_CHARS = 240


class Toolbox:
    def __init__(self, config: dict, store, app: str):
        self.config = config
        self.store = store
        self.app = app
        self.repo = config["apps"][app]["github_repo"]

    # --- search_kb ---------------------------------------------------------

    def search_kb(self, query: str, source_filter: str | None = None) -> str:
        from .router import scope
        where = {"app": scope(self.config, self.app)}   # app + pipeline neighbors
        if source_filter:
            where["source"] = source_filter
        results = self.store.hybrid_query(
            query, k=self.config["retrieval"]["top_k"], where=where)
        if not results:
            return "No matches in the knowledge base."
        lines = []
        for i, r in enumerate(results, 1):
            tags = []
            if r.get("gh_solved") or r.get("ns_solved"):
                tags.append("solved")
            tag_str = f" ({', '.join(tags)})" if tags else ""
            snippet = " ".join(r["text"].split())[:SNIPPET_CHARS]
            lines.append(f"[{i}] {r.get('title', '?')} — {r['source']}{tag_str}\n"
                         f"    {r.get('url', '')}\n    {snippet}")
        return "\n".join(lines)

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
            if name == "grep_code":
                return self.grep_code(args["pattern"], args.get("version"),
                                      bool(args.get("regex", False)))
            if name == "read_file":
                return self.read_file(args["path"], args.get("version"),
                                      args.get("start"), args.get("end"))
        except Exception as e:
            return f"{name} failed: {type(e).__name__}: {e}"
        return f"unknown tool: {name}"


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_kb",
        "description": (
            "Search the knowledge base of past GitHub issues, solved NeuroStars "
            "threads, and docs for this app. Try this FIRST for any error or "
            "question — like a maintainer asking 'did someone already hit this?'. "
            "Returns titles, URLs, and snippets."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "Error string, symptom, or question."},
            "source_filter": {"type": "string", "enum": ["issues", "neurostars", "docs"],
                              "description": "Optional: restrict to one source."},
        }, "required": ["query"]}}},
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
