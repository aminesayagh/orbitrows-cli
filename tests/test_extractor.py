from orbitrows.services.extractor.client import EXTRACTORS, Gliner2, connect


def test_default_is_gliner2():
    assert EXTRACTORS["gliner2"] is Gliner2  # registered, not loaded here


def test_a_class_kept_elsewhere_is_loaded_once_per_name():
    name = "orbitrows.services.extractor.client:Extractor"
    e = connect(name)
    assert type(e).__name__ == "Extractor" and connect(name) is e
