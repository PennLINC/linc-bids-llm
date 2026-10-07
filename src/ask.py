"""Ask the assistant a question about a BIDS App.

    python -m src.ask "how do I set --output-resolution?"
    python -m src.ask --app qsirecon "..."   # default: the first app in config

Runs the agent's tool loop and prints the answer and every tool call it made.
"""
import json
import sys
from pathlib import Path

from . import common  # first import: runs the truststore inject
from . import answer as answer_mod
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


def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    app, words = None, []
    it = iter(argv)
    for a in it:
        if a == "--app":
            app = next(it, None)
        elif a in ("--agent", "--oneshot"):
            continue      # retired path switches: every question goes to the agent
        else:
            words.append(a)
    return " ".join(words).strip(), app


def _print_transcript(transcript: list[dict]) -> None:
    print("\nTool calls:")
    for step in transcript:
        args = ", ".join(f"{k}={v!r}" for k, v in step["args"].items())
        head = step["result"].splitlines()[0] if step["result"] else ""
        print(f"  {step['tool']}({args})")
        print(f"    -> {head}")


def main():
    question, app = _parse_args(sys.argv[1:])
    if not question:
        sys.exit('usage: python -m src.ask [--app NAME] "your question"')

    config = common.load_config()
    load_manifest(config)
    app = app or next(iter(config["apps"]))
    store = Store(config)

    result = answer_mod.answer_agent(question, app, config, store)
    print(result.answer)
    if result.transcript:
        _print_transcript(result.transcript)
    print(f"\n[{result.iterations} model turn(s)]")


if __name__ == "__main__":
    main()
