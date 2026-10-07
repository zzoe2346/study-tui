import asyncio
import copy
import hashlib

import pytest

from study_tui.demo import DEMO_LESSONS, DEMO_PLAN
from study_tui.service import StudyService
from study_tui.storage import Repository


class Generator:
    def __init__(self):
        self.plan_calls = 0
        self.lesson_calls = 0
        self.lesson = copy.deepcopy(DEMO_LESSONS[1])
        self.error = None
        self.started = asyncio.Event()
        self.block = False

    async def generate_plan(self, topic, feedback=None, previous=None):
        self.plan_calls += 1
        return copy.deepcopy(DEMO_PLAN)

    async def generate_lesson(self, plan, ordinal, prior_summary=""):
        self.lesson_calls += 1
        self.started.set()
        if self.block:
            await asyncio.Future()
        if self.error:
            raise self.error
        return copy.deepcopy(self.lesson)


class Renderer:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.lesson_saved_before_render = False

    async def render_visuals(self, lesson, material_dir):
        self.calls += 1
        self.lesson_saved_before_render = (material_dir / "lesson.json").exists()
        if self.fail:
            raise RuntimeError("All saved candidates unavailable")
        assets = []
        for visual in lesson["visuals"]:
            path = material_dir / f"{visual['id']}.txt"
            source = visual["source"] if visual["kind"] == "ascii" else visual["fallbacks"][-1]["source"]
            path.write_text(source, "utf-8")
            assets.append({"visual_id": visual["id"], "requested_kind": visual["kind"], "kind": "ascii",
                           "source_path": path.name, "rendered_path": path.name,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "ready",
                           "fallback_used": visual["kind"] != "ascii", "fallback_reason": "fixture renderer"})
        return assets


@pytest.fixture
def setup(tmp_path):
    repo = Repository(tmp_path / "data")
    generator, renderer = Generator(), Renderer()
    service = StudyService(repo, generator, renderer)
    course = service.save_draft(DEMO_PLAN)
    part = service.start_course(course)
    yield repo, generator, renderer, service, course, part
    repo.close()


async def test_ready_material_cache_does_not_call_cli_or_renderer_again(setup):
    repo, generator, renderer, service, course, part = setup
    material = await service.ensure_material(part)
    assert renderer.lesson_saved_before_render
    assert await service.ensure_material(part) == material
    assert generator.lesson_calls == renderer.calls == 1
    assert service.resume_course(course) == part


async def test_render_failure_preserves_previous_and_local_definition_retry_uses_no_ai(setup):
    repo, generator, renderer, service, _, part = setup
    renderer.fail = True
    with pytest.raises(RuntimeError):
        await service.ensure_material(part)
    assert repo.get_active_material(part) is None
    assert generator.lesson_calls == 1
    assert repo.list_jobs()[0]["status"] == "failed"
    renderer.fail = False
    recovered = await service.ensure_material(part)
    assert generator.lesson_calls == 1
    assert repo.valid_material(recovered["id"])
    renderer.fail = True
    service.save_progress(part, answers={"q1": "preserved"}, scroll_y=10)
    before = repo.get_progress(part)
    with pytest.raises(RuntimeError):
        await service.ensure_material(part, regenerate=True)
    assert repo.get_active_material(part)["id"] == recovered["id"]
    assert repo.get_progress(part) == before


async def test_subscription_error_never_retries_generator_or_erases_ready_material(setup):
    repo, generator, renderer, service, _, part = setup
    old = await service.ensure_material(part)
    generator.error = RuntimeError("quota exceeded")
    with pytest.raises(RuntimeError, match="quota"):
        await service.ensure_material(part, regenerate=True)
    assert generator.lesson_calls == 2
    assert renderer.calls == 1
    assert repo.get_active_material(part)["id"] == old["id"]


async def test_cancelled_generation_records_state_and_preserves_progress(setup):
    repo, generator, _, service, _, part = setup
    old = await service.ensure_material(part)
    before = repo.get_progress(part)
    generator.started.clear()
    generator.block = True
    task = asyncio.create_task(service.ensure_material(part, regenerate=True))
    await generator.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.get_active_material(part)["id"] == old["id"]
    assert repo.get_progress(part) == before
    assert repo.list_jobs()[0]["status"] == "cancelled"


async def test_explicit_followup_makes_new_draft_and_keeps_original_completed(setup):
    repo, generator, _, service, course, first = setup
    next_part = service.choose_next(first)
    service.choose_next(next_part)
    assert repo.get_course(course)["status"] == "completed"
    assert generator.plan_calls == 0
    child = await service.create_follow_up(course, "통계와 추정 행 수")
    assert child != course
    assert repo.get_course(child)["parent_course_id"] == course
    assert repo.get_course(child)["status"] == "draft"
    assert repo.get_course(course)["status"] == "completed"
    assert generator.plan_calls == 1


async def test_concurrent_same_part_requests_share_saved_material_after_first_finishes(setup):
    _, generator, renderer, service, _, part = setup
    first, second = await asyncio.gather(service.ensure_material(part), service.ensure_material(part))
    assert first["id"] == second["id"]
    assert generator.lesson_calls == renderer.calls == 1
