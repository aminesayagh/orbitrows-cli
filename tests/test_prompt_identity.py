"""The prompt pipeline is a port of the PoC's (commit 8c923a8): what was tuned with the real model must stay as it was.
Every text and decimal threshold of a PoC module (its Jev questions, rules, GLiNER2 labels, cut-offs) must still be in
the ported module; code around them may change (the questions to the user were added). Needs the PoC repository next to
the CLI's (../poc), else skipped."""
import ast
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

COMMIT = "8c923a8"
SRC = Path(__file__).resolve().parents[1] / "src"
PORTED = {f"pipeline/prompt/{p.name}": f"v2/prompt_ast/{p.name}" for p in sorted((SRC / "pipeline" / "prompt").glob("*.py"))
          if p.name not in ("pipeline.py", "asking.py", "source_route.py")} | {  # source_route is new, not a PoC port
    "sql/dml.py": "v2/ast/dml.py", "sql/sql.py": "v2/ast/sql.py", "units/parameters.py": "v2/units/parameters.py"}
GONE = {"fastino/gliner2-multi-v1",                          # split.py's model name: the loader is services/extractor now
        "coref_work"}                                        # coref.py's work folder: a private temporary one per process now
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def poc_repo() -> Path | None:
    """../poc next to the CLI's main checkout (found from a worktree too), when it has COMMIT."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=SRC, env=ENV,
                         capture_output=True, text=True)
    poc = Path(out.stdout.strip()).parent.parent / "poc" if out.returncode == 0 else None
    if poc and poc.is_dir() and subprocess.run(["git", "cat-file", "-e", f"{COMMIT}^{{commit}}"], cwd=poc, env=ENV,
                                               capture_output=True).returncode == 0:
        return poc
    return None


def tuned(text: str) -> Counter:
    """The module's texts (with a letter: punctuation alone is formatting) and decimal numbers, docstrings and the
    __main__ block left out."""
    tree = ast.parse(text)
    tree.body = [n for n in tree.body if not (isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))]
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef,
            ast.AsyncFunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr)}
    return Counter(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in docs
                   and (isinstance(n.value, str) and any(c.isalpha() for c in n.value) or isinstance(n.value, float)))


@pytest.mark.skipif(poc_repo() is None, reason=f"the PoC repository (../poc with commit {COMMIT}) is not available")
def test_what_was_tuned_is_kept():
    poc, lost = poc_repo(), {}
    for module, path in PORTED.items():
        theirs = subprocess.run(["git", "show", f"{COMMIT}:{path}"], cwd=poc, env=ENV, capture_output=True, text=True,
                                check=True).stdout
        if missing := [v for v in tuned(theirs) - tuned((SRC / module).read_text()) if v not in GONE]:
            lost[module] = missing
    assert not lost, lost
