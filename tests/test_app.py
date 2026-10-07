"""User workflows exercised through the running Textual app, without AI calls."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Select, Static, TextArea

from study_tui.app import StudyApp
from study_tui.codex import GenerationFailure
from study_tui.demo import DemoGenerator
from study_tui.storage import Repository


class StoredASCIIRenderer:
    """Select the already authored ASCII candidate; never generate a fallback."""

    async def render_visuals(self, lesson: dict, directory: Path) -> list[dict]:
        assets = []
        for visual in lesson["visuals"]:
            candidate = visual if visual["kind"] == "ascii" else visual["fallbacks"][-1]
            relative = f"visuals/{visual['id']}.txt"
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = candidate["source"].encode("utf-8")
            path.write_bytes(data)
            assets.append({"visual_id": visual["id"], "requested_kind": visual["kind"],
                           "kind": "ascii", "source_path": relative, "rendered_path": relative,
                           "sha256": hashlib.sha256(data).hexdigest(), "status": "ready",
                           "fallback_used": visual["kind"] != "ascii",
                           "fallback_reason": "QA: 원본 렌더러를 사용하지 않았습니다." if visual["kind"] != "ascii" else None})
        return assets


class UnusedPDFExporter:
    async def export(self, *args):
        raise AssertionError("This user flow must not request a PDF implicitly")


class CountingDemo(DemoGenerator):
    def __init__(self):
        self.lesson_calls = 0
        self.plan_calls = 0

    async def generate_plan(self, *args, **kwargs):
        self.plan_calls += 1
        return await super().generate_plan(*args, **kwargs)

    async def generate_lesson(self, *args, **kwargs):
        self.lesson_calls += 1
        return await super().generate_lesson(*args, **kwargs)


async def settle(app, pilot):
    async with asyncio.timeout(3):
        await pilot.pause()
        while app.busy:
            await pilot.pause(0.03)
    await pilot.pause()


async def click(app, pilot, selector):
    widget = app.query_one(selector)
    widget.scroll_visible(animate=False)
    await pilot.pause()
    assert await pilot.click(selector), f"Could not click {selector}"
    await settle(app, pilot)


async def start_demo(app, pilot):
    app.query_one("#topic", Input).value = "PostgreSQL B-tree 인덱스"
    await click(app, pilot, "#propose")
    assert app.view == "plan"
    await click(app, pilot, "#start")
    assert app.view == "study"


@pytest.mark.asyncio
async def test_edit_start_skip_quizzes_complete_review_and_explicit_followup(tmp_path):
    repo = Repository(tmp_path)
    generator = CountingDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(115, 42)) as pilot:
            app.query_one("#topic", Input).value = "직접 입력한 주제"
            await click(app, pilot, "#propose")
            course_id = app.course_id
            app.query_one("#title-editor", Input).value = "직접 수정한 과정 이름"
            app.query_one("#goals-editor", TextArea).load_text("키 탐색 비용을 설명한다\n쓰기 비용을 함께 비교한다")
            await click(app, pilot, "#save-plan")
            assert repo.get_plan(course_id)["title"] == "직접 수정한 과정 이름"
            await click(app, pilot, "#start")
            assert app.view == "study"
            first = app.part_id
            assert generator.lesson_calls == 1
            assert not repo.get_progress(first)["answers_revealed"]
            # The actual button advances without visiting, answering or revealing a quiz.
            await click(app, pilot, "#next-part")
            second = app.part_id
            assert second != first
            assert repo.get_progress(first)["status"] == "completed"
            assert repo.get_progress(first)["answers"] == {}
            assert not repo.get_progress(first)["answers_revealed"]
            assert generator.lesson_calls == 2
            await click(app, pilot, "#next-part")
            assert app.view == "completed"
            assert repo.get_course(course_id)["status"] == "completed"
            assert len(repo.list_courses()) == 1
            assert generator.plan_calls == 1
            await click(app, pilot, "#review")
            assert app.view == "study"
            assert repo.get_progress(second)["status"] == "completed"
            app.query_one("#section-picker", Select).value = len(app.lesson["sections"])
            await settle(app, pilot)
            await click(app, pilot, "#followup-0")
            assert app.view == "plan"
            assert app.course_id != course_id
            assert repo.get_course(app.course_id)["parent_course_id"] == course_id
            assert repo.get_course(course_id)["status"] == "completed"
            assert generator.plan_calls == 2
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_notes_reveal_and_reading_position_resume_without_generation(tmp_path):
    repo = Repository(tmp_path)
    generator = CountingDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(100, 28)) as pilot:
            await start_demo(app, pilot)
            course_id, part_id = app.course_id, app.part_id
            app.query_one("#section-picker", Select).value = len(app.lesson["sections"])
            await settle(app, pilot)
            app.query_one("#note-q1", TextArea).load_text("인덱스 탐색과 행 접근 비용을 함께 비교한다.")
            # Revealing answers must immediately persist the pending note.
            await click(app, pilot, "#reveal")
            progress = repo.get_progress(part_id)
            assert progress["answers"]["q1"] == "인덱스 탐색과 행 접근 비용을 함께 비교한다."
            assert progress["answers_revealed"] is True
            content = app.query_one("#content", VerticalScroll)
            content.scroll_to(y=8, animate=False, force=True)
            await pilot.pause(0.6)
            saved_scroll = repo.get_progress(part_id)["scroll_y"]
            assert saved_scroll > 0
            await pilot.press("ctrl+q")
        repo.close()
        repo = Repository(tmp_path)
        resumed = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
        async with resumed.run_test(size=(100, 28)) as pilot:
            await click(resumed, pilot, f"#course-{course_id}")
            assert resumed.part_id == part_id
            assert resumed.section_index == len(resumed.lesson["sections"])
            assert resumed.query_one("#note-q1", TextArea).text == "인덱스 탐색과 행 접근 비용을 함께 비교한다."
            assert repo.get_progress(part_id)["answers_revealed"] is True
            assert resumed.query_one("#content", VerticalScroll).scroll_y == saved_scroll
            assert generator.lesson_calls == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_regeneration_cancellation_keeps_material_and_notes_and_ui_responds(tmp_path):
    class BlockingDemo(CountingDemo):
        def __init__(self):
            super().__init__()
            self.block = False
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def generate_lesson(self, *args, **kwargs):
            if self.block:
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            return await super().generate_lesson(*args, **kwargs)

    repo = Repository(tmp_path)
    generator = BlockingDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(110, 38)) as pilot:
            await start_demo(app, pilot)
            part_id = app.part_id
            old_material = app.material["id"]
            app.query_one("#section-picker", Select).value = len(app.lesson["sections"])
            await settle(app, pilot)
            app.query_one("#note-q1", TextArea).load_text("취소해도 이 메모는 남는다.")
            generator.block = True
            button = app.query_one("#regenerate")
            button.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click("#regenerate")
            await asyncio.wait_for(generator.entered.wait(), 2)
            assert app.busy
            # The running worker does not block the app's cancellation binding.
            await pilot.press("ctrl+x")
            await settle(app, pilot)
            assert generator.cancelled.is_set()
            assert not app.busy
            assert app.view == "study"
            assert app.material["id"] == old_material
            assert repo.get_active_material(part_id)["id"] == old_material
            assert repo.get_progress(part_id)["answers"]["q1"] == "취소해도 이 메모는 남는다."
            assert "취소" in str(app.query_one("#status", Static).content)
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.view == "home"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_initial_generation_failure_can_retry_without_restarting_course(tmp_path):
    class FailingOnceDemo(CountingDemo):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def generate_lesson(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise GenerationFailure("network", "연결 실패 fixture: 직접 재시도하세요.")
            return await super().generate_lesson(*args, **kwargs)

    repo = Repository(tmp_path)
    generator = FailingOnceDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(110, 38)) as pilot:
            app.query_one("#topic", Input).value = "인덱스"
            await click(app, pilot, "#propose")
            course_id = app.course_id
            await click(app, pilot, "#start")
            assert app.view == "pending"
            assert repo.get_course(course_id)["status"] == "active"
            assert repo.get_active_material(app.part_id) is None
            await click(app, pilot, "#retry-part")
            assert app.view == "study"
            assert app.course_id == course_id
            assert generator.plan_calls == 1
            assert generator.lesson_calls == 1
            assert len(repo.list_courses()) == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_initial_generation_cancel_has_a_manual_retry_route(tmp_path):
    class BlockingFirstDemo(CountingDemo):
        def __init__(self):
            super().__init__()
            self.block = True
            self.entered = asyncio.Event()

        async def generate_lesson(self, *args, **kwargs):
            if self.block:
                self.entered.set()
                await asyncio.Event().wait()
            return await super().generate_lesson(*args, **kwargs)

    repo = Repository(tmp_path)
    generator = BlockingFirstDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(110, 38)) as pilot:
            app.query_one("#topic", Input).value = "인덱스"
            await click(app, pilot, "#propose")
            course_id = app.course_id
            app.query_one("#start").scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click("#start")
            await asyncio.wait_for(generator.entered.wait(), 2)
            await pilot.press("ctrl+x")
            await settle(app, pilot)
            assert app.view == "pending"
            assert repo.get_course(course_id)["status"] == "active"
            generator.block = False
            await click(app, pilot, "#retry-part")
            assert app.view == "study"
            assert app.course_id == course_id
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_80_by_24_terminal_controls_and_ascii_remain_accessible(tmp_path):
    repo = Repository(tmp_path)
    app = StudyApp(repo, CountingDemo(), StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await start_demo(app, pilot)
            await click(app, pilot, "#next-section")
            assert app.section_index == 1
            assert len(app.query(".diagram-text")) == 2
            text = app.query(".diagram-text").first(Static).content.plain
            assert text == app.lesson["visuals"][0]["fallbacks"][-1]["source"]
            app.query_one("#section-picker", Select).value = len(app.lesson["sections"])
            await settle(app, pilot)
            await click(app, pilot, "#reveal")
            assert repo.get_progress(app.part_id)["answers_revealed"]
            await click(app, pilot, "#next-part")
            assert app.view == "study"
            assert repo.get_part(app.part_id)["ordinal"] == 2
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_72_column_ascii_scrolls_horizontally_without_wrapping(tmp_path):
    wide_diagram = "[Root]" + "-" * 58 + "> [Rows]"

    class WideDiagramDemo(CountingDemo):
        async def generate_lesson(self, *args, **kwargs):
            lesson = await super().generate_lesson(*args, **kwargs)
            lesson["visuals"][0].update(kind="ascii", source=wide_diagram, fallbacks=[])
            return lesson

    repo = Repository(tmp_path)
    app = StudyApp(repo, WideDiagramDemo(), StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await start_demo(app, pilot)
            app.query_one("#section-picker", Select).value = 1
            await settle(app, pilot)
            diagram = app.query(".diagram").first(VerticalScroll)
            text = app.query(".diagram-text").first(Static)
            assert text.content.plain == wide_diagram
            assert text.region.width == 72
            assert text.region.height == 1
            assert diagram.max_scroll_x > 0
            diagram.scroll_to(x=diagram.max_scroll_x, animate=False, force=True)
            await pilot.pause()
            assert diagram.scroll_x == diagram.max_scroll_x
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_completed_course_reviews_earlier_part_and_remembers_it_without_ai(tmp_path):
    repo = Repository(tmp_path)
    generator = CountingDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(110, 38)) as pilot:
            await start_demo(app, pilot)
            course_id, first_id = app.course_id, app.part_id
            first_material_id = app.material["id"]
            await click(app, pilot, "#next-part")
            second_id = app.part_id
            await click(app, pilot, "#next-part")
            assert app.view == "completed"
            assert generator.lesson_calls == 2
            app.query_one("#part-picker", Select).value = first_id
            await settle(app, pilot)
            assert app.view == "study"
            assert app.part_id == first_id
            assert app.material["id"] == first_material_id
            assert generator.lesson_calls == 2
            assert repo.get_course(course_id)["status"] == "completed"
            assert repo.get_course(course_id)["current_part_id"] == first_id
            assert repo.get_progress(first_id)["status"] == "completed"
            assert repo.get_progress(second_id)["status"] == "completed"
            await pilot.press("ctrl+q")
        repo.close()
        repo = Repository(tmp_path)
        resumed = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
        async with resumed.run_test(size=(110, 38)) as pilot:
            await click(resumed, pilot, f"#course-{course_id}")
            assert resumed.view == "completed"
            await click(resumed, pilot, "#review")
            assert resumed.view == "study"
            assert resumed.part_id == first_id
            assert resumed.material["id"] == first_material_id
            assert generator.lesson_calls == 2
            assert repo.get_course(course_id)["status"] == "completed"
            assert repo.get_progress(first_id)["status"] == "completed"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_skipping_to_last_part_does_not_complete_unfinished_course(tmp_path):
    repo = Repository(tmp_path)
    generator = CountingDemo()
    app = StudyApp(repo, generator, StoredASCIIRenderer(), UnusedPDFExporter(), demo=True)
    try:
        async with app.run_test(size=(110, 38)) as pilot:
            await start_demo(app, pilot)
            course_id, first_id = app.course_id, app.part_id
            second_id = repo.list_parts(course_id)[1]["id"]
            app.query_one("#part-picker", Select).value = second_id
            await settle(app, pilot)
            assert app.part_id == second_id
            assert repo.get_progress(first_id)["status"] != "completed"
            await click(app, pilot, "#next-part")
            assert app.view == "study"
            assert app.part_id == first_id
            assert repo.get_course(course_id)["status"] == "active"
            assert repo.get_progress(second_id)["status"] == "completed"
            assert repo.get_progress(first_id)["status"] != "completed"
            await click(app, pilot, "#next-part")
            assert app.view == "completed"
            assert repo.get_course(course_id)["status"] == "completed"
            assert all(repo.get_progress(part["id"])["status"] == "completed"
                       for part in repo.list_parts(course_id))
            assert generator.lesson_calls == 2
            assert not repo.get_progress(first_id)["answers_revealed"]
            assert not repo.get_progress(second_id)["answers_revealed"]
    finally:
        repo.close()
