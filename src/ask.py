"""Ask the assistant a question about a BIDS App.

    python -m src.ask "how do I set --output-resolution?"
    python -m src.ask --agent "paste a traceback here..."
    python -m src.ask --oneshot "..."      # force the cheap path
    python -m src.ask --app qsiprep "..."  # v0 has one app; this is the default

Auto-routes between a one-shot RAG answer and the agentic tool loop, prints the
routing decision, the answer, and every source consulted.
"""
import json
import sys
from pathlib import Path

from . import common  # first import: runs the truststore inject
from . import answer as answer_mod
from . import router as router_mod
from .store import Store


def load_manifest(config: dict) -> dict:
    """Refuse to run against a missing index or a mismatched embedding model."""
    path = Path(config["index"]["path"]) / "manifest.json"
    if not path.exists():
        sys.exit(f"No index manifest at {path}. Build it first: "
                 "python -m src.ingest")
    manifest = json.loads(path.read_text())
    built_with = manifest.get("embedding_model")
    configured = config["retrieval"]["embed_model"]
    if built_with != configured:
        sys.exit(
            "Embedding model mismatch — refusing to run:\n"
            f"  index was built with: {built_with}\n"
            f"  config says:          {configured}\n"
            "Mismatched embeddings silently wreck retrieval. Set "
            "retrieval.embed_model to match the index, or rebuild with ingest.")
    return manifest


def _parse_args(argv: list[str]) -> tuple[str, str | None, str]:
    force, app, words = None, None, []
    it = iter(argv)
    for a in it:
        if a in ("--agent", "--oneshot"):
            force = a.lstrip("-")
        elif a == "--app":
            app = next(it, None)
        else:
            words.append(a)
    return " ".join(words).strip(), app, force


def _print_sources(chunks: list[dict]) -> None:
    print("\nSources:")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] {c.get('title', '?')} — {c.get('source', '')}")
        print(f"      {c.get('url', '')}")


def _print_transcript(transcript: list[dict]) -> None:
    print("\nTool calls:")
    for step in transcript:
        args = ", ".join(f"{k}={v!r}" for k, v in step["args"].items())
        head = step["result"].splitlines()[0] if step["result"] else ""
        print(f"  {step['tool']}({args})")
        print(f"    -> {head}")


def main():
    question, app, force = _parse_args(sys.argv[1:])
    if not question:
        sys.exit('usage: python -m src.ask [--agent|--oneshot] [--app NAME] '
                 '"your question"')

    config = common.load_config()
    load_manifest(config)
    app = app or next(iter(config["apps"]))
    store = Store(config)

    if force == "oneshot":
        chunks = store.hybrid_query(question, k=config["retrieval"]["top_k"],
                                    where={"app": router_mod.scope(config, app)})
        decision = router_mod.Decision("oneshot", chunks, "forced --oneshot")
    elif force == "agent":
        decision = router_mod.Decision("agent", [], "forced --agent")
    else:
        decision = router_mod.route(question, store, config, app)

    print(f"[route: {decision.path} — {decision.reason}]\n")

    if decision.path == "oneshot":
        if not decision.chunks:
            sys.exit("Nothing retrieved — is the index empty? Re-run ingest.")
        print(answer_mod.answer_oneshot(question, decision.chunks, app, config))
        _print_sources(decision.chunks)
    else:
        result = answer_mod.answer_agent(question, app, config, store)
        print(result.answer)
        if result.transcript:
            _print_transcript(result.transcript)
        print(f"\n[{result.iterations} model turn(s)]")


if __name__ == "__main__":
    main()
