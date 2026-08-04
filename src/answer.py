"""The two answer paths and their prompts.

Unlike linc-llm's strict "answer only from context", this assistant is
diagnostic: it may reason beyond the retrieved text, but must label speculation,
cite what it used (URLs / permalinks), and ask for the version + full traceback
when the input is thin. When genuinely stuck it drafts a GitHub issue rather
than guessing.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date

from . import common
from .tools import TOOL_SCHEMAS, Toolbox

VERSION_HINT_RE = re.compile(r'\b\d+\.\d+(?:\.\d+)?(?:rc\d+)?\b')

CITE_RULES = (
    "Every claim must be traceable: cite the source you used by its URL or "
    "GitHub permalink inline. If you reason beyond the sources, label it "
    "explicitly (e.g. 'Likely, though not documented: ...'). If the version "
    "the user ran is unknown and it matters, ask for it."
)

SYSTEM_ONESHOT = (
    "You are a troubleshooting assistant for the {app} BIDS App, answering a "
    "member of the lab. You are given numbered context chunks retrieved from "
    "past GitHub issues, solved NeuroStars threads, and the docs.\n"
    "- Prefer answering from the context; cite chunks by their bracket index "
    "like [1] or [2][4], and only cite chunks you actually used.\n"
    "- You may add practical diagnostic reasoning, but label anything not "
    "supported by the context as such.\n"
    "- If the context does not actually answer the question, say so and ask "
    "for the version, exact command, and full traceback.\n"
    "- Be concise and practical; give the steps, not a preamble.\n"
    "Today's date: {today}."
)

SYSTEM_AGENT = (
    "You are a maintainer-style troubleshooting assistant for the {app} BIDS "
    "App, helping a member of the lab diagnose an error or answer a question.\n"
    "Work like a maintainer:\n"
    "1. FIRST call search_kb — someone may have already hit this (closed "
    "issues, solved NeuroStars threads).\n"
    "2. If it's a code question or the traceback points at source, grep_code "
    "the version the user ran to find the raising line, then read_file to read "
    "it and get a permalink.\n"
    "3. Version awareness is core: users run old containers. If the version is "
    "unknown and it matters, ASK for it before grepping; note when a fix landed "
    "in a later release.\n"
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
    "Today's date: {today}."
)


def _client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set — put it in .env. The answer paths need "
            "it (harvesting/indexing do not).")
    from openai import OpenAI  # import lazily
    return OpenAI()


def _chat(client, model: str, messages: list, config: dict, tools=None,
          meter=None):
    kwargs = dict(model=model, messages=messages,
                  max_completion_tokens=config["llm"]["max_output_tokens"])
    if tools:
        kwargs["tools"] = tools
    resp = client.chat.completions.create(**kwargs)
    if meter is not None:
        meter.record(model, getattr(resp, "usage", None))
    return resp.choices[0].message


# --- one-shot path ---------------------------------------------------------

def _oneshot_user_prompt(question: str, chunks: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        solved = " (solved)" if c.get("gh_solved") or c.get("ns_solved") else ""
        parts.append(f"[{i}] {c.get('title', '?')} — {c.get('source', '')}{solved}\n"
                     f"{c.get('url', '')}\n{c['text']}")
    return "Context chunks:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {question}"


def answer_oneshot(question: str, chunks: list[dict], app: str,
                   config: dict, client=None, meter=None) -> str:
    client = client or _client()
    system = SYSTEM_ONESHOT.format(app=app, today=date.today().isoformat())
    msg = _chat(client, config["llm"]["oneshot_model"], [
        {"role": "system", "content": system},
        {"role": "user", "content": _oneshot_user_prompt(question, chunks)},
    ], config, meter=meter)
    return msg.content or ""


# --- agentic path ----------------------------------------------------------

@dataclass
class AgentResult:
    answer: str
    transcript: list = field(default_factory=list)  # [{tool, args, result}]
    iterations: int = 0


def _version_hint(question: str) -> str:
    m = VERSION_HINT_RE.search(question)
    if m:
        return (f"The user's message mentions '{m.group(0)}', which may be the "
                "version — confirm before relying on it.")
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
                 history: list | None = None, client=None, meter=None) -> AgentResult:
    """Run the tool loop on the Responses API: the model calls search_kb /
    grep_code / read_file until it answers or hits the iteration cap (then it's
    asked to wrap up with no tools).

    The Responses API is used here — not chat.completions — because the agent
    model is a reasoning model, and chat.completions rejects function tools
    together with reasoning. `previous_response_id` threads server-side state so
    the model's reasoning carries across tool calls without us re-sending it.
    """
    client = client or _client()
    toolbox = Toolbox(config, store, app)
    instructions = SYSTEM_AGENT.format(app=app, today=date.today().isoformat())
    tools = _responses_tools()
    max_out = config["llm"]["max_output_tokens"]
    model = config["llm"]["agent_model"]

    input_items = list(history or [])
    input_items.append({"role": "user",
                        "content": f"{question}\n\n({_version_hint(question)})"})

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
