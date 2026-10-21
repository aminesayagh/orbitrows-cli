"""The span extractor behind every step: labelled spans and text labels. GLiNER2 (local) by default; EXTRACTOR in
the env picks another: a name in EXTRACTORS, or "package.module:Class" for an Extractor subclass kept outside this file."""
import importlib
import os

# multilingual; gliner2.5-multi-v1 only returned short nouns in the split (0/12) and 77% vs 92% on the units benchmark
GLINER2 = "fastino/gliner2-multi-v1"


class Extractor:
    """What a step asks for spans. A subclass takes GLiNER2's calls and options and returns GLiNER2's formats:
        extract_entities(text, {label: description or {"description", "threshold"}}, threshold=, include_confidence=,
                         include_spans=, overlap_policy=)  → {"entities": {label: [{"text", "start", "end", "confidence"}]}}
        classify_text(text, {task: [labels] or {"labels", "multi_label"}}, threshold=, include_confidence=)
                                                          → {task: {"label", "confidence"}}"""
    name = "extractor"

    def extract_entities(self, text, labels, **options) -> dict:
        raise NotImplementedError

    def classify_text(self, text, tasks, **options) -> dict:
        raise NotImplementedError


class Gliner2(Extractor):
    name = "gliner2"

    def __init__(self, checkpoint=GLINER2):
        """Loaded once per process by connect() (seconds on CPU; 1.2 GB, downloaded on the very first run)."""
        from gliner2 import AutoExtractor
        self.model = AutoExtractor.from_pretrained(checkpoint)

    def extract_entities(self, text, labels, **options):
        return self.model.extract_entities(text, labels, **options)

    def classify_text(self, text, tasks, **options):
        return self.model.classify_text(text, tasks, **options)


EXTRACTORS = {"gliner2": Gliner2}
_loaded = {}


def connect(name=None) -> Extractor:
    """The extractor named by `name`, else EXTRACTOR from the env, else GLiNER2; one instance per name."""
    name = name or os.environ.get("EXTRACTOR", "gliner2")
    if name not in _loaded:
        if ":" in name:
            module, cls = name.split(":")
            _loaded[name] = getattr(importlib.import_module(module), cls)()
        else:
            _loaded[name] = EXTRACTORS[name]()
    return _loaded[name]

