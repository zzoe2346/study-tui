"""Textual course editor and reader; quiz notes never gate progression."""
from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path, PurePosixPath
import subprocess
import sys

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Markdown, Select, Static, TextArea
from rich.text import Text

from .models import validate_plan
from .storage import Repository, StorageError
from .service import StudyService
from .rendering import VisualRenderError, PDFRenderError


def safe_markdown(value: str) -> str:
    # Terminal reader never fetches a remote Markdown image.
    return re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[그림: \1]", value)


def open_local(path: Path) -> None:
    command = ["open", str(path)] if sys.platform == "darwin" else (
        ["cmd", "/c", "start", "", str(path)] if sys.platform == "win32" else ["xdg-open", str(path)])
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class StudyApp(App):
    TITLE = "study-tui"
    SUB_TITLE = "주제 하나, 끝이 있는 학습"
    BINDINGS = [("ctrl+q", "quit", "종료"), ("escape", "home", "과정 목록"),
                ("ctrl+s", "save", "저장"), ("ctrl+x", "cancel", "생성 취소")]
    CSS = """
    Screen { background: $background; }
    #status { height: auto; min-height: 2; padding: 0 2; color: $text-muted; background: $panel; }
    #content { padding: 1 3; }
    .headline { text-style: bold; color: $accent; margin-bottom: 1; height: auto; }
    .hint { color: $text-muted; height: auto; margin: 0 0 1 0; }
    Input { margin-bottom: 1; }
    TextArea { height: 7; margin-bottom: 1; }
    #scope-editor { height: 4; }
    #parts-editor { height: 7; }
    .actions { height: auto; min-height: 3; layout: horizontal; margin: 1 0; }
    Button { margin-right: 1; }
    .course { width: 100%; margin-bottom: 1; text-align: left; }
    .diagram { height: auto; min-height: 4; overflow-x: auto; overflow-y: auto; padding: 1; border: solid $primary; }
    .diagram-text { width: auto; height: auto; }
    .caption { color: $text-muted; height: auto; margin: 1 0; }
    Markdown { padding: 0; margin: 1 0; }
    #section-picker { margin-bottom: 1; }
    .followup { width: 100%; height: auto; min-height: 3; margin-bottom: 1; }
    """

    def __init__(self, repo: Repository, generator, renderer, pdf_exporter, *, demo: bool = False):
        super().__init__()
        self.repo = repo
        self.generator = generator
        self.renderer = renderer
        self.pdf_exporter = pdf_exporter
        self.service = StudyService(repo, generator, renderer)
        self.demo = demo
        self.view = "home"
        self.course_id: str | None = None
        self.part_id: str | None = None
        self.material: dict | None = None
        self.lesson: dict | None = None
        self.plan: dict | None = None
        self.section_index = 0
        self.busy = False
        self._worker = None
        self._rendering = False
        self._notes_timer = None
        self._last_scroll = None
        self._restoring_scroll = False
        self._followups: list[dict] = []
        if hasattr(generator, "on_event"):
            generator.on_event = self.set_status

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("준비", id="status", markup=False)
        yield VerticalScroll(id="content")
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(0.5, self.save_scroll)
        await self.show_home()

    def set_status(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#status", Static).update(message)

    async def replace_content(self, *widgets) -> None:
        self._rendering = True
        content = self.query_one("#content", VerticalScroll)
        await content.remove_children()
        await content.mount(*widgets)
        content.scroll_home(animate=False)
        self._rendering = False

    def start_job(self, coro, label: str) -> None:
        if self.busy:
            coro.close()
            self.set_status("현재 작업을 마치거나 Ctrl+X로 취소하세요.")
            return
        self.busy = True
        self.set_status(f"{label} · Ctrl+X로 취소할 수 있습니다")
        self._worker = self.run_worker(self.job(coro), group="generation", exclusive=True, exit_on_error=False)

    async def job(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            self.set_status("작업을 취소했습니다. 저장된 교재와 메모를 유지했습니다.")
            raise
        except Exception as exc:
            # Domain failures already carry safe user messages. Unknown failures expose no raw CLI output.
            if isinstance(exc, (StorageError, ValueError, VisualRenderError, PDFRenderError)) or hasattr(exc, "category"):
                message = str(exc)
            else:
                message = f"작업을 완료하지 못했습니다 ({type(exc).__name__}). 저장된 교재를 유지했습니다."
            self.set_status(message)
            self.notify(message, severity="error", timeout=8)
        finally:
            self.busy = False

    async def show_home(self) -> None:
        self.flush_notes()
        self.save_scroll()
        self.view = "home"
        self.part_id = None
        self.material = None
        self.lesson = None
        widgets = [Label("어떤 주제를 공부할까요?", classes="headline"),
            Static("데이터베이스 인덱스, 트랜잭션 격리, 시스템 설계… 원하는 주제를 직접 입력하세요.", classes="hint"),
            Input(placeholder="예: PostgreSQL B-tree 인덱스의 동작과 비용", id="topic"),
            Button("학습 계획 만들기", id="propose", variant="primary")]
        if self.demo:
            widgets.insert(1, Static("DEMO · 고정된 DB 인덱스 예제로 체험합니다. AI 호출은 없습니다.", classes="hint"))
        widgets.append(Label("저장된 과정", classes="headline"))
        courses = self.repo.list_courses()
        for course in courses:
            status = {"draft":"계획 수정 중","active":"학습 중","completed":"완료"}[course["status"]]
            widgets.append(Button(f"{course['title']}  ·  {status}", id=f"course-{course['id']}", classes="course"))
        if not courses:
            widgets.append(Static("아직 저장된 과정이 없습니다.", classes="hint"))
        await self.replace_content(*widgets)
        self.set_status("메모와 읽던 위치는 자동 저장됩니다." + (" · DEMO" if self.demo else ""))
        self.query_one("#topic", Input).focus()

    def read_plan_editor(self) -> dict:
        plan = copy.deepcopy(self.plan)
        plan["title"] = self.query_one("#title-editor", Input).value.strip()
        plan["objectives"] = [line.strip() for line in self.query_one("#goals-editor", TextArea).text.splitlines() if line.strip()]
        plan["scope"] = self.query_one("#scope-editor", TextArea).text.strip()
        parts = []
        for line in self.query_one("#parts-editor", TextArea).text.splitlines():
            if not line.strip():
                continue
            values = line.split("|", 2)
            if len(values) != 3:
                raise ValueError("파트는 ‘분 | 제목 | 목표; 목표’ 형식으로 한 줄씩 입력하세요.")
            parts.append({"ordinal":len(parts)+1,"minutes":int(values[0].strip()),
                "title":values[1].strip(),"objectives":[x.strip() for x in values[2].split(";") if x.strip()]})
        plan["parts"] = parts
        return validate_plan(plan)

    async def show_plan(self, plan: dict, course_id: str | None = None) -> None:
        self.view = "plan"
        self.plan = plan
        self.course_id = course_id
        await self.replace_content(Label("계획을 읽고 원하는 만큼 수정하세요", classes="headline"),
            Label("과정 이름"), Input(plan["title"], id="title-editor"),
            Label("목표 · 한 줄에 하나"), TextArea("\n".join(plan["objectives"]), id="goals-editor"),
            Label("학습 범위"), TextArea(plan["scope"], id="scope-editor"),
            Label("파트 · 분 | 제목 | 목표; 목표"),
            TextArea("\n".join(f"{p['minutes']} | {p['title']} | {'; '.join(p['objectives'])}" for p in plan["parts"]), id="parts-editor"),
            Static("각 파트 30–45분. 목표와 파트 수를 직접 바꿀 수 있습니다.", classes="hint"),
            Input(placeholder="AI에게 짧게 조정 요청: 예) 쓰기 비용 예제를 더 자세히", id="adjustment"),
            Horizontal(Button("AI로 조정", id="adjust"), Button("계획 저장", id="save-plan"),
                Button("이 계획으로 시작", id="start", variant="primary"), classes="actions"))
        self.set_status("계획을 시작하면 파트 구성은 고정됩니다. 새 주제는 별도 과정으로 저장됩니다.")

    async def propose(self) -> None:
        topic = self.query_one("#topic", Input).value
        plan = await self.service.propose_plan(topic)
        course_id = self.service.save_draft(plan)
        await self.show_plan(plan, course_id)

    async def adjust(self) -> None:
        previous = self.read_plan_editor()
        feedback = self.query_one("#adjustment", Input).value.strip()
        if not feedback:
            raise ValueError("조정할 내용을 입력하세요.")
        plan = await self.service.propose_plan(previous["topic"], feedback=feedback, previous=previous)
        self.course_id = self.service.save_draft(plan, self.course_id)
        await self.show_plan(plan, self.course_id)

    def save_plan(self) -> None:
        self.plan = self.read_plan_editor()
        self.course_id = self.service.save_draft(self.plan, self.course_id)
        self.set_status("계획을 저장했습니다.")

    async def start_course(self) -> None:
        self.save_plan()
        part_id = self.service.start_course(self.course_id)
        await self.load_part(part_id)

    async def load_part(self, part_id: str, *, regenerate: bool = False) -> None:
        self.flush_notes()
        self.save_scroll()
        try:
            material = await self.service.ensure_material(part_id, regenerate=regenerate)
        except (Exception, asyncio.CancelledError):
            if self.repo.get_active_material(part_id) is None:
                self.part_id = part_id
                self.course_id = self.repo.get_part(part_id)["course_id"]
                self.view = "pending"
                await self.replace_content(Label("교재가 아직 완성되지 않았습니다", classes="headline"),
                    Static("현재 과정은 저장되어 있습니다. 연결이나 생성 상태를 확인하고 직접 다시 시도하세요.", classes="hint"),
                    Button("이 파트 다시 시도",id="retry-part",variant="primary"), Button("과정 목록",id="home"))
            raise
        self.part_id = part_id
        self.course_id = self.repo.get_part(part_id)["course_id"]
        self.material = material
        self.lesson = self.repo.load_lesson(material["id"])
        self.repo.select_part(part_id)
        self.section_index = self.repo.get_progress(part_id)["section_index"]
        await self.show_study()

    async def show_study(self) -> None:
        self.view = "study"
        lesson = self.lesson
        progress = self.repo.get_progress(self.part_id)
        self.section_index = min(self.section_index, len(lesson["sections"]))
        part = self.repo.get_part(self.part_id)
        count = len(self.repo.list_parts(self.course_id))
        options = [(f"{i+1}. {section['title']}",i) for i,section in enumerate(lesson["sections"])]
        options.append(("퀴즈 · 정답 / 후속 주제",len(lesson["sections"])))
        widgets = [Label(f"{lesson['title']} · {part['ordinal']}/{count} · {lesson['minutes']}분", classes="headline"),
            self.part_picker(),
            Select(options,value=self.section_index,allow_blank=False,id="section-picker")]
        if self.demo:
            widgets.append(Static("DEMO · 고정 예제 교재", classes="hint"))
        if self.section_index < len(lesson["sections"]):
            section = lesson["sections"][self.section_index]
            widgets.append(Markdown(safe_markdown(section["body_markdown"])))
            assets = {a["visual_id"]:a for a in self.material["assets"]}
            visuals = {v["id"]:v for v in lesson["visuals"]}
            for visual_id in section["visual_ids"]:
                visual,asset = visuals[visual_id],assets[visual_id]
                if asset["kind"] == "ascii":
                    text = self.repo.resolve(asset["rendered_path"]).read_text(encoding="utf-8")
                    widgets.append(VerticalScroll(Static(Text(text),classes="diagram-text"),classes="diagram"))
                else:
                    widgets.append(Button(f"그림 열기 · {visual_id}",id=f"asset-{visual_id}"))
                widgets.append(Static(visual["caption"]+"\n"+visual["alt_text"],classes="caption",markup=False))
                if asset["fallback_used"]:
                    widgets.append(Static(f"대체 그림 사용 · {asset['fallback_reason']}",classes="hint",markup=False))
                    widgets.append(Button("원래 그림 다시 시도",id=f"retry-{visual_id}"))
            sources = {s["id"]:s for s in lesson["sources"]}
            for source_id in section["source_ids"]:
                source = sources[source_id]
                widgets.append(Markdown(f"출처: [{source['title']}]({source['url']})"))
        else:
            widgets.extend([Label("간단한 퀴즈",classes="headline"),
                Static("메모하거나 종이에 풀어도 좋습니다. 답을 쓰지 않아도 다음 파트로 갈 수 있습니다.",classes="hint")])
            for quiz in lesson["quizzes"]:
                widgets.append(Markdown(f"**{quiz['id']}.** {safe_markdown(quiz['question'])}"))
                widgets.append(TextArea(progress["answers"].get(quiz["id"],""),id=f"note-{quiz['id']}"))
            widgets.append(Button("정답과 해설 보기",id="reveal",variant="primary"))
            if progress["answers_revealed"]:
                widgets.append(Label("정답과 해설",classes="headline"))
                for quiz in lesson["quizzes"]:
                    widgets.append(Markdown(f"**{quiz['id']} · {safe_markdown(quiz['answer'])}**\n\n{safe_markdown(quiz['explanation'])}"))
            widgets.extend(self.followup_widgets(lesson["follow_ups"]))
        widgets.append(Horizontal(Button("이전 내용",id="previous-section"),Button("다음 내용",id="next-section"), classes="actions"))
        widgets.append(Horizontal(Button("파트 PDF",id="pdf"), Button("교재 다시 생성",id="regenerate"),
            Button("다음 파트 / 과정 완료",id="next-part",variant="success"), classes="actions"))
        await self.replace_content(*widgets)
        self._restoring_scroll = True
        self.call_after_refresh(self.restore_scroll, progress.get("scroll_y",0))
        self.set_status("학습 자료 준비 완료 · 메모 자동 저장 · 정답 보기와 다음 파트는 직접 선택합니다.")

    def followup_widgets(self, followups: list[dict]) -> list:
        self._followups = followups
        widgets = [Label("후속 학습 · 선택하면 새 과정",classes="headline")]
        for i,item in enumerate(followups):
            widgets.append(Button(item["topic"],id=f"followup-{i}",classes="followup"))
            widgets.append(Static(item["reason"],classes="hint",markup=False))
        return widgets

    def flush_notes(self) -> None:
        if self._notes_timer:
            self._notes_timer.stop()
            self._notes_timer = None
        if self.view != "study" or not self.part_id or self._rendering:
            return
        areas = list(self.query("TextArea"))
        if areas:
            answers = {area.id.removeprefix("note-"):area.text for area in areas if area.id and area.id.startswith("note-")}
            if answers:
                self.service.save_progress(self.part_id,answers=answers)

    def save_scroll(self) -> None:
        if self.view != "study" or not self.part_id or self._rendering or self._restoring_scroll:
            return
        scroll = self.query_one("#content",VerticalScroll).scroll_y
        if scroll != self._last_scroll:
            self.service.save_progress(self.part_id,scroll_y=float(scroll))
            self._last_scroll = scroll

    def restore_scroll(self, value: float) -> None:
        if self.view == "study":
            content = self.query_one("#content",VerticalScroll)
            content.scroll_to(y=value,animate=False,force=True)
            self._last_scroll = content.scroll_y
        self._restoring_scroll = False

    @on(TextArea.Changed)
    def note_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id and event.text_area.id.startswith("note-") and not self._rendering:
            if self._notes_timer:
                self._notes_timer.stop()
            self._notes_timer = self.set_timer(1, self.flush_notes)

    @on(Select.Changed,"#section-picker")
    async def section_selected(self, event: Select.Changed) -> None:
        if self._rendering or event.value == Select.BLANK or int(event.value) == self.section_index:
            return
        await self.goto_section(int(event.value))

    def part_picker(self) -> Select:
        parts = self.repo.list_parts(self.course_id)
        return Select([(f"파트 {p['ordinal']} · {p['outline']['title']}",p['id']) for p in parts],
                      value=self.part_id,allow_blank=False,id="part-picker")

    @on(Select.Changed,"#part-picker")
    def part_selected(self, event: Select.Changed) -> None:
        if self._rendering or event.value == Select.BLANK or event.value == self.part_id:
            return
        if self.busy:
            self.query_one("#part-picker",Select).value = self.part_id
            return
        self.start_job(self.load_part(str(event.value)),"선택한 파트 불러오는 중")

    async def goto_section(self, index: int) -> None:
        self.flush_notes()
        self.section_index = max(0,min(index,len(self.lesson["sections"])))
        self.service.save_progress(self.part_id,section_index=self.section_index,scroll_y=0)
        await self.show_study()

    async def next_part(self) -> None:
        self.flush_notes()
        self.save_scroll()
        next_id = self.service.choose_next(self.part_id)
        if next_id:
            await self.load_part(next_id)
        else:
            await self.show_completion()

    async def show_completion(self) -> None:
        self.view = "completed"
        course = self.repo.get_course(self.course_id)
        followups = self.lesson["follow_ups"] if self.lesson else []
        await self.replace_content(Label(f"과정을 마쳤습니다 · {course['title']}",classes="headline"),
            Static("계획한 파트가 모두 끝났습니다. 교재를 다시 읽거나 새로운 주제를 선택할 수 있습니다.",classes="hint"),
            self.part_picker(),Button("선택한 파트 다시 읽기",id="review"),*self.followup_widgets(followups),Button("과정 목록",id="home"))
        self.set_status("과정 완료 · 후속 과정은 선택할 때만 만들어집니다.")

    async def open_course(self, course_id: str) -> None:
        course = self.repo.get_course(course_id)
        self.course_id = course_id
        if course["status"] == "draft":
            await self.show_plan(self.repo.get_plan(course_id),course_id)
        else:
            parts = self.repo.list_parts(course_id)
            part_id = course["current_part_id"] or parts[-1]["id"]
            await self.load_part(part_id)
            if course["status"] == "completed":
                await self.show_completion()

    async def follow_up(self,index:int) -> None:
        self.flush_notes()
        topic = self._followups[index]["topic"]
        course_id = await self.service.create_follow_up(self.course_id,topic)
        await self.show_plan(self.repo.get_plan(course_id),course_id)

    async def export_pdf(self) -> None:
        directory = self.repo.resolve(str(PurePosixPath(self.material["lesson_path"]).parent))
        assets = copy.deepcopy(self.material["assets"])
        for asset in assets:
            for key in ("source_path","rendered_path"):
                asset[key] = self.repo.resolve(asset[key]).relative_to(directory).as_posix()
        pdf = await self.pdf_exporter.export(self.lesson,assets,directory)
        open_local(pdf)
        self.set_status(f"파트 PDF를 내보냈습니다: {pdf}")

    async def retry_visual(self, visual_id: str) -> None:
        visual = next(v for v in self.lesson["visuals"] if v["id"] == visual_id)
        current = copy.deepcopy(next(a for a in self.material["assets"] if a["visual_id"] == visual_id))
        directory = self.repo.resolve(str(PurePosixPath(self.material["lesson_path"]).parent))
        for key in ("source_path","rendered_path"):
            current[key] = self.repo.resolve(current[key]).relative_to(directory).as_posix()
        asset = await self.renderer.retry_original(visual,directory,current)
        self.material = self.repo.replace_asset(self.material["id"],asset)
        await self.show_study()

    @on(Button.Pressed)
    async def button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if self.busy:
            self.set_status("작업 중입니다. Ctrl+X로 취소할 수 있습니다.")
            return
        if button_id == "propose": self.start_job(self.propose(),"계획 생성 중")
        elif button_id == "adjust": self.start_job(self.adjust(),"계획 조정 중")
        elif button_id == "start": self.start_job(self.start_course(),"첫 파트 준비 중")
        elif button_id == "retry-part": self.start_job(self.load_part(self.part_id),"교재 다시 준비 중")
        elif button_id == "save-plan":
            try: self.save_plan()
            except (ValueError,StorageError) as exc: self.set_status(str(exc))
        elif button_id.startswith("course-"): self.start_job(self.open_course(button_id.removeprefix("course-")),"과정 불러오는 중")
        elif button_id == "previous-section": await self.goto_section(self.section_index-1)
        elif button_id == "next-section": await self.goto_section(self.section_index+1)
        elif button_id == "next-part": self.start_job(self.next_part(),"다음 파트 준비 중")
        elif button_id == "reveal":
            self.flush_notes(); self.service.reveal_answers(self.part_id); await self.show_study()
        elif button_id == "regenerate": self.start_job(self.load_part(self.part_id,regenerate=True),"교재 다시 생성 중")
        elif button_id == "pdf": self.start_job(self.export_pdf(),"PDF 내보내는 중")
        elif button_id.startswith("asset-"):
            asset = next(a for a in self.material["assets"] if a["visual_id"] == button_id.removeprefix("asset-"))
            open_local(self.repo.resolve(asset["rendered_path"]))
        elif button_id.startswith("retry-"): self.start_job(self.retry_visual(button_id.removeprefix("retry-")),"원래 그림 다시 시도 중")
        elif button_id.startswith("followup-"): self.start_job(self.follow_up(int(button_id.removeprefix("followup-"))),"새 후속 과정 준비 중")
        elif button_id == "review": await self.show_study()
        elif button_id == "home": await self.show_home()

    def action_cancel(self) -> None:
        if self._worker and self.busy:
            self._worker.cancel()
            self.set_status("취소 요청 · 프로세스를 정리하고 있습니다")

    async def action_home(self) -> None:
        if self.busy:
            self.action_cancel()
            return
        await self.show_home()

    def action_save(self) -> None:
        if self.view == "plan":
            try: self.save_plan()
            except (ValueError,StorageError) as exc: self.set_status(str(exc))
        else:
            self.flush_notes(); self.save_scroll(); self.set_status("읽던 위치와 메모를 저장했습니다.")

    async def action_quit(self) -> None:
        self.flush_notes()
        self.save_scroll()
        if self._worker and self.busy:
            self._worker.cancel()
            try:
                await self._worker.wait()
            except Exception:
                pass
        self.exit()
