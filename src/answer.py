"""The answer path: the agent's prompt and its tool loop.

Unlike linc-llm's strict "answer only from context", this assistant is
diagnostic: it may reason beyond the retrieved text, but must label speculation,
cite what it used (URLs / permalinks), and ask for the version + full traceback
when the input is thin. When genuinely stuck it drafts a GitHub issue rather
than guessing.

Every question goes to the agent. Until October 2026 a router sent short
first questions with a confident-looking match to a one-shot path (one call
to a smaller model over the top 8 chunks, no tools); testers found its answers
unreliable, and the agent answered the same questions well on a follow-up.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import common
from .tools import TOOL_SCHEMAS, Toolbox

VERSION_HINT_RE = re.compile(r'\b\d+\.\d+(?:\.\d+)?(?:rc\d+)?\b')

CITE_RULES = (
    "Every claim must be traceable: cite the source you used by its URL or "
    "GitHub permalink inline. If you reason beyond the sources, label it "
    "explicitly (e.g. 'Likely, though not documented: ...'). If the version "
    "the user ran is unknown and it matters, ask for it."
)

SYSTEM_AGENT = (
    "You are a maintainer-style troubleshooting assistant for the {app} BIDS "
    "App, helping a member of the lab diagnose an error or answer a question.\n"
    "Work like a maintainer:\n"
    "1. FIRST call search_kb — someone may have already hit this (closed "
    "issues, solved NeuroStars threads).\n"
    "2. When a result looks like the same problem, read_thread it before you "
    "rely on it: a search snippet is only the start of one part, and the fix is "
    "usually in the replies (an accepted answer, a maintainer's last comments). "
    "Read docs pages with read_file.\n"
    "3. If it's a code question or the traceback points at source, grep_code "
    "the version the user ran to find the raising line, then read_file to read "
    "it and get a permalink.\n"
    "4. Version awareness is core: users run old containers. A version the user "
    "gave earlier in this chat still counts (each user turn ends with a note on "
    "what has been stated) — do not ask for it again. If the version is unknown "
    "and it matters, ASK for it before grepping; note when a fix landed in a "
    "later release.\n"
    f"{CITE_RULES}\n"
    "- You may reason beyond the docs — 'the docs don't cover this; based on "
    "the code at <permalink>, likely X' is in-bounds — but never invent APIs or "
    "error messages; verify them with the tools.\n"
    "- Distinguish a CONFIRMED cause (you found the raising code or a matching "
    "solved thread) from diagnostic guidance. If you cannot confirm the cause "
    "from the index or the code, DO NOT guess: give your best diagnostic "
    "guidance, and THEN always append a fileable GitHub issue draft so the user "
    "can escalate. Delimit it clearly as '--- GitHub issue draft ---' with a "
    "title line and sections for Version, Command, Traceback/symptom, What was "
    "tried, and Suspected code path (with a permalink if you have one). Remind "
    "the user to search open issues for a duplicate before filing.\n"
    "{notes}"
    "Today's date: {today}."
)


def _notes_block(config: dict, app: str) -> str:
    """Domain-boundary facts for the app + its neighbors, injected into the
    system prompt. This is how the assistant is told things the corpus/model
    gets wrong — e.g. that reconstruction is qsirecon's job, not qsiprep's."""
    lines = []
    for a in common.scope(config, app):
        for note in (config["apps"].get(a) or {}).get("notes", []) or []:
            lines.append(f"- ({a}) {' '.join(note.split())}")
    if not lines:
        return ""
    return ("Important package facts (authoritative — trust these over any "
            "older/ambiguous retrieved text):\n" + "\n".join(lines) + "\n")


def _client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set — put it in .env. Answering needs it "
            "(harvesting/indexing do not).")
    from openai import OpenAI  # import lazily
    return OpenAI()


# --- agent loop ------------------------------------------------------------

@dataclass
class AgentResult:
    answer: str
    transcript: list = field(default_factory=list)  # [{tool, args, result}]
    iterations: int = 0


HISTORY_TURNS = 12  # recent chat messages fed to the agent for follow-up context
MAX_VERSION_MENTIONS = 4  # earlier tokens the hint lists; more reads as noise


def agent_history(messages: list[dict]) -> list[dict]:
    """The prior chat turns the agent sees, as plain role/content items: the
    last HISTORY_TURNS messages, plus the opening user message once the window
    has scrolled past it.

    The opening message is where the version (and the error being chased) is
    stated, and nothing later repeats it — so once the window scrolled past it
    the agent asked for the version again. Re-sending that one message costs
    its own length per turn, small next to the recent turns it rides with, and
    keeps the mention in context: the model can tell a release from an output
    resolution, which a bare token carried forward on its own could not. The
    app logs this with each feedback entry, so a rated follow-up replays with
    exactly the turns the agent had.

    The window size is a cost/noise knob, not a hard limit: only the turns'
    text is sent (tool transcripts are not), and each turn re-bills it once per
    tool iteration via previous_response_id. Message length (pasted tracebacks,
    long answers) drives that cost more than the count does."""
    recent = messages[-HISTORY_TURNS:]
    first = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
    if first is not None and first < len(messages) - HISTORY_TURNS:
        recent = [messages[first], *recent]
    return [{"role": m["role"], "content": m["content"]} for m in recent]


def _version_mentions(text: str) -> list[str]:
    """Distinct version-looking tokens in `text`, in order of appearance. The
    pattern is deliberately loose — '1.25' (an output resolution) matches too —
    which is why every hint says the token *may* be the version."""
    return list(dict.fromkeys(VERSION_HINT_RE.findall(text or "")))


def _quoted(items: list[str]) -> str:
    quoted = [f"'{v}'" for v in items]
    if len(quoted) < 2:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def _version_hint(question: str, history: list | None = None) -> str:
    """The note appended to the user turn so the model treats the version the
    way a maintainer would: use the one stated anywhere in this chat, ask only
    when none was. Earlier user turns in `history` count — a follow-up rarely
    repeats the version given at the start of a chat, and a hint that read the
    current message alone told the model to ask for it again on every
    version-less follow-up (a tester's top complaint)."""
    now = _version_mentions(question)
    if now:
        return (f"The user's message mentions '{now[0]}', which may be the "
                "version — confirm before relying on it.")
    earlier = []
    for m in history or []:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            earlier.extend(_version_mentions(m["content"]))
    earlier = list(dict.fromkeys(earlier))
    if earlier:
        # x.y.z tokens first: they read as releases, where x.y is as often a
        # resolution or a count — so the cap drops the likelier false positives
        earlier.sort(key=lambda v: v.count(".") < 2)
        which = "which may be" if len(earlier) == 1 else "one of which may be"
        return (f"Earlier in this chat the user mentioned "
                f"{_quoted(earlier[:MAX_VERSION_MENTIONS])}, {which} the version "
                "— do not ask for it again; confirm only if it is ambiguous.")
    return "The user did not state a version; ask if it matters for the answer."


def _responses_tools() -> list[dict]:
    """TOOL_SCHEMAS is in chat.completions shape; the Responses API wants the
    function fields flattened onto the tool object."""
    return [{"type": "function", "name": t["function"]["name"],
             "description": t["function"]["description"],
             "parameters": t["function"]["parameters"]}
            for t in TOOL_SCHEMAS]


def _function_calls(resp) -> list:
    return [it for it in (resp.output or [])
            if getattr(it, "type", None) == "function_call"]


def answer_agent(question: str, app: str, config: dict, store,
                 history: list | None = None, client=None, meter=None,
                 on_step: Callable[[str, dict], None] | None = None) -> AgentResult:
    """Run the tool loop on the Responses API: the model calls search_kb /
    read_thread / grep_code / read_file until it answers or hits the iteration
    cap (then it's asked to wrap up with no tools). `on_step(tool, args)` is
    called as each tool starts, so a UI can show the work as it happens.

    The Responses API is used here — not chat.completions — because the agent
    model is a reasoning model, and chat.completions rejects function tools
    together with reasoning. `previous_response_id` threads server-side state so
    the model's reasoning carries across tool calls without us re-sending it.
    """
    client = client or _client()
    toolbox = Toolbox(config, store, app)
    instructions = SYSTEM_AGENT.format(app=app, today=date.today().isoformat(),
                                       notes=_notes_block(config, app))
    tools = _responses_tools()
    max_out = config["llm"]["max_output_tokens"]
    model = config["llm"]["agent_model"]

    input_items = list(history or [])
    input_items.append({"role": "user", "content":
                        f"{question}\n\n({_version_hint(question, history)})"})

    transcript: list = []
    max_iter = config["llm"]["max_tool_iterations"]
    resp = client.responses.create(model=model, instructions=instructions,
                                   input=input_items, tools=tools,
                                   max_output_tokens=max_out)
    if meter is not None:
        meter.record(model, getattr(resp, "usage", None))
    turns = 1
    while True:
        calls = _function_calls(resp)
        if not calls:
            return AgentResult(resp.output_text or "", transcript, turns)

        outputs = []
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_step is not None:
                on_step(call.name, args)
            result = toolbox.call(call.name, args)
            transcript.append({"tool": call.name, "args": args, "result": result})
            outputs.append({"type": "function_call_output",
                            "call_id": call.call_id, "output": result})

        if turns >= max_iter:
            break
        resp = client.responses.create(
            model=model, previous_response_id=resp.id, input=outputs,
            tools=tools, max_output_tokens=max_out)
        if meter is not None:
            meter.record(model, getattr(resp, "usage", None))
        turns += 1

    # Hit the cap — feed the last tool outputs plus a wrap-up nudge, no tools.
    resp = client.responses.create(
        model=model, previous_response_id=resp.id,
        input=outputs + [{"role": "user", "content":
                          "You've reached the tool-call limit. Answer now with "
                          "what you have, or produce the GitHub issue draft if "
                          "unresolved."}],
        max_output_tokens=max_out)
    if meter is not None:
        meter.record(model, getattr(resp, "usage", None))
    return AgentResult(resp.output_text or "", transcript, turns)
