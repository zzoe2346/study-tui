import asyncio
import copy
import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image
from pypdf import PdfReader
import pytest

from study_tui.rendering import PDFExporter, PDFRenderError, VisualRenderer, VisualRenderError, sanitize_svg, validate_ascii


ASCII = "Query\n  |\n  v\n[Index] --> [Row]\n  |\n  +-------> [Next key]"


def lesson_fixture():
    return {"schema_version": 1, "title": "B-tree 인덱스: 정렬된 탐색과 비용", "minutes": 35,
            "sections": [
                {"kind": "concept", "title": "인덱스가 줄이는 탐색 범위", "body_markdown": "인덱스는 정렬된 키와 행 위치를 저장합니다. 전체 테이블을 읽는 대신 필요한 범위로 이동합니다. 선택도가 낮으면 전체 스캔이 더 유리할 수 있습니다.", "visual_ids": ["v1"], "source_ids": ["s1"]},
                {"kind": "mechanism", "title": "루트에서 리프까지", "body_markdown": "루트의 경계 키로 하위 페이지를 고릅니다. 리프에서 일치하는 키를 찾고 연결된 행을 읽습니다. v2의 Query는 질의, Index는 탐색 경로, Row는 최종 행을 뜻합니다.", "visual_ids": ["v2"], "source_ids": ["s1"]},
                {"kind": "example", "title": "실무 예시: 주문 목록", "body_markdown": "복합 인덱스의 첫 키는 필터 조건, 다음 키는 정렬 조건과 맞아야 합니다.\n\n```sql\nCREATE INDEX orders_user_created\n    ON orders (user_id, created_at DESC);\n\nSELECT id, created_at, total\nFROM orders\nWHERE user_id = 42\nORDER BY created_at DESC\nLIMIT 20;\n```\n\n" + "대규모 테이블에서는 실행 계획과 실제 시간을 함께 확인합니다. " * 35 + "\n\n```text\n" + "A long code line with an explanatory comment and column names user_id created_at status total " * 3 + "\n```", "visual_ids": [], "source_ids": ["s1"]},
                {"kind": "summary", "title": "핵심 정리", "body_markdown": "- 검색과 정렬을 위해 추가 저장 공간을 사용합니다.\n- 쓰기 비용과 선택도를 고려합니다.\n- 인덱스 생성 전후에 실행 계획을 비교합니다.", "visual_ids": [], "source_ids": ["s1"]}],
            "visuals": [
                {"id": "v1", "kind": "mermaid", "source": "flowchart TD\n A[루트 페이지] --> B[왼쪽 리프]\n A --> C[오른쪽 리프]\n B --> D[테이블 행]", "caption": "루트의 키 범위에 따라 리프 페이지와 행으로 이동하는 탐색 경로입니다.", "alt_text": "루트에서 두 리프로 분기하고 왼쪽 리프에서 행을 읽는 도식", "fallbacks": [{"kind": "ascii", "source": ASCII}]},
                {"id": "v2", "kind": "image_prompt", "source": "A clear index lookup illustration", "caption": "질의가 인덱스에서 행과 다음 키를 찾는 흐름입니다.", "alt_text": "인덱스 탐색과 다음 키", "fallbacks": [{"kind": "mermaid", "source": "this is invalid Mermaid"}, {"kind": "ascii", "source": ASCII}]}],
            "quizzes": [{"id": "q1", "question": "테이블 전체 스캔이 인덱스보다 유리할 수 있는 조건은 무엇인가요?", "answer": "대부분의 행을 반환하는 낮은 선택도의 조건입니다.", "explanation": "정답고유표시: 인덱스 탐색 후 다수의 행을 읽는 비용이 순차 스캔보다 커질 수 있습니다."}],
            "sources": [{"id": "s1", "title": "PostgreSQL: Indexes", "url": "https://www.postgresql.org/docs/current/indexes.html", "checked_at": None}],
            "follow_ups": [{"topic": "복합 인덱스와 정렬", "reason": "조건과 정렬 순서가 인덱스 활용에 미치는 영향을 더 공부할 수 있습니다."}]}


def test_ascii_preserves_spaces_and_rejects_unprintable_or_wide():
    assert validate_ascii(ASCII).decode() == ASCII
    for source in ("A\tB", "A\x1b[31m", "가", "x" * 73, "\n" * 49):
        with pytest.raises(VisualRenderError):
            validate_ascii(source)


@pytest.mark.parametrize("body", ["<script>alert(1)</script>", '<rect onclick="alert(1)"/>', '<foreignObject/>', '<image href="https://example.com/a.png"/>', '<style>@import "https://example.com/a.css";</style>', '<rect style="fill:u\\72l(\\68ttps://example.com/a)"/>', '<animate attributeName="href" values="https://example.com"/>'])
def test_svg_rejects_active_or_external_content(body):
    with pytest.raises(VisualRenderError):
        sanitize_svg(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{body}</svg>')


@pytest.mark.asyncio
async def test_stored_ascii_fallback_and_failed_retry_preserve_current(tmp_path):
    class BrokenRenderer(VisualRenderer):
        async def _mermaid(self, source):
            raise VisualRenderError("Mermaid 시험 실패")
    renderer = BrokenRenderer()
    lesson = lesson_fixture()
    assets = await renderer.render_visuals(lesson, tmp_path)
    assert all(asset["kind"] == "ascii" and asset["fallback_used"] for asset in assets)
    assert (tmp_path / assets[0]["rendered_path"]).read_text() == ASCII
    before = (tmp_path / assets[0]["rendered_path"]).read_bytes()
    with pytest.raises(VisualRenderError):
        await renderer.retry_original(lesson["visuals"][0], tmp_path, assets[0])
    assert (tmp_path / assets[0]["rendered_path"]).read_bytes() == before
    bad = copy.deepcopy(lesson)
    bad["visuals"][0]["fallbacks"][0]["source"] = "x" * 73
    with pytest.raises(VisualRenderError, match="모든 그림 후보"):
        await renderer.render_visuals(bad, tmp_path / "bad")


@pytest.mark.asyncio
async def test_native_image_provider_and_cancellation(tmp_path):
    async def image_provider(prompt, destination):
        Image.new("RGB", (200, 200), "white").save(destination, "PNG")
        return destination
    lesson = lesson_fixture()
    lesson["visuals"] = [lesson["visuals"][1]]
    assets = await VisualRenderer(image_generator=image_provider).render_visuals(lesson, tmp_path)
    assert assets[0]["kind"] == "image" and not assets[0]["fallback_used"]

    async def cancelled(prompt, destination):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await VisualRenderer(image_generator=cancelled).render_visuals(lesson, tmp_path / "cancel")
    assert not (tmp_path / "cancel" / "visual-manifest.json").exists()


@pytest.mark.asyncio
async def test_actual_mermaid_korean_and_pdf_same_assets_answers_cache(tmp_path):
    lesson = lesson_fixture()
    assets = await VisualRenderer().render_visuals(lesson, tmp_path)
    assert assets[0]["kind"] == "svg" and not assets[0]["fallback_used"]
    assert assets[1]["kind"] == "ascii" and assets[1]["fallback_used"]
    svg = (tmp_path / assets[0]["rendered_path"]).read_text()
    assert "foreignObject" not in svg
    text = "".join(ET.fromstring(svg).itertext())
    assert "루트 페이지" in text and "테이블 행" in text
    pdf = await PDFExporter().export(lesson, assets, tmp_path)
    reader = PdfReader(pdf)
    pages = [page.extract_text() for page in reader.pages]
    quiz_page = next(i for i, page in enumerate(pages) if "이해도 확인 퀴즈" in page)
    answer_page = next(i for i, page in enumerate(pages) if "정답고유표시" in page)
    assert answer_page > quiz_page
    assert "B-tree" in pages[0] and "인덱스" in pages[0]
    assert "ASCII 대체 사용" in "\n".join(pages)
    assert "[Index]" in "\n".join(pages) and "[Next key]" in "\n".join(pages)
    assert not any("정답고유표시" in page for page in pages[:answer_page])
    assert "white-space:pre" in (tmp_path / "lesson.html").read_text()
    before = pdf.stat().st_mtime_ns
    assert await PDFExporter().export(lesson, assets, tmp_path) == pdf
    assert pdf.stat().st_mtime_ns == before
    (tmp_path / assets[1]["rendered_path"]).write_text("corrupt")
    with pytest.raises(PDFRenderError, match="해시"):
        await PDFExporter().export(lesson, assets, tmp_path)


def test_markdown_cannot_insert_html_or_remote_images():
    from study_tui.rendering import _markdown
    output = _markdown().render('<script>bad()</script>\n\n![remote](https://example.com/a.png)\n\n[unsafe](javascript:alert(1))')
    assert "<script>" not in output and "<img" not in output
    assert '<a href="javascript:' not in output
