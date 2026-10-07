import copy

import pytest

from study_tui.demo import DEMO_LESSONS, DEMO_PLAN
from study_tui.models import ContentError, validate_lesson, validate_plan


def test_fixtures_meet_complete_learning_contract_and_validation_copies():
    plan = validate_plan(DEMO_PLAN)
    plan["parts"][0]["title"] = "changed"
    assert DEMO_PLAN["parts"][0]["title"] != "changed"
    for lesson in DEMO_LESSONS.values():
        assert validate_lesson(lesson) == lesson


def test_plan_rejects_noncontinuous_parts_and_unexpected_fields():
    plan = copy.deepcopy(DEMO_PLAN)
    plan["parts"][1]["ordinal"] = 3
    with pytest.raises(ContentError, match="연속"):
        validate_plan(plan)
    plan = copy.deepcopy(DEMO_PLAN)
    plan["score_required"] = 80
    with pytest.raises(ContentError, match="additionalProperties"):
        validate_plan(plan)


@pytest.mark.parametrize("mutation", [
    lambda x: x["sections"][1]["visual_ids"].append("v999"),
    lambda x: x["sections"][0]["source_ids"].append("s999"),
    lambda x: x["visuals"][1].update(id="v1"),
    lambda x: x["quizzes"][1].update(id="q1"),
    lambda x: x["sources"][1].update(id="s1"),
    lambda x: x["sections"][3].update(kind="concept"),
    lambda x: x["sections"][1].update(visual_ids=[]),
    lambda x: x["visuals"][0].update(fallbacks=[]),
    lambda x: x["sources"][0].update(url="file:///etc/passwd"),
    lambda x: x["sources"][0].update(url="https://secret@example.com/docs"),
])
def test_lesson_rejects_invalid_references_duplicates_contract_and_source_schemes(mutation):
    lesson = copy.deepcopy(DEMO_LESSONS[1])
    mutation(lesson)
    with pytest.raises(ContentError):
        validate_lesson(lesson)


def test_ascii_primary_has_no_fallback_and_nonascii_requires_last_ascii():
    lesson = copy.deepcopy(DEMO_LESSONS[2])
    lesson["visuals"][0]["fallbacks"] = [{"kind": "ascii", "source": "+---+"}]
    with pytest.raises(ContentError, match="ASCII 원본"):
        validate_lesson(lesson)
    lesson = copy.deepcopy(DEMO_LESSONS[1])
    lesson["visuals"][0]["fallbacks"] = [{"kind": "ascii", "source": "+---+"}, {"kind": "mermaid", "source": "flowchart TD\nA-->B"}]
    with pytest.raises(ContentError, match="마지막"):
        validate_lesson(lesson)


def test_checked_at_is_not_assumed_verified_and_never_filled_in_by_validation():
    assert validate_lesson(DEMO_LESSONS[1])["sources"][0]["checked_at"] is None

