"""Streamlit chat UI over the same agent as ask.py.

    streamlit run app.py

Runs locally in the browser (localhost:8501); nothing is hosted. A lab member
who has never cloned the app can paste an error and get a linked, version-aware
answer. Each tool call shows while the agent works and stays in an expander
after, so maintainers can see the assistant's work. Thumbs+comment feedback is
logged to .feedback/ (gitignored) — the tuning signal for Stage 6.
"""
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src import answer as answer_mod
from src import common
from src.ask import load_manifest
from src.budget import Budget
from src.checkouts import cloned_tags
from src.feedback import issue_url, log_feedback, run_context
from src.store import Store
from src.tools import describe_call

CHATS_DIR = Path(".chats")

# Problem categories for structured feedback (first entry = "no problem").
CATEGORIES = ["— (looked good)", "wrong fix / advice", "bad or broken sources",
              "hallucination / made-up detail", "wrong version",
              "didn't escalate / ask for info", "other"]

# TEMPORARY (testing phase): chat history is saved on the server for evaluation.
# Remove this banner + its two render sites once testing wraps.
DISCLAIMER = ("⚠️ **Testing preview** — your chat history is saved on the server "
              "for evaluation. Please **don't enter personal or sensitive "
              "information**.")


@st.cache_resource
def setup():
    """Config, index check, store, and the shared daily-spend budget — cached
    across reruns so every session shares one running spend total."""
    config = common.load_config()
    manifest = load_manifest(config)  # raises SystemExit with a clear message
    return config, manifest, Store(config), Budget(config)


# --- local chat cache ---------------------------------------------------------

def load_chats(scope: str) -> list[dict]:
    if not CHATS_DIR.exists():
        return []
    chats = []
    for path in CHATS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("scope") == scope:
            chats.append(data)
    return sorted(chats, key=lambda c: c.get("created", ""), reverse=True)


def save_chat(chat: dict) -> None:
    CHATS_DIR.mkdir(exist_ok=True)
    (CHATS_DIR / f"{chat['id']}.json").write_text(json.dumps(chat, indent=2))


# --- rendering ----------------------------------------------------------------

def render_assistant(msg: dict) -> None:
    """Render a stored assistant turn: answer, then how it was produced."""
    st.markdown(msg["content"])
    if msg.get("seconds") is not None:
        st.caption(f"{len(msg.get('transcript') or [])} tool calls · "
                   f"{msg['seconds']:.0f} s")
    # Chats saved before every answer came from the agent: the one-shot path's
    # answers carry the routing decision and the chunks they were given.
    if msg.get("route_reason"):
        st.caption(f"path: **{msg['route_path']}** — {msg['route_reason']}")
    if msg.get("sources"):
        with st.expander(f"Sources ({len(msg['sources'])})"):
            for i, s in enumerate(msg["sources"], 1):
                st.markdown(f"{i}. [{s['title']}]({s['url']}) — *{s['source']}*")
    if msg.get("transcript"):
        with st.expander(f"Tool calls ({len(msg['transcript'])})"):
            for step in msg["transcript"]:
                args = ", ".join(f"{k}={v!r}" for k, v in step["args"].items())
                st.markdown(f"**{step['tool']}**({args})")
                st.code(step["result"][:1500])


# --- feedback -----------------------------------------------------------------

def feedback_block(app: str, state: dict, config: dict, manifest: dict) -> None:
    messages = state["messages"]
    if not messages or messages[-1]["role"] != "assistant":
        return
    question = messages[-2]["content"] if len(messages) >= 2 else ""
    answer_md = messages[-1]["content"]
    path = messages[-1].get("route_path", "?")
    key = f"{app}::{state['id']}::{len(messages)}"

    with st.expander("Rate this answer / report a problem"):
        # The thumb is the field everything downstream keys on, and the one
        # testers skipped (it has no label of its own) — so it is asked for by
        # name here and gates the button below.
        st.caption("Was this answer good? Pick 👍 or 👎 to rate it.")
        rating = st.feedback("thumbs", key=f"rate::{key}")
        category = st.selectbox(
            "If it wasn't good, what was wrong?", CATEGORIES, key=f"cat::{key}")
        correct_url = st.text_input(
            "Correct source URL, if you know it", key=f"url::{key}",
            placeholder="https://github.com/... or https://neurostars.org/...",
            help="Lets this case become a retrieval regression test.")
        comment = st.text_input(
            "What was wrong, or what should it have said?", key=f"comm::{key}")
        if st.button("Log feedback", key=f"log::{key}", disabled=rating is None,
                     help="Pick 👍 or 👎 first." if rating is None else None):
            log_feedback({
                "app": app,
                "path": path,
                "seconds": messages[-1].get("seconds"),
                "tool_calls": len(messages[-1].get("transcript") or []),
                # Which answer this is, and for whom: the shared login has no
                # users, so a random per-browser id is what tells one tester
                # revising a rating from two testers rating the same chat.
                "session": st.session_state.setdefault("sid", uuid.uuid4().hex[:8]),
                "chat_id": state["id"],
                "turn": sum(m["role"] == "assistant" for m in messages),
                # A follow-up's question means little alone ("and on 1.0.0?");
                # keep the turns the agent was given so the case can be replayed.
                "history": answer_mod.agent_history(messages[:-2]),
                "question": question,
                "answer": answer_md,
                "rating": {0: "down", 1: "up"}.get(rating),
                "category": None if category == CATEGORIES[0] else category,
                "correct_url": correct_url.strip() or None,
                "comment": comment,
                "index_built": manifest.get("built_at"),
                **run_context(config, path),   # models/embed/commit provenance
            })
            st.toast("Feedback logged — thanks!")
        repo = (config.get("feedback") or {}).get("github_repo")
        if repo:
            st.link_button("Report on GitHub",
                           issue_url(repo, question, answer_md, f"{app} / {path}"))


# --- answering ----------------------------------------------------------------

def answer_turn(question: str, app: str, config: dict, store,
                history: list[dict], meter=None, on_step=None) -> dict:
    """Answer with the agent and package the assistant message dict (content
    + how it was produced) for storage/rendering."""
    started = time.monotonic()
    result = answer_mod.answer_agent(question, app, config, store, history=history,
                                     meter=meter, on_step=on_step)
    return {"role": "assistant", "route_path": "agent", "content": result.answer,
            "transcript": result.transcript,
            "seconds": round(time.monotonic() - started, 1)}


# --- entry --------------------------------------------------------------------

st.set_page_config(page_title="PennLINC Assistant", page_icon="🧠")

try:
    config, manifest, store, budget = setup()
except SystemExit as e:
    st.error(str(e))
    st.stop()

# Apps shown as their own tool. `hidden` apps still exist for neighbor scoping
# (e.g. ModelArrayIO rides under ModelArray) but get no card / selector entry.
apps = [a for a, d in config["apps"].items() if not (d or {}).get("hidden")]


def _blurb(app: str) -> str:
    return (config["apps"].get(app) or {}).get("blurb", "")


# --- landing page: choose a tool ---------------------------------------------
# No app selected yet (fresh visit, or "All tools" clicked) -> show the picker.
if st.session_state.get("app") not in apps:
    st.title("PennLINC Assistant")
    st.caption("Troubleshooting for the lab's BIDS Apps. Pick a tool to start — "
               "every answer links back to the issue, thread, doc, or "
               "version-pinned code it came from.")
    st.warning(DISCLAIMER)          # TEMPORARY (testing phase)
    st.write("")
    cols = st.columns(2)
    for i, a in enumerate(apps):
        with cols[i % 2].container(border=True):
            st.subheader(a)
            st.caption(_blurb(a) or "​")   # zero-width keeps card heights even
            if st.button("Open", key=f"open::{a}", use_container_width=True):
                st.session_state.app = a
                st.rerun()
    st.stop()

app = st.session_state.app

with st.sidebar:
    st.title("PennLINC Assistant")
    if st.button("← All tools", use_container_width=True):
        st.session_state.app = None
        st.rerun()
    app = st.selectbox("App", apps, index=apps.index(app))
    st.session_state.app = app
    if _blurb(app):
        st.caption(_blurb(app))
    st.warning(DISCLAIMER)          # TEMPORARY (testing phase)
    st.divider()
    st.markdown(
        f"**Index built:** {manifest.get('built_at', '?')}\n\n"
        f"**Chunks:** "
        + ", ".join(f"{k}: {v}" for k, v in manifest.get("chunks", {}).items())
        + f"\n\n**Checkouts ({app}):** "
        + (", ".join(cloned_tags(config, app)) or "none — run `python -m src.checkouts`")
        + f"\n\n**Model:** `{config['llm']['agent_model']}`"
    )
    if budget.limit is not None:
        st.caption(f"Spend today (UTC): ${budget.spent_today():.2f} / "
                   f"${budget.limit:.0f}")

state = st.session_state.setdefault("chats", {}).setdefault(
    app, {"id": None, "created": None, "messages": []})

with st.sidebar:
    st.divider()
    st.subheader("Chats")
    if st.button("＋ New chat"):
        state.update(id=None, created=None, messages=[])
    for prior in load_chats(app)[:20]:
        if st.button(prior.get("title", "(chat)")[:48], key=f"load::{prior['id']}"):
            state.update(id=prior["id"], created=prior.get("created"),
                         messages=prior.get("messages", []))

st.title(f"Ask about {app}")

for message in state["messages"]:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            render_assistant(message)
        else:
            st.markdown(message["content"])

if question := st.chat_input(f"Paste an error or ask about {app}…"):
    state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        if budget.over():
            st.error(f"The daily budget of ${budget.limit:.0f} (UTC) has been "
                     "reached — please try again tomorrow. Ping a maintainer if "
                     "you need it raised.")
            st.stop()
        history = answer_mod.agent_history(state["messages"][:-1])
        error = None
        # The agent takes tens of seconds; show each step as it starts.
        with st.status("Thinking…") as status:
            steps = []

            def show_step(tool: str, args: dict) -> None:
                steps.append(describe_call(tool, args))
                status.update(label=f"{steps[-1]}…")
                status.write(steps[-1])

            try:
                msg = answer_turn(question, app, config, store, history,
                                  meter=budget, on_step=show_step)
                status.update(label=f"Done: {len(steps)} steps in "
                                    f"{msg['seconds']:.0f} s",
                              state="complete", expanded=False)
            except SystemExit as e:  # e.g. missing OPENAI_API_KEY
                error = str(e)
                status.update(label="Couldn't answer", state="error")
        if error:
            st.error(error)
            st.stop()
        render_assistant(msg)
    state["messages"].append(msg)

    if state["id"] is None:
        slug = re.sub(r"\W+", "-", question)[:32].strip("-")
        state["id"] = datetime.now().strftime("%Y%m%d-%H%M%S-") + slug
        state["created"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_chat({
        "id": state["id"], "scope": app,
        "title": state["messages"][0]["content"][:60],
        "created": state["created"], "messages": state["messages"],
    })

feedback_block(app, state, config, manifest)
