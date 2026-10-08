"""scripts/add_app_to_config.py: copying an app's entry from the template into
a host's own config.yaml without disturbing anything else in it."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "config.example.yaml").read_text()

spec = importlib.util.spec_from_file_location(
    "add_app_to_config", ROOT / "scripts" / "add_app_to_config.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def server_copy_without(app: str) -> str:
    """The template as a host has it: its own contact and budget, and no entry
    for `app` yet (that entry's comment lines included)."""
    entry = mod.entry_lines(TEMPLATE, app)
    text = TEMPLATE.replace(entry, "").replace("\n\n\n", "\n\n")
    text = text.replace("contact_email: you@example.edu",
                        "contact_email: lab@example.org   # this host")
    return text.replace("daily_budget_usd: 300", "daily_budget_usd: 50")


def test_adds_the_entry_and_keeps_the_hosts_settings():
    config = server_copy_without("babs")
    assert "babs" not in yaml.safe_load(config)["apps"]
    new = mod.add_app(config, TEMPLATE, "babs")
    parsed = yaml.safe_load(new)
    assert parsed["apps"]["babs"] == yaml.safe_load(TEMPLATE)["apps"]["babs"]
    assert list(parsed["apps"])[-1] == "babs"           # appended to apps:
    assert parsed["contact_email"] == "lab@example.org"
    assert parsed["llm"]["daily_budget_usd"] == 50
    assert "# BABS runs other BIDS Apps at scale" in new  # comments come along
    assert new.replace(mod.entry_lines(TEMPLATE, "babs") + "\n", "", 1) == config


def test_refuses_an_app_already_there_or_missing_from_the_template():
    with pytest.raises(SystemExit, match="already in the config"):
        mod.add_app(TEMPLATE, TEMPLATE, "babs")
    with pytest.raises(SystemExit, match="no 'fmriprep' entry"):
        mod.add_app(server_copy_without("babs"), TEMPLATE, "fmriprep")


def test_entry_ends_before_the_next_top_level_key():
    config = "apps:\n  qsiprep:\n    github_repo: PennLINC/qsiprep\nretrieval:\n  top_k: 8\n"
    new = mod.add_app(config, TEMPLATE, "babs")
    parsed = yaml.safe_load(new)
    assert list(parsed["apps"]) == ["qsiprep", "babs"]
    assert parsed["retrieval"] == {"top_k": 8}


def test_main_writes_the_file(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(server_copy_without("babs"))
    tpl = tmp_path / "config.example.yaml"
    tpl.write_text(TEMPLATE)
    mod.main(["babs", "--config", str(cfg), "--template", str(tpl)])
    assert "babs" in yaml.safe_load(cfg.read_text())["apps"]
    assert capsys.readouterr().out.startswith(f"added babs to {cfg}; apps: qsiprep,")
