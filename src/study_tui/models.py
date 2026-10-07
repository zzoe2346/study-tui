"""Validate transport JSON before it becomes saved study material.

The public representation remains plain JSON: it can be passed to the CLI,
renderers and TUI without a second conversion or a competing content model.
Validation checks shape and references, not the truth of lesson claims.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .schemas import LESSON_SCHEMA, PLAN_SCHEMA


class ContentError(ValueError):
    """Generated or edited content does not meet the study contract."""

    category = "invalid_content"


def _validate(payload: dict, schema: dict) -> dict:
    if not isinstance(payload, dict):
        raise ContentError("JSON 객체가 필요합니다.")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ContentError("저장할 수 없는 JSON 값입니다.") from exc
    if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
        raise ContentError("자료가 8 MiB 제한을 넘습니다.")
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
                    key=lambda error: str(list(error.absolute_path)))
    if errors:
        error = errors[0]
        path = ".".join(map(str, error.absolute_path)) or "$"
        # Do not include arbitrary model output (possibly sensitive) in diagnostics.
        raise ContentError(f"{path}: {error.validator} 검증 실패")
    return copy.deepcopy(payload)


def validate_plan(payload: dict) -> dict:
    plan = _validate(payload, PLAN_SCHEMA)
    if [part["ordinal"] for part in plan["parts"]] != list(range(1, len(plan["parts"]) + 1)):
        raise ContentError("파트 순서는 1부터 연속이어야 합니다.")
    if len(plan["parts"]) > 30:
        raise ContentError("하나의 기본 과정은 30파트 이하여야 합니다.")
    if not plan["topic"].strip() or not plan["title"].strip():
        raise ContentError("주제와 제목을 입력하세요.")
    return plan


def _unique(items: list[dict], label: str) -> set[str]:
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ContentError(f"{label} ID가 중복되었습니다.")
    return set(ids)


def validate_lesson(payload: dict) -> dict:
    lesson = _validate(payload, LESSON_SCHEMA)
    kinds = {section["kind"] for section in lesson["sections"]}
    if not {"concept", "mechanism", "example", "summary"} <= kinds:
        raise ContentError("개념·원리·예시·요약을 모두 포함해야 합니다.")
    visual_ids = _unique(lesson["visuals"], "그림")
    source_ids = _unique(lesson["sources"], "출처")
    _unique(lesson["quizzes"], "문제")
    used_visuals: set[str] = set()
    used_sources: set[str] = set()
    for section in lesson["sections"]:
        if not set(section["visual_ids"]) <= visual_ids:
            raise ContentError("존재하지 않는 그림 참조가 있습니다.")
        if not set(section["source_ids"]) <= source_ids:
            raise ContentError("존재하지 않는 출처 참조가 있습니다.")
        used_visuals.update(section["visual_ids"])
        used_sources.update(section["source_ids"])
    if visual_ids != used_visuals:
        raise ContentError("모든 그림을 본문에서 참조하고 설명해야 합니다.")
    if not used_sources:
        raise ContentError("본문에 근거 출처를 연결해야 합니다.")
    for source in lesson["sources"]:
        parsed = urlsplit(source["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ContentError("출처 URL은 인증 정보 없는 HTTP(S) 주소여야 합니다.")
        # checked_at is untrusted model metadata, never a local verification claim.
    for visual in lesson["visuals"]:
        fallback = visual["fallbacks"]
        if visual["kind"] == "ascii":
            if fallback:
                raise ContentError("ASCII 원본에는 대체 후보를 넣지 않습니다.")
        elif (not fallback or fallback[-1]["kind"] != "ascii"
              or sum(item["kind"] == "ascii" for item in fallback) != 1):
            raise ContentError("각 그림의 마지막 대체 후보로 ASCII 하나가 필요합니다.")
    return lesson


def stable_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
