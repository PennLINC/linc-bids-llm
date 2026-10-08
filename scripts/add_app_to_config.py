"""Copy one app's entry from config.example.yaml into config.yaml.

config.yaml is each host's own copy of the template (gitignored), so an app
added to the template never reaches a running server by itself. This copies
the entry's lines as written, comments included, to the end of `apps:`, and
writes the file only if the result parses with every other setting unchanged.

    ~/miniforge3/envs/linc-bids-llm/bin/python scripts/add_app_to_config.py babs

The next refresh then harvests the app whole (an incremental sync takes a new
app as all-new) and clones its checkouts; see DEPLOY.md, "Adding an app".
"""
import argparse
from pathlib import Path

import yaml


def entry_lines(template: str, app: str) -> str:
    """The app's entry in the template: the comment lines right above it,
    then every line indented under it."""
    lines = template.splitlines(keepends=True)
    try:
        i = lines.index(f"  {app}:\n")
    except ValueError:
        raise SystemExit(f"the template has no '{app}' entry under apps:") from None
    start = i
    while start and lines[start - 1].startswith("  #"):
        start -= 1
    end = i + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    while not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end])


def add_app(config: str, template: str, app: str) -> str:
    """`config` with the template's entry for `app` added at the end of apps:."""
    old = yaml.safe_load(config)
    if app in (old.get("apps") or {}):
        raise SystemExit(f"{app} is already in the config; nothing to change")
    lines = config.splitlines(keepends=True)
    try:
        apps_at = lines.index("apps:\n")
    except ValueError:
        raise SystemExit("the config has no top-level 'apps:' line") from None
    # the entry goes before the next top-level key, or at the end of the file
    nxt = next((j for j in range(apps_at + 1, len(lines))
                if lines[j][:1] not in ("", " ", "#", "\n")), len(lines))
    head = "".join(lines[:nxt])
    if not head.endswith("\n\n"):
        head += "\n"
    new = head + entry_lines(template, app) + "\n" + "".join(lines[nxt:])

    parsed = yaml.safe_load(new)
    others = {k: v for k, v in parsed["apps"].items() if k != app}
    if (parsed["apps"].get(app) != yaml.safe_load(template)["apps"][app]
            or others != old["apps"]
            or {k: v for k, v in parsed.items() if k != "apps"}
            != {k: v for k, v in old.items() if k != "apps"}):
        raise SystemExit("the edited config would not parse as expected; "
                         "nothing was written")
    return new


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("app", help="the app's key under apps:, e.g. babs")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--template", default="config.example.yaml")
    args = ap.parse_args(argv)
    path = Path(args.config)
    new = add_app(path.read_text(), Path(args.template).read_text(), args.app)
    path.write_text(new)
    print(f"added {args.app} to {path}; apps: "
          + ", ".join(yaml.safe_load(new)["apps"]))


if __name__ == "__main__":
    main()
