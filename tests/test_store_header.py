from pathlib import Path

from orbitrows.pipeline.store.header import decide, detect
from orbitrows.services.model.client import Model

CASES = Path(__file__).parent / "fixtures" / "cases"


class Stub(Model):
    def __init__(self, p):
        self.p, self.states = p, []

    async def answer(self, state, questions):
        self.states.append(state)
        return {"header": self.p}, (None, None)


def test_decide():
    assert decide(0.95) == {"has_header": True, "probability": 0.95}
    assert decide(0.8)["has_header"] and "warning" not in decide(0.8)
    assert decide(0.05) == {"has_header": False, "probability": 0.05}
    assert decide(0.2)["has_header"] is False and "warning" not in decide(0.2)
    assert decide(0.5)["has_header"] and "warning" in decide(0.5) and "warning" in decide(0.3)


async def test_detect_asks_one_question_about_the_first_rows():
    jev = Stub(0.95)
    out = await detect(jev, CASES / "store" / "store.csv")
    assert out == {"has_header": True, "probability": 0.95}
    state = jev.states[0]
    assert state["file"] == "store.csv" and len(state["first_rows"]) == 5


async def test_detect_uncertain_keeps_the_header_flagged():
    out = await detect(Stub(0.5), CASES / "store" / "store.csv")
    assert out["has_header"] and out["warning"] == "header row uncertain: first row kept as header"
    assert out["question"]["type"] == "yes_no" and out["question"]["key"] == "header" and out["question"]["call"]


async def test_detect_sure_asks_the_user_nothing():
    assert "question" not in await detect(Stub(0.9), CASES / "store" / "store.csv")


async def test_detect_no_header():
    assert (await detect(Stub(0.05), CASES / "noheader" / "inventaire.csv"))["has_header"] is False
