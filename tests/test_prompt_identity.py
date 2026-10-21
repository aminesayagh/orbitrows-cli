"""The prompt pipeline is a port: every ported module equals the PoC's (commit 8c923a8) once import lines are removed,
except the adaptations listed in ADAPTED and REMOVED. Each Jev question, rule, threshold and GLiNER2 label was tuned
with the real model; a change outside them fails here. Needs the PoC repository next to the CLI's (../poc), else
skipped."""
import difflib
import os
import re
import subprocess
from pathlib import Path

import pytest

COMMIT = "8c923a8"
SRC = Path(__file__).resolve().parents[1] / "src"
PORTED = {f"pipeline/prompt/{p.name}": f"v2/prompt_ast/{p.name}"
          for p in sorted((SRC / "pipeline" / "prompt").glob("*.py")) if p.name != "pipeline.py"} | {
    "sql/dml.py": "v2/ast/dml.py", "sql/sql.py": "v2/ast/sql.py", "units/parameters.py": "v2/units/parameters.py"}
IMPORT = re.compile(r"\s*(from \S+ import |import \S)")
MAIN = '\nif __name__ == "__main__":'                           # the PoC's self-checks and CLIs: not ported
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

# (PoC text, CLI text) per module: the PoC's model-layer port (branch model-layer, 6486188), then the CLI's paths
JEV_DOC = ("v2/jev/client.py", "v2/model/client.py")
ADAPTED = {
    "pipeline/prompt/__init__.py": [("keep jev.client.SURE", "keep model.client.SURE")],
    "pipeline/prompt/check_cuts.py": [JEV_DOC, ("plan_cuts(prompt, model or load_model())",
                                                "plan_cuts(prompt, model or extractor.connect())")],
    "pipeline/prompt/coref.py": [JEV_DOC, ("ROOT = HERE.parents[1]", "ROOT = HERE.parents[2]"),
                                 ("# the poc folder: .venv-corpipe", "# the CLI folder: .venv-corpipe")],
    "pipeline/prompt/corpipe_server.py": [('parents[2] / ".venv-corpipe"', 'parents[3] / ".venv-corpipe"')],
    "pipeline/prompt/link.py": [JEV_DOC],
    "pipeline/prompt/operations_model.py": [("model = load_model()", "model = extractor.connect()")],
    "pipeline/prompt/split.py": [("model = model or load_model()", "model = model or extractor.connect()")],
}
# split.py's GLiNER2 loader moved to services/extractor/client.py (model-layer): these PoC blocks are gone
REMOVED = {"pipeline/prompt/split.py": [
    '# multilingual; gliner2.5-multi-v1 only returned short nouns (0/12 splits)\nMODEL = "fastino/gliner2-multi-v1"\n',
    "_models = {}\n\n\ndef load_model(name: str = MODEL):\n    if name not in _models:\n"
    "        _models[name] = AutoExtractor.from_pretrained(name)\n    return _models[name]\n\n\n"]}


def poc_repo() -> Path | None:
    """../poc next to the CLI's main checkout (found from a worktree too), when it has COMMIT."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=SRC, env=ENV,
                         capture_output=True, text=True)
    poc = Path(out.stdout.strip()).parent.parent / "poc" if out.returncode == 0 else None
    if poc and poc.is_dir() and subprocess.run(["git", "cat-file", "-e", f"{COMMIT}^{{commit}}"], cwd=poc, env=ENV,
                                               capture_output=True).returncode == 0:
        return poc
    return None


POC = poc_repo()


def code(text: str, module: str) -> list[str]:
    """The lines that matter: no import line, no __main__ block (corpipe_server.py is a script: it keeps its own)."""
    if (i := text.find(MAIN)) >= 0 and not module.endswith("corpipe_server.py"):
        text = text[:i]
    return [line.rstrip() for line in text.rstrip().splitlines() if not IMPORT.match(line)]


def adapt(lines: list[str], module: str) -> list[str]:
    """The PoC's lines with the documented adaptations applied; each must still apply."""
    out = "\n".join(lines)
    for old, new in ADAPTED.get(module, []):
        assert old in out, f"{module}: adaptation no longer applies: {old!r}"
        out = out.replace(old, new)
    for block in REMOVED.get(module, []):
        assert block in out, f"{module}: removed block not found: {block!r}"
        out = out.replace(block, "")
    return out.split("\n")


@pytest.mark.skipif(POC is None, reason=f"the PoC repository (../poc with commit {COMMIT}) is not available")
@pytest.mark.parametrize("module", sorted(PORTED))
def test_module_is_the_pocs(module):
    poc = subprocess.run(["git", "show", f"{COMMIT}:{PORTED[module]}"], cwd=POC, env=ENV, capture_output=True,
                         text=True, check=True).stdout
    mine, theirs = code((SRC / module).read_text(), module), adapt(code(poc, module), module)
    assert mine == theirs, "\n".join(difflib.unified_diff(theirs, mine, "poc", "cli", lineterm="", n=1))
