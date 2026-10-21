import pytest

from orbitrows.services.extractor import client as extractor


class NoSpans(extractor.Extractor):
    """An extractor that finds nothing, so a test never loads GLiNER2 (seconds on CPU); headers fall back to the dictionary."""

    def extract_entities(self, text, labels, **options):
        return {"entities": {}}

    def classify_text(self, text, tasks, **options):
        return {task: {"label": None, "confidence": 0.0} for task in tasks}


@pytest.fixture
def no_gliner(monkeypatch):
    monkeypatch.setitem(extractor._loaded, "gliner2", NoSpans())
    monkeypatch.delenv("EXTRACTOR", raising=False)
