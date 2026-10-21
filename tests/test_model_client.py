from orbitrows.services.model.client import MODELS, RETRIES, Jev, Model, ask, choice, connect, sure, yes_no


def test_jev_is_the_default_model():
    assert MODELS["jev"] is Jev  # registered, never instantiated: no real Jev call in tests


def test_connect_a_class_kept_elsewhere():
    assert type(connect("orbitrows.services.model.client:Model")).__name__ == "Model"


def test_sure():
    answers = {"a": ("x", {"x": 0.9, "y": 0.1}), "b": ("x", {"x": 0.5, "y": 0.5})}
    assert sure(answers, "a") == "x" and sure(answers, "b") is None and sure(answers, "missing") is None


def test_question_builders():
    assert choice("ctx", "task", ["a", "b"], rules=["r"], header="h")["instructions"] == \
        {"context": "ctx", "task": "task", "header": "h", "rules": ["r"]}
    assert yes_no("ctx", "task")["type"] == "noul"


class TimeoutError_(Exception):  # "timeout" in the class name is what ask() retries on
    pass


class Flaky(Model):
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    async def answer(self, state, questions):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError_()
        return {"q": 0.9}, (None, None)


async def test_ask_retries_a_timeout():
    model = Flaky(RETRIES)
    assert await ask(model, {}, {"q": yes_no("c", "t")}) == {"q": 0.9} and model.calls == RETRIES + 1


async def test_ask_gives_up_after_the_retries():
    model = Flaky(RETRIES + 1)
    try:
        await ask(model, {}, {"q": yes_no("c", "t")})
        raise AssertionError("no error raised")
    except TimeoutError_:
        assert model.calls == RETRIES + 1


async def test_ask_without_questions_does_not_call_the_model():
    model = Flaky(0)
    assert await ask(model, {}, {}) == {} and model.calls == 0
