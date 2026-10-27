"""What the CLI runs beside its code, prepared once and loaded early.

    setup(say)  `orbitrows setup`: everything a message needs, each part checked and skipped when already there:
                Duckling (the date reader's service, services/duckling) and every model, loaded once by its own
                loader (a load downloads exactly what it reads, and proves it works): GLiNER2, the meaning model
                (values, the linker's too), the operations search model, spaCy. → the parts that aren't ready, with
                why ([] when all are)
    preload()   a session's start: the same models loaded in the background, so its first message doesn't wait on
                them (a message waits for the preload to end first: no model loaded twice)
"""
import threading

from orbitrows.services import duckling


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


def setup(say=print) -> list[str]:
    failed = []
    why = duckling.setup()
    say(f"Duckling (dates): {'ready' if not why else why}")
    failed += [f"Duckling (dates): {why}"] if why else []
    say("Models (GLiNER2, meanings, operations search, spaCy): loading, the first time downloads them …")
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
