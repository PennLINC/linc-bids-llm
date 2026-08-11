"""Streamlit chat UI over the same pipeline as ask.py: router -> one-shot RAG
or the agentic tool loop.

    streamlit run app.py

Runs locally in the browser (localhost:8501); nothing is hosted. A lab member
who has never cloned the app can paste an error and get a linked, version-aware
answer. The routing decision and every tool call are shown in expanders so
maintainers can see the assistant's work. Thumbs+comment feedback is logged to
.feedback/ (gitignored) — the tuning signal for Stage 6.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src import answer as answer_mod
from src import common, router as router_mod
from src.ask import load_manifest
from src.budget import Budget
from src.checkouts import cloned_tags
from src.feedback import issue_url, log_feedback, run_context
from src.store import Store

CHATS_DIR = Path(".chats")
HISTORY_TURNS = 6  # prior messages fed to the agent for follow-up context

# Problem categories for structured feedback (first entry = "no problem").
CATEGORIES = ["— (looked good)", "wrong fix / advice", "bad or broken sources",
              "hallucination / made-up detail", "wrong version",
              "didn't escalate / ask for info", "other"]


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


def agent_history(messages: list[dict]) -> list[dict]:
    """Recent turns as plain role/content items for the agent's context."""
    return [{"role": m["role"], "content": m["content"]}
            for m in messages[-HISTORY_TURNS:]]


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
        rating = st.feedback("thumbs", key=f"rate::{key}")
        category = st.selectbox(
            "If it wasn't good, what was wrong?", CATEGORIES, key=f"cat::{key}")
        correct_url = st.text_input(
            "Correct source URL, if you know it", key=f"url::{key}",
            placeholder="https://github.com/... or https://neurostars.org/...",
            help="Lets this case become a retrieval regression test.")
        comment = st.text_input(
            "What was wrong, or what should it have said?", key=f"comm::{key}")
        if st.button("Log feedback", key=f"log::{key}"):
            log_feedback({
                "app": app,
                "path": path,
                "question": question,
                "answer": answer_md,
                "rating": {0: "down", 1: "up"}.get(rating),
                "category": None if category == CATEGORIES[0] else category,
                "correct_url": correct_url.strip() or None,
                "comment": comment,
                "index_built": manifest.get("built_at"),
                **run_context(config, path),   # models/embed/commit provenance
            })
            st.toast("Logged to .feedback/ — thanks! Submit with "
                     "scripts/submit_feedback.sh")
        repo = (config.get("feedback") or {}).get("github_repo")
        if repo:
            st.link_button("Report on GitHub",
                           issue_url(repo, question, answer_md, f"{app} / {path}"))


# --- answering ----------------------------------------------------------------

def answer_turn(question: str, app: str, mode: str, config: dict, store,
                history: list[dict], meter=None) -> dict:
    """Route (respecting a manual override), answer, and package the assistant
    message dict (content + how it was produced) for storage/rendering."""
    if mode == "One-shot":
        chunks = store.hybrid_query(question, k=config["retrieval"]["top_k"],
                                    where={"app": router_mod.scope(config, app)})
        decision = router_mod.Decision("oneshot", chunks, "forced (sidebar)")
    elif mode == "Agent":
        decision = router_mod.Decision("agent", [], "forced (sidebar)")
    else:
        decision = router_mod.route(question, store, config, app)

    msg = {"role": "assistant", "route_path": decision.path,
           "route_reason": decision.reason}
    if decision.path == "oneshot":
        if not decision.chunks:
            msg["content"] = ("Nothing relevant in the index for that. Try "
                              "rephrasing, or switch to Agent mode in the sidebar.")
            return msg
        msg["content"] = answer_mod.answer_oneshot(
            question, decision.chunks, app, config, meter=meter)
        msg["sources"] = [{"title": c.get("title", "?"), "url": c.get("url", ""),
                           "source": c.get("source", "")} for c in decision.chunks]
    else:
        result = answer_mod.answer_agent(question, app, config, store,
                                         history=history, meter=meter)
        msg["content"] = result.answer
        msg["transcript"] = result.transcript
    return msg


# --- entry --------------------------------------------------------------------

st.set_page_config(page_title="bids-assistant", page_icon="🧠")

try:
    config, manifest, store, budget = setup()
except SystemExit as e:
    st.error(str(e))
    st.stop()

apps = list(config["apps"].keys())

with st.sidebar:
    st.title("bids-assistant")
    st.caption("Troubleshooting for the lab's BIDS Apps — every answer links "
               "back to issues, threads, docs, or version-pinned code.")
    app = st.selectbox("App", apps) if len(apps) > 1 else apps[0]
    mode = st.radio("Answer mode", ["Auto", "One-shot", "Agent"], horizontal=True,
                    help="Auto routes FAQ-shaped questions to a fast one-shot "
                         "answer and tracebacks/code questions to the agent.")
    st.divider()
    st.markdown(
        f"**Index built:** {manifest.get('built_at', '?')}\n\n"
        f"**Chunks:** "
        + ", ".join(f"{k}: {v}" for k, v in manifest.get("chunks", {}).items())
        + f"\n\n**Checkouts ({app}):** "
        + (", ".join(cloned_tags(config, app)) or "none — run `python -m src.checkouts`")
        + f"\n\n**Models:** one-shot `{config['llm']['oneshot_model']}`, "
        f"agent `{config['llm']['agent_model']}`"
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
        history = agent_history(state["messages"][:-1])
        with st.spinner("Retrieving + answering…"):
            try:
                msg = answer_turn(question, app, mode, config, store, history,
                                  meter=budget)
            except SystemExit as e:  # e.g. missing OPENAI_API_KEY
                st.error(str(e))
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
