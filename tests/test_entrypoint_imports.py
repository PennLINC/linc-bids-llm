"""app.py and probes/*.py are run, never imported by a test, so a `from src.x
import name` that src/ no longer provides fails only when they start (CI's
undefined-name check cannot see it). Resolve every project import they make.
`import app` itself cannot be the check: outside a Streamlit runtime st.stop()
does not stop, so after setup() fails (no index on a fresh checkout) the
module-level code runs on into a NameError."""
import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINTS = [ROOT / "app.py"] + sorted((ROOT / "probes").glob("*.py"))
PROJECT = ("src", "eval")


def project_imports(path):
    """(module, name) per imported name: `from src.x import a, b` gives
    ("src.x", "a") and ("src.x", "b"); `import src.x` gives ("src.x", None)
    and `from src.x import *` ("src.x", "*")."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in PROJECT:
                for alias in node.names:
                    yield node.module, alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PROJECT:
                    yield alias.name, None


def resolve(module, name):
    mod = importlib.import_module(module)
    if name in (None, "*") or hasattr(mod, name):
        return
    try:
        importlib.import_module(f"{module}.{name}")   # `from src import answer`
    except ModuleNotFoundError as err:
        if err.name != f"{module}.{name}":
            raise                                     # the submodule's own problem
        raise ImportError(f"{module} has no {name!r}") from None


@pytest.mark.parametrize("path", ENTRYPOINTS,
                         ids=lambda p: p.relative_to(ROOT).as_posix())
def test_project_imports_resolve(path):
    imports = list(project_imports(path))
    assert imports, f"{path.name} imports nothing from {PROJECT} — did it move?"
    missing = []
    for module, name in imports:
        try:
            resolve(module, name)
        except ImportError as err:
            missing.append(f"from {module} import {name}: {err}")
    assert missing == []
