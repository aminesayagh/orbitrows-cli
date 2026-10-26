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
          if p.name not in ("pipeline.py", "asking.py", "source_route.py", "relations.py")} | {  # new, not PoC ports
    "pipeline/expressions/words.py": "v2/prompt_ast/operations.py",          # the formula's passes: their own module
    "pipeline/expressions/steps.py": "v2/prompt_ast/operations_jev.py",      # since 2026-10-26
    "pipeline/expressions/links.py": "v2/prompt_ast/operations_map.py",
    "sql/dml.py": "v2/ast/dml.py", "sql/sql.py": "v2/ast/sql.py", "units/parameters.py": "v2/units/parameters.py"}
GONE = {"fastino/gliner2-multi-v1",                          # split.py's model name: the loader is services/extractor now
        "coref_work",                                        # coref.py's work folder: a private temporary one per process now
        'SELECT DISTINCT "', '" FROM "', '" WHERE "', '" IS NOT NULL', '" IS NOT NULL LIMIT ',   # a read of the
        '" IS NOT NULL LIMIT 50', '" IS NOT NULL LIMIT 13', '" IS NOT NULL AND TRIM("',      # store's rows: the
        '") != \'\' LIMIT '}                                # pipeline reads the profile instead (2026-10-25)
FORMULA_KINDS = [   # the formula's kinds of a number, a text, a date, a letter case: catalog entries since 2026-10-25
    "The column becomes this text or this date ('set the status to draft', 'change the date to 2026-11-01').",
    "This text is added at the start of the column's text ('put NEW in front of the names').",
    "This text is added at the end of the column's text ('add (old) at the end of the names').",
    "This text replaces another one inside the column's text ('replace cotton with organic cotton': organic cotton), also when the replaced text is said only once ('change Orbit to ORBIT in the names, but to Orbit™ for the active ones': ORBIT and Orbit™ both replace Orbit).",
    "This text is removed from the column's text ('remove the word Kids').",
    "The date is moved this many days, weeks, months or years LATER ('7 days later', 'push back a month').",
    "The date is moved this many days, weeks, months or years EARLIER ('bring it forward 2 weeks').",
    "All in capitals ('write the brands in capitals', 'uppercase the SKUs').",
    "All in lower case ('put the colours in lower case').",
    "Only the first letter of the text in capital ('capitalise the names').", "No change of letter case.",
    "The value is raised by this amount or percentage ('raise by 15%', 'add 10', 'an extra 2').",
    "The value is lowered by this amount or percentage ('take 5 off', 'a 20% discount', 'cut by 3').",
    "The value becomes this number ('set the price to 50', 'make it 8').",
    "The value is multiplied by this factor ('twice', 'halve', 'triple', 'a third of it').",
    "The value is divided by this number.",
    "This percentage or fraction OF a column is added to the value ('add 10% of the cost to the price', 'raise the price by 15% of the cost').",
    "This percentage or fraction OF a column is taken off the value ('lower the price by 10% of the cost').",
    "The value is rounded to this number of decimals.",
    "The value is rounded to the nearest multiple of this number ('to the nearest 5').",
    "The final value must never go above this number ('never above 300', 'capped at 12').",
    "The final value must never go below this number ('never below 20', 'at least 3').",
]
PARTS_TEMPLATES = [   # and its outputs' fixed list (the catalog's outputs since 2026-10-25)
    "outputs",   # one use fewer: the pending (computed) outputs are left out of the check, 2026-10-26
    "value", "count", "count_distinct", "sum", "avg", "min", "max", "Its values, as they are.",
    "How many values (count).", "How many different values.", "Their total (sum).", "Their average.",
    "The smallest (minimum).", "The largest (maximum).", "aggregate", "argument",
    "set_value", "add", "subtract", "add_percent", "subtract_percent", "double", "halve", "column", "column_percent",
    "round", "prefix", "suffix", "replace", "empty",
    "Set to a value written in the prompt ('set the price to 50', 'mark them as draft').",
    "Increase by an amount ('raise by 10', 'add 3').", "Decrease by an amount ('lower by 10').",
    "Increase by a percentage ('raise by 5%').",
    "Decrease by a percentage ('lower by 5%', '20% off', 'cut by a third' is not this).",
    "Double it (multiply by 2).", "Halve it (divide by 2).",
    "Set to another column's value, possibly plus or minus an amount ('to the cost plus 10').",
    "Set to another column's value plus a percentage ('to the cost plus 30%').",
    "Round it (to a number of decimals if one is written).", "Add a text at the start.", "Add a text at the end.",
    "Replace a text inside it by another text.", "Empty it: no value (clear, remove the value).", "none",
    "at_least_number", "at_least_column", "at_most_number", "at_most_column", "No limit on the new value.",
    "The new value must stay at or above a number ('never below 20', 'at least 5').",
    "The new value must stay at or above another column, possibly plus an amount ('never below the cost', 'at least the cost plus 10').",
    "The new value must stay at or below a number ('never above 500', 'no more than 9').",
    "The new value must stay at or below another column, possibly plus an amount ('never above the old price').",
    "TEXT", "INTEGER", "type", "operator", "left", "right", "arithmetic", "arguments", "function", "coalesce",
    "greatest", "least", 0.5, "at_least", "at_most", "flagged", "Does ", "request", "problem", "_column", "parts",
    "concat", "_guard", " set a limit on the new value of ", "_bound_number", "In ",
    ", which number is the limit on the new value of ",
    ", or is added to the limit's column ('never below the cost plus 10': 10)?", "_bound_column",
    ", which column is the limit on the new value of ", " based on ('never below the cost': cost)?", "limit of ",
    "how", ": a part is missing", "limit column of ", "change of ", "limit number of ",
]
REPLACED = {"pipeline/prompt/grouping.py": {   # the one tree question over every candidate tree (mixed ones for three
    "all", "any", "_all", "_any", "|tree", "How do the conditions of ", " combine?", "tree", "AND", "OR",  # conditions
    "how the conditions combine: no answer, AND kept"},  # only): one AND / OR question per pair now, any size
    "pipeline/prompt/operation.py": {   # "asks for information back?": never read; a change reports what SQLite's
    "Answer yes when the merchant asks to see or get something back: a list, a count, a total, the new values, a file.",
    "A request that only reads always gives information back: yes.",         # execution reports (2026-10-25)
    "A condition or a computation the request needs to make its change is not information back: no.",
    "_back", "') ask to get information back?", "reports_back", "reports back uncertain",
    "Does request ", "id", "text", "request", "problem", "probability", 0.0, 0.5},   # one use fewer of each
    # 2026-10-25: no row is read before the session runs the program (the test runs and counts are gone)
    "pipeline/prompt/assemble.py": {"where",   # 2026-10-26: lift's chain is written by relations.chained
                                    "select", "update", "delete", "SAVEPOINT assemble", "ROLLBACK TO assemble",
                                    "RELEASE assemble", "request", "ast", "rows", "predicates", "changes no record",
                                    "returns", ": each condition matches records, but not together"},
    "pipeline/prompt/conditions.py": {"rows", "no record matches the condition on "},
    # 2026-10-25: step 5's templates and their limits are the operations catalog's changes now (a number's change
    # is the formula's)
    "pipeline/prompt/parts.py": {"SQLite, on all rows", *PARTS_TEMPLATES},
    "sql/sql.py": {"SAVEPOINT ast_check", "ROLLBACK TO ast_check", "RELEASE ast_check"},
    # 2026-10-25: each formula step's SQL is written by the operations catalog's builder (one SQL per operation)
    "pipeline/expressions/steps.py": set(FORMULA_KINDS),
    "pipeline/expressions/links.py": {"type", "value", "operator", "left", "right", "arithmetic", "name",
                                          "arguments", "function", "concat", "round", "upper", "parts", "date",
                                          "least", "greatest", "coalesce", "substr"}}
MOVED = {"pipeline/prompt/formula.py": ["pipeline/expressions/__init__.py"],
         "pipeline/prompt/conditions.py": ["pipeline/prompt/relations.py"],   # the links' resolver (2026-10-26)
         "pipeline/prompt/assemble.py": ["pipeline/prompt/relations.py"]}   # code moved as it was: its texts are
# looked for there too (2026-10-26: compose(), the branch's positive condition and the A47 repair)
TRIMMED = {"pipeline/expressions/words.py"}   # the PoC's option A (word rules), never called, removed 2026-10-25: its
# kept definitions (the formula's helpers) are compared, each as the PoC wrote it


def kept(text: str, mine: str) -> str:
    """The PoC module with only the top-level definitions the CLI's still has."""
    names = {getattr(n, "name", None) or next((t.id for t in getattr(n, "targets", []) if isinstance(t, ast.Name)), None)
             for n in ast.parse(mine).body}
    tree = ast.parse(text)
    tree.body = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) or (
        getattr(n, "name", None) or next((t.id for t in getattr(n, "targets", []) if isinstance(t, ast.Name)), None)
    ) in names]
    return ast.unparse(tree)


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
        mine = "\n".join((SRC / m).read_text() for m in [module, *MOVED.get(module, [])])
        if missing := [v for v in tuned(kept(theirs, (SRC / module).read_text()) if module in TRIMMED else theirs)
                       - tuned(mine) if v not in GONE | REPLACED.get(module, set())]:
            lost[module] = missing
    assert not lost, lost
