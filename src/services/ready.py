"""What the CLI runs beside its code, prepared once and loaded early.

    setup(say)  `orbitrows setup`: everything a message needs, each part checked and skipped when already there:
                Duckling (the date reader's service, services/duckling), CorPipe's own environment (README, "CorPipe
                setup": a venv, its repository, its requirements; its transformers version clashes with ours) and
                every model, loaded once by its own loader (a load downloads exactly what it reads, and proves it
                works): GLiNER2, the meaning model (values, the linker's too), the operations search model, spaCy,
                CorPipe's. → the parts that aren't ready, with why ([] when all are)
    preload()   a session's start: the same models loaded in the background, so its first message doesn't wait on
                them (a message waits for the preload to end first: no model loaded twice)
"""
import subprocess
import sys
import threading

from orbitrows.pipeline.prompt import coref
from orbitrows.services import duckling

CORPIPE_GIT = "https://github.com/ufal/crac2026-corpipe"


def corpipe(say) -> str | None:
    """CorPipe's environment, made when missing (minutes: its requirements are large); None when it is there."""
    if not coref.missing():
        return None
    venv, src = coref.ROOT / coref.PYTHON.parents[1], (coref.ROOT / coref.CORPIPE).parent
    say("CorPipe (references): making its environment, this takes a few minutes …")
    steps = [[sys.executable, "-m", "venv", str(venv)]] if not (coref.ROOT / coref.PYTHON).is_file() else []
    if not src.is_dir():
        steps.append(["git", "clone", "--depth", "1", CORPIPE_GIT, str(src)])
    steps.append([str(coref.ROOT / coref.PYTHON), "-m", "pip", "install", "-q", "-r", str(src / "requirements.txt")])
    for cmd in steps:
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode:
            return f"`{' '.join(cmd[:3])} …` failed: {done.stderr.strip()[-300:]}"
    return coref.missing()


def models() -> None:
    """Every model a message reads with, loaded (downloaded the first time)."""
    from orbitrows.pipeline.expressions.words import nlp
    from orbitrows.pipeline.operations import index
    from orbitrows.pipeline.prompt import values
    from orbitrows.services.extractor import client as extractor
    extractor.connect()
    values.encoder()
    index.model(index.CONFIGS[index.DEFAULT]["model_name"])
    nlp("en")
    if not coref.missing():                                   # CorPipe's model, in its own kept-loaded server
        coref.mentions(["Show the products."], name="warmup")


def setup(say=print) -> list[str]:
    failed = []
    for name, step in (("Duckling (dates)", duckling.setup), ("CorPipe (references)", lambda: corpipe(say))):
        why = step()
        say(f"{name}: {'ready' if not why else why}")
        failed += [f"{name}: {why}"] if why else []
    say("Models (GLiNER2, meanings, operations search, spaCy, CorPipe's): loading, the first time downloads them …")
    try:
        models()
        say("Models: ready")
    except Exception as e:                                    # a download cut, a disk full: said, never hidden
        failed.append(f"Models: {e!r}"[:300])
        say(failed[-1])
    return failed


_preload: list[threading.Thread] = []


def preload() -> None:
    def run():
        try:
            models()
        except Exception:                                     # the message's own load says what is wrong
            pass
    _preload.append(threading.Thread(target=run, name="preload", daemon=True))
    _preload[0].start()


def wait() -> None:
    """A message waits for the session's preload, if any, before it loads anything itself."""
    for t in _preload:
        t.join()
