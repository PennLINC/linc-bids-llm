import json
from pathlib import Path

import pytest

from src.ask import load_manifest, _parse_args


def write_manifest(config, model):
    path = Path(config["index"]["path"])
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps({"embedding_model": model}))


def test_load_manifest_missing_index_exits(config):
    with pytest.raises(SystemExit, match="No index manifest"):
        load_manifest(config)


def test_load_manifest_model_mismatch_exits(config):
    write_manifest(config, "some/other-model")
    with pytest.raises(SystemExit, match="mismatch"):
        load_manifest(config)


def test_load_manifest_match_ok(config):
    write_manifest(config, config["retrieval"]["embed_model"])
    manifest = load_manifest(config)
    assert manifest["embedding_model"] == config["retrieval"]["embed_model"]


def test_parse_args():
    assert _parse_args(["how", "do", "I", "run"]) == ("how do I run", None)
    assert _parse_args(["--app", "qsiprep", "why", "fail"]) == ("why fail", "qsiprep")
    # the retired path switches are dropped, not read as part of the question
    assert _parse_args(["--agent", "a", "traceback"]) == ("a traceback", None)
    assert _parse_args(["--oneshot", "q"]) == ("q", None)
    # flags of the app being asked about stay in the question
    assert _parse_args(["what", "is", "--output-resolution"]) == (
        "what is --output-resolution", None)
