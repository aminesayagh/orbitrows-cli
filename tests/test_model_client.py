import json

import pytest

from orbitrows.services.model.client import (FLOOR, MODELS, RETRIES, Answers, Jev, Model, Recorded, ask, call_id, choice,
                                             connect, options, settle, sure, yes_no)


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


def test_call_id_is_stable_and_ignores_key_order():
    assert call_id({"a": 1, "b": [2]}, {"q": yes_no("c", "t")}) == call_id({"b": [2], "a": 1}, {"q": yes_no("c", "t")})
    assert len(call_id({}, {})) == 16 and call_id({"a": 1}, {}) != call_id({"a": 2}, {})


async def test_ask_names_its_call():
    a = await ask(Flaky(0), {"s": 1}, {"q": yes_no("c", "t")})
    assert isinstance(a, Answers) and a.call == call_id({"s": 1}, {"q": yes_no("c", "t")})
    assert (await ask(Flaky(0), {}, {})).call is None


class Counting(Model):
    def __init__(self):
        self.opened = self.closed = self.calls = 0

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *exc):
        self.closed += 1

    async def answer(self, state, questions):
        self.calls += 1
        return {"c": ("x", {"x": 0.6, "y": 0.4}), "n": 0.5}, (1, 2)


async def test_recorded_asks_once_then_replays():
    inner, calls, saves = Counting(), {}, []
    q = {"c": choice("ctx", "t", ["x", "y"]), "n": yes_no("ctx", "t")}
    async with Recorded(inner, calls, saved=lambda: saves.append(1)) as m:
        first = await ask(m, {"s": 1}, q)
        again = await ask(m, {"s": 1}, q)
    assert calls == {first.call: {"c": ["x", {"x": 0.6, "y": 0.4}], "n": 0.5}} and json.loads(json.dumps(calls)) == calls
    assert dict(first) == dict(again) and (inner.opened, inner.calls, inner.closed, len(saves)) == (1, 1, 1, 1)


async def test_recorded_replay_never_opens_the_model():
    inner = Counting()
    calls = {call_id({}, {"n": yes_no("c", "t")}): {"n": 0.3}}
    async with Recorded(inner, calls) as m:
        assert await ask(m, {}, {"n": yes_no("c", "t")}) == {"n": 0.3}
    assert (inner.opened, inner.calls, inner.closed) == (0, 0, 0)


def test_settle_records_the_user_answer_as_certain():
    calls = {"c1": {"c": ["x", {"x": 0.6, "y": 0.4}], "n": 0.5}}
    settle(calls, "c1", "c", "y")
    settle(calls, "c1", "n", "no")
    assert calls == {"c1": {"c": ["y", {"y": 1.0}], "n": 0.0}} and sure(calls["c1"], "c") == "y"
    settle(calls, "c1", "n", "yes")
    assert calls["c1"]["n"] == 1.0
    for call, key in (("nope", "c"), ("c1", "nope")):
        with pytest.raises(KeyError):
            settle(calls, call, key, "x")


def test_options():
    probs = {"a": 0.05, "b": 0.3, "c": 0.12, "d": 0.2, "e": 0.11, "uncertain": 0.22}
    assert [o["value"] for o in options(probs)] == ["b", "d", "c", "e"]          # floor, likeliest first, at most 4
    assert options({"a": 0.95, "b": 0.04, "c": 0.01}) == [{"answer": "a", "value": "a", "probability": 0.95},
                                                          {"answer": "b", "value": "b", "probability": 0.04}]  # two
    assert options({"uncertain": 0.7, "a": 0.2, "b": 0.1}, labels={"a": "price"})[0] == \
        {"answer": "price", "value": "a", "probability": 0.2}
    probs = {"a": 0.5, "b": 0.3, "c": 0.1, "d": 0.0999}                      # "c" is kept by the floor rule alone
    assert FLOOR == 0.10 and [o["value"] for o in options(probs)] == ["a", "b", "c"]
