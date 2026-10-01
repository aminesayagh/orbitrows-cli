from types import SimpleNamespace

from orbitrows.pipeline.context import classify_columns


class FakeJev:
    """Answers system_one from a dict: question name -> choice label or noul probability. Unlisted nouls = 0."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    async def system_one(self, state, questions):
        self.requests.append(sorted(questions))
        choices, nouls = {}, {}
        for name, q in questions.items():
            if q.type == "choice":
                label, p = self.answers.get(name, ("string" if name.endswith("_type") else "none", 0.9))
                choices[name] = SimpleNamespace(choice=label, probabilities={label: p})
            else:
                nouls[name] = SimpleNamespace(noul=self.answers.get(name, 0.0))
        return SimpleNamespace(choices=choices, nouls=nouls)


HEADER = ["SKU", "Prix", "Prix promo", "Devise", "Devise promo", "Poids (kg)", "Remarque", "Weight unit (kg)", "Weight"]
ROWS = [["A-1", "19.90", "15.90", "EUR", "EUR", "0.4", "", "kg", "2"]]


async def test_context_roles_refs_and_conflicts():
    jev = FakeJev({
        "c4": ("dedicated_context", 0.97), "c5": ("dedicated_context", 0.88), "c6": ("header_context", 0.95),
        "c7": ("uncertain", 0.51), "c8": ("both", 0.9),
        "c4_category": ("currency", 0.99), "c5_category": ("currency", 0.95), "c8_category": ("physical_unit", 0.9),
        "c4_describes_c2": 0.93, "c4_describes_c3": 0.81, "c5_describes_c3": 0.64, "c5_describes_c2": 0.2,
        "c8_describes_c9": 0.9, "c2_type": ("number", 0.95),
    })
    context, conflicts = await classify_columns(jev, HEADER, ROWS)

    assert len(jev.requests) == 2
    assert "c2_type" in jev.requests[0] and "c2" in jev.requests[0]  # type and role in the same request
    assert context["c2"].type == "number" and context["c1"].type == "string"
    assert not any("describes_c6" in q for q in jev.requests[1])  # declares its own unit: not a candidate
    assert not any("describes_c4" in q for q in jev.requests[1])  # a context column is not a candidate

    assert context["c2"].client == "c4" and context["c9"].client == "c8"
    assert context["c4"].category == "currency" and context["c4"].confidence == 0.97
    assert (context["c6"].category, context["c6"].header_context) == ("physical_unit", "kg")  # from the dictionary
    assert (context["c8"].category, context["c8"].header_context) == ("physical_unit", "kg")  # both: Jev + dictionary
    assert context["c7"].role == "uncertain" and context["c7"].category is None  # flagged later, at merge
    assert context["c1"].client is None

    [conflict] = conflicts  # Prix promo is claimed by both currency columns
    assert conflict.key == "c3" and conflict.options == [("c4", 0.81), ("c5", 0.64)]
    assert context["c3"].client is None  # left for the user


async def test_no_context_column_means_one_request():
    jev = FakeJev({"c2": ("header_context", 0.9), "c3": ("header_context", 0.8)})
    context, conflicts = await classify_columns(jev, ["SKU", "Prix (€)", "Remise (%)"], [["A-1", "9.90", "10"]])
    assert len(jev.requests) == 1 and conflicts == []
    assert (context["c2"].category, context["c2"].header_context) == ("currency", "EUR")
    assert (context["c3"].category, context["c3"].header_context) == ("percentage", "%")


async def test_other_category_and_unresolved_header():
    jev = FakeJev({"c1": ("dedicated_context", 0.9), "c1_category": ("other", 0.6), "c2": ("header_context", 0.7)})
    context, _ = await classify_columns(jev, ["Taux", "Prix TTC", "Stock"], [["x", "1", "2"]])
    assert context["c1"].category == "other"
    assert context["c2"].header_context is None  # header didn't resolve: stays a candidate, flagged at merge
    assert "c1_describes_c2" in jev.requests[1]
