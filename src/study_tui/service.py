"""Finite study flow; generation is opt-in and never grades quiz notes."""
from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

from .models import validate_lesson, validate_plan
from .storage import Repository, StorageError, digest


class StudyService:
    def __init__(self, repo: Repository, generator, renderer):
        self.repo = repo
        self.generator = generator
        self.renderer = renderer
        self._generation_lock = asyncio.Lock()

    async def propose_plan(self, topic: str, feedback: str | None = None,
                           previous: dict | None = None) -> dict:
        topic = topic.strip()
        if not topic:
            raise StorageError("학습할 주제를 입력하세요.")
        if previous is not None:
            previous = validate_plan(previous)
        async with self._generation_lock:
            job = self.repo.begin_job(None, None, "plan")
            try:
                plan = validate_plan(await self.generator.generate_plan(topic, feedback=feedback, previous=previous))
                self.repo.finish_job(job, "succeeded")
                return plan
            except asyncio.CancelledError:
                self.repo.finish_job(job, "cancelled", error_category="cancelled")
                raise
            except Exception as exc:
                self.repo.finish_job(job, "failed", error_category=getattr(exc, "category", "invalid_content"))
                raise

    def save_draft(self, plan: dict, course_id: str | None = None,
                   parent_course_id: str | None = None) -> str:
        return self.repo.save_draft(plan, course_id, parent_course_id)

    def start_course(self, course_id: str) -> str:
        return self.repo.start_course(course_id)

    def _prior_summary(self, part: dict) -> str:
        if part["ordinal"] <= 1:
            return ""
        parts = self.repo.list_parts(part["course_id"])
        previous = next((p for p in parts if p["ordinal"] == part["ordinal"] - 1), None)
        material = self.repo.get_active_material(previous["id"]) if previous else None
        if material and self.repo.valid_material(material["id"]):
            lesson = self.repo.load_lesson(material["id"])
            return "\n\n".join(s["body_markdown"] for s in lesson["sections"] if s["kind"] == "summary")[:12000]
        return ""

    async def ensure_material(self, part_id: str, regenerate: bool = False) -> dict:
        async with self._generation_lock:
            part = self.repo.get_part(part_id)
            course = self.repo.get_course(part["course_id"])
            if course["status"] == "draft":
                raise StorageError("과정을 먼저 시작하세요.")
            existing = self.repo.get_active_material(part_id)
            # A ready lesson remains valid even if a later part was regenerated.
            # Reading a stored course must not silently spend subscription usage.
            if not regenerate and existing and self.repo.valid_material(existing["id"]):
                return existing
            plan = self.repo.get_plan(part["course_id"])
            prior_summary = self._prior_summary(part)
            search_mode = getattr(self.generator, "search_mode", "cached")
            prompt_version = str(getattr(self.generator, "prompt_version", "1"))
            input_hash = digest({"plan": plan, "part": part["outline"], "prior_summary": prior_summary,
                                 "prompt_version": prompt_version, "schema_version": 1,
                                 "search_mode": search_mode, "model": getattr(self.generator, "model", None)})
            job = self.repo.begin_job(part["course_id"], part_id, "lesson")
            material = None
            try:
                # Reuse only the complete, validated JSON saved before a prior render.
                # This path contains no extra AI call or new fallback generation.
                material = self.repo.find_definition(part_id, input_hash) if not regenerate else None
                if material:
                    lesson = self.repo.load_lesson(material["id"])
                else:
                    lesson = validate_lesson(await self.generator.generate_lesson(plan, part["ordinal"], prior_summary=prior_summary))
                    material = self.repo.stage_material(
                        part_id, lesson, input_hash, prompt_version=prompt_version,
                        cli_version=getattr(self.generator, "cli_version", None),
                        reported_model=getattr(self.generator, "reported_model", None),
                        search_mode=search_mode)
                material_dir = self.repo.resolve(str(PurePosixPath(material["lesson_path"]).parent))
                render = getattr(self.renderer, "render_visuals", self.renderer)
                assets = await render(lesson, material_dir)
                ready = self.repo.ready_material(material["id"], assets)
                self.repo.finish_job(job, "succeeded", material_id=ready["id"])
                return ready
            except asyncio.CancelledError:
                if material:
                    self.repo.fail_material(material["id"])
                self.repo.finish_job(job, "cancelled", error_category="cancelled")
                raise
            except Exception as exc:
                if material:
                    self.repo.fail_material(material["id"])
                category = getattr(exc, "category", "render_failed" if material else "generation_failed")
                self.repo.finish_job(job, "failed", error_category=category)
                raise

    # The name from the design is kept for callers that use its original interface.
    load_or_generate = ensure_material

    def save_progress(self, part_id: str, *, section_index: int | None = None,
                      scroll_y: float | None = None,
                      answers: dict[str, str] | None = None,
                      answers_revealed: bool | None = None) -> dict:
        return self.repo.save_progress(part_id, section_index=section_index, scroll_y=scroll_y, answers=answers,
                                       answers_revealed=answers_revealed)

    def reveal_answers(self, part_id: str) -> dict:
        return self.repo.save_progress(part_id, answers_revealed=True)

    def choose_next(self, part_id: str) -> str | None:
        return self.repo.choose_next(part_id)

    def resume_course(self, course_id: str) -> str | None:
        return self.repo.get_course(course_id)["current_part_id"]

    async def create_follow_up(self, course_id: str, topic: str) -> str:
        self.repo.get_course(course_id)
        plan = await self.propose_plan(topic)
        return self.save_draft(plan, parent_course_id=course_id)
