"""Local visual candidates and a print template from the same saved Lesson.

Rendering never asks an AI for another fallback. A caller supplies an optional
subscription image provider; every other candidate is already in the Lesson.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Awaitable, Callable
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as SafeET
from markdown_it import MarkdownIt
from PIL import Image
from playwright.async_api import async_playwright


PACKAGE_ASSETS = Path(__file__).parent / "assets"
FONT_KR = PACKAGE_ASSETS / "fonts" / "NotoSansKR.ttf"
FONT_MONO = PACKAGE_ASSETS / "fonts" / "NotoSansMono.ttf"
MERMAID_JS = PACKAGE_ASSETS / "vendor" / "mermaid-11.12.2.min.js"
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
ImageProvider = Callable[[str, Path], Awaitable[Path]]


class VisualRenderError(RuntimeError):
    """No usable stored candidate was rendered; old ready assets stay valid."""


class PDFRenderError(RuntimeError):
    pass


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_asset_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise VisualRenderError("자료 경로가 허용된 디렉터리를 벗어납니다.")
    base = root.resolve()
    candidate = root / rel
    for parent in (candidate, *candidate.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise VisualRenderError("자료의 심볼릭 링크를 열 수 없습니다.")
    if not candidate.resolve().is_relative_to(base):
        raise VisualRenderError("자료 경로가 허용된 디렉터리를 벗어납니다.")
    return candidate


def validate_ascii(source: str) -> bytes:
    if not source.strip() or any(ord(char) < 32 and char != "\n" or ord(char) > 126 for char in source):
        raise VisualRenderError("ASCII 도식은 인쇄 가능한 ASCII와 줄바꿈만 허용합니다.")
    lines = source.split("\n")
    if max(map(len, lines)) > 72:
        raise VisualRenderError("ASCII 도식이 최대 인쇄 폭 72칸을 넘습니다.")
    if len(lines) > 48:
        raise VisualRenderError("ASCII 도식이 한 페이지 높이를 넘습니다. 분할한 저장 후보가 필요합니다.")
    return source.encode("utf-8")


def sanitize_svg(source: str) -> bytes:
    """Accept inert, self-contained SVG only; don't repair executable content."""
    if len(source.encode("utf-8")) > 2 * 1024 * 1024:
        raise VisualRenderError("SVG가 허용 크기를 넘습니다.")
    try:
        root = SafeET.fromstring(source)
    except Exception as exc:
        raise VisualRenderError("SVG XML을 읽을 수 없습니다.") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise VisualRenderError("SVG 루트와 네임스페이스가 필요합니다.")
    try:
        dimensions = [float(value) for value in re.split(r"[ ,]+", root.attrib["viewBox"].strip())]
        if len(dimensions) != 4 or not all(math.isfinite(x) for x in dimensions) or not all(x > 0 for x in dimensions[2:]):
            raise ValueError
    except (KeyError, ValueError):
        raise VisualRenderError("SVG의 유효한 viewBox가 필요합니다.") from None
    forbidden = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video", "animate", "set"}
    for node in root.iter():
        if not node.tag.startswith(f"{{{SVG_NS}}}"):
            raise VisualRenderError("SVG 외부 네임스페이스 요소는 허용하지 않습니다.")
        tag = node.tag.split("}")[-1].lower()
        if tag in forbidden:
            raise VisualRenderError("SVG에 실행 가능한 요소가 포함되어 있습니다.")
        for attribute, value in node.attrib.items():
            name = attribute.split("}")[-1].lower()
            if name.startswith("on") or name in {"src", "srcset"}:
                raise VisualRenderError("SVG에 실행 또는 외부 참조 속성이 포함되어 있습니다.")
            if name == "href" and not value.startswith("#"):
                raise VisualRenderError("SVG 외부 참조는 허용하지 않습니다.")
            _validate_svg_css(value)
        if tag == "style":
            _validate_svg_css(node.text or "")
    root.set("role", "img")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _validate_svg_css(value: str) -> None:
    if "\\" in value or re.search(r"@import|javascript\s*:|expression\s*\(|(?:https?|file|data)\s*:", value, re.I):
        raise VisualRenderError("SVG의 외부 또는 실행 가능한 스타일은 허용하지 않습니다.")
    for match in re.finditer(r"url\s*\((.*?)\)", value, re.I | re.S):
        if not match.group(1).strip(" \t\r\n\"'").startswith("#"):
            raise VisualRenderError("SVG 스타일의 외부 참조는 허용하지 않습니다.")


def _describe_svg(content: bytes, visual: dict) -> bytes:
    root = SafeET.fromstring(content)
    for child in list(root):
        if child.tag in {f"{{{SVG_NS}}}title", f"{{{SVG_NS}}}desc"}:
            root.remove(child)
    title = ET.Element(f"{{{SVG_NS}}}title", {"id": visual["id"] + "-title"})
    title.text = visual["caption"]
    description = ET.Element(f"{{{SVG_NS}}}desc", {"id": visual["id"] + "-desc"})
    description.text = visual["alt_text"]
    root.insert(0, description)
    root.insert(0, title)
    root.set("aria-labelledby", title.attrib["id"])
    root.set("aria-describedby", description.attrib["id"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def validate_image(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise VisualRenderError("이미지 파일 경로 또는 용량이 올바르지 않습니다.")
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width < 128 or image.height < 128 or image.width * image.height > 40_000_000:
                raise VisualRenderError("이미지 형식 또는 해상도가 올바르지 않습니다.")
            image.verify()
        # Normalize to PNG: extensions and content always agree.
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            return output.getvalue()
    except VisualRenderError:
        raise
    except Exception as exc:
        raise VisualRenderError("이미지 내용을 읽을 수 없습니다.") from exc


class VisualRenderer:
    def __init__(self, image_generator: ImageProvider | None = None, timeout_seconds: float = 45):
        self.image_generator = image_generator
        self.timeout_seconds = timeout_seconds

    async def render_visuals(self, lesson: dict, material_dir: Path) -> list[dict]:
        material_dir = Path(material_dir)
        material_dir.mkdir(parents=True, exist_ok=True)
        assets = []
        for visual in lesson["visuals"]:
            failures: list[str] = []
            candidates = [{"kind": visual["kind"], "source": visual["source"]}, *visual["fallbacks"]]
            for index, candidate in enumerate(candidates):
                try:
                    asset = await self._candidate(visual, candidate, material_dir, "primary" if index == 0 else f"fallback-{index}")
                    asset["fallback_used"] = index != 0
                    asset["fallback_reason"] = "; ".join(failures) if index else None
                    assets.append(asset)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures.append(self._safe_failure(candidate["kind"], exc))
            else:
                raise VisualRenderError(f"{visual['id']}: 저장된 모든 그림 후보를 사용할 수 없습니다. " + "; ".join(failures))
        _atomic_write(material_dir / "visual-manifest.json", _json_bytes(assets))
        return assets

    async def retry_original(self, visual: dict, material_dir: Path, current_asset: dict) -> dict:
        # Unique files keep the current fallback intact even if a retry is interrupted.
        asset = await self._candidate(visual, visual, Path(material_dir), "original-" + uuid.uuid4().hex[:8])
        asset.update(fallback_used=False, fallback_reason=None)
        return asset

    @staticmethod
    def _safe_failure(kind: str, exc: Exception) -> str:
        if isinstance(exc, VisualRenderError):
            return f"{kind}: {exc}"
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return f"{kind}: 제한 시간 초과"
        return f"{kind}: 로컬 렌더링 실패 ({type(exc).__name__})"

    async def _candidate(self, visual: dict, candidate: dict, material_dir: Path, label: str) -> dict:
        visual_id = visual["id"]
        if not re.fullmatch(r"v[1-9][0-9]*", visual_id):
            raise VisualRenderError("그림 ID가 올바르지 않습니다.")
        kind, source = candidate["kind"], candidate["source"]
        stem = f"visuals/{visual_id}-{label}"
        suffix = {"mermaid": ".mmd", "svg": ".svg", "ascii": ".txt", "image_prompt": ".prompt.txt"}.get(kind)
        if suffix is None:
            raise VisualRenderError("지원하지 않는 그림 종류입니다.")
        source_path = _safe_asset_path(material_dir, stem + suffix)
        _atomic_write(source_path, source.encode("utf-8"))
        if kind == "ascii":
            rendered = validate_ascii(source)
            selected, rendered_path = "ascii", source_path
        elif kind == "svg":
            rendered = sanitize_svg(source)
            selected, rendered_path = "svg", _safe_asset_path(material_dir, stem + ".rendered.svg")
        elif kind == "mermaid":
            async with asyncio.timeout(self.timeout_seconds):
                rendered = sanitize_svg(await self._mermaid(source))
            selected, rendered_path = "svg", _safe_asset_path(material_dir, stem + ".svg")
        else:
            if self.image_generator is None:
                raise VisualRenderError("구독 CLI 이미지 자동화를 사용할 수 없어 저장된 대체 후보를 사용합니다.")
            destination = _safe_asset_path(material_dir, stem + ".generated.png")
            result = Path(await self.image_generator(source, destination))
            if not result.resolve().is_relative_to(material_dir.resolve()):
                raise VisualRenderError("이미지 생성 결과가 자료 디렉터리 밖에 있습니다.")
            rendered = validate_image(result)
            selected, rendered_path = "image", _safe_asset_path(material_dir, stem + ".png")
        if selected == "svg":
            rendered = _describe_svg(rendered, visual)
        _atomic_write(rendered_path, rendered)
        return {"visual_id": visual_id, "requested_kind": visual["kind"], "kind": selected,
                "source_path": source_path.relative_to(material_dir).as_posix(),
                "rendered_path": rendered_path.relative_to(material_dir).as_posix(),
                "sha256": _sha(rendered), "status": "ready", "fallback_used": False,
                "fallback_reason": None}

    async def _mermaid(self, source: str) -> str:
        if len(source.encode("utf-8")) > 128 * 1024 or "%%{" in source or source.lstrip().startswith("---"):
            raise VisualRenderError("Mermaid의 크기 또는 설정 재정의가 허용되지 않습니다.")
        if re.search(r"^\s*click\s", source, re.M):
            raise VisualRenderError("Mermaid 클릭 동작은 허용하지 않습니다.")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                renderer_page = PACKAGE_ASSETS / "vendor" / "renderer.html"
                allowed = {renderer_page.resolve().as_uri(), FONT_KR.resolve().as_uri(), FONT_MONO.resolve().as_uri()}
                async def local_only(route):
                    if route.request.url in allowed:
                        await route.continue_()
                    else:
                        await route.abort()
                await page.route("**/*", local_only)
                await page.goto(renderer_page.resolve().as_uri())
                await page.add_style_tag(content=_font_css())
                # Loading the local application-controlled bundle executes no lesson HTML.
                await page.add_script_tag(path=str(MERMAID_JS))
                await page.evaluate("async () => { await document.fonts.load('16px \"Lesson Sans\"'); await document.fonts.ready; if (!document.fonts.check('16px \"Lesson Sans\"')) throw new Error('Local Korean font did not load'); }")
                return await page.evaluate("""async (source) => {
                    mermaid.initialize({startOnLoad: false, securityLevel: 'strict',
                        htmlLabels: false, theme: 'neutral', fontFamily: 'Lesson Sans, sans-serif',
                        fontSize: 16, flowchart: {htmlLabels: false, useMaxWidth: false},
                        sequence: {useMaxWidth: false}, deterministicIds: true});
                    const result = await mermaid.render('studyDiagram', source);
                    return result.svg;
                }""", source)
            finally:
                await browser.close()


def _font_css() -> str:
    return f"""@font-face {{font-family:'Lesson Sans';src:url('{FONT_KR.resolve().as_uri()}');font-weight:100 900;}}
    @font-face {{font-family:'Lesson Mono';src:url('{FONT_MONO.resolve().as_uri()}');font-weight:100 900;}}
    body {{font-family:'Lesson Sans',sans-serif;}}"""


PRINT_CSS = """
@page { size:A4; margin:15mm 15mm 18mm; }
* { box-sizing:border-box; }
body { margin:0; color:#151515; background:#fff; font-size:11pt; line-height:1.65; }
h1 {font-size:24pt;line-height:1.4;margin:0 0 5mm;font-weight:750;}
h2 {font-size:16pt;line-height:1.45;margin:9mm 0 3mm;border-bottom:1px solid #aaa;padding-bottom:2mm;}
h3 {font-size:12pt;line-height:1.5;margin:5mm 0 2mm;}
h1,h2,h3 {break-after:avoid;}
p,li {orphans:3;widows:3;overflow-wrap:anywhere;}
p {margin:2.5mm 0;}
ul,ol {padding-left:6mm;}
a {color:#222;text-decoration:underline;overflow-wrap:anywhere;}
.eyebrow {font-size:9pt;letter-spacing:1.5px;font-weight:650;margin-bottom:4mm;}
.metadata {font-size:9pt;color:#555;border-bottom:2px solid #222;padding-bottom:5mm;margin-bottom:6mm;}
pre {font-family:'Lesson Mono','Lesson Sans',monospace;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:9pt;line-height:1.5;border:1px solid #bbb;padding:3mm;background:#f5f5f5;}
code {font-family:'Lesson Mono','Lesson Sans',monospace;font-size:9pt;}
pre code {font-size:inherit;}
pre.diagram {white-space:pre;overflow-wrap:normal;word-break:normal;font-size:9pt;line-height:1.3;padding:2mm;break-inside:avoid;}
figure {margin:5mm 0;break-inside:avoid;border:1px solid #bbb;padding:4mm;}
figure img {display:block;max-width:100%;max-height:145mm;height:auto;margin:auto;filter:grayscale(1);}
figure svg {display:block;max-width:100%;max-height:145mm;height:auto;margin:auto;filter:grayscale(1);}
figure svg text,figure svg tspan {font-family:'Lesson Sans',sans-serif !important;}
figcaption {font-size:9pt;line-height:1.55;margin-top:3mm;}
.fallback {font-size:8pt;color:#555;margin-top:2mm;}
.source-note {font-size:8pt;color:#555;margin:2mm 0 0;}
.quiz {padding:4mm 0;border-bottom:1px solid #ccc;}
.quiz p {margin-top:0;}
.answer-space {height:18mm;border-bottom:1px dotted #aaa;margin:4mm 0;}
.answer-section {break-before:page;}
.answer {margin:5mm 0;}
.answer h3 {margin-top:0;}
table {border-collapse:collapse;width:100%;font-size:9pt;table-layout:fixed;}
th,td {border:1px solid #aaa;padding:2mm;overflow-wrap:anywhere;}
blockquote {border-left:3px solid #aaa;margin:4mm 0;padding-left:4mm;}
"""


def _markdown() -> MarkdownIt:
    renderer = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    renderer.validateLink = lambda url: urlparse(url).scheme.lower() in {"http", "https"}
    renderer.add_render_rule("image", lambda _renderer, tokens, index, options, env: "<span>[본문 이미지 생략: 저장된 그림을 참조하세요]</span>")
    return renderer


class PDFExporter:
    def __init__(self, timeout_seconds: float = 120):
        self.timeout_seconds = timeout_seconds

    async def export(self, lesson: dict, assets: list[dict], material_dir: Path) -> Path:
        material_dir = Path(material_dir)
        material_dir.mkdir(parents=True, exist_ok=True)
        mapping = {asset["visual_id"]: asset for asset in assets}
        if set(mapping) != {visual["id"] for visual in lesson["visuals"]}:
            raise PDFRenderError("PDF에 필요한 그림 자산이 완성되지 않았습니다.")
        checked = {}
        allowed = {FONT_KR.resolve().as_uri(), FONT_MONO.resolve().as_uri()}
        for visual_id, asset in mapping.items():
            if asset["status"] != "ready":
                raise PDFRenderError("그림 자산이 ready 상태가 아닙니다.")
            path = _safe_asset_path(material_dir, asset["rendered_path"])
            content = path.read_bytes()
            if _sha(content) != asset["sha256"]:
                raise PDFRenderError("그림 자산의 저장 해시가 일치하지 않습니다.")
            if asset["kind"] == "ascii":
                validate_ascii(content.decode("utf-8"))
            elif asset["kind"] == "svg":
                sanitize_svg(content.decode("utf-8"))
            elif asset["kind"] == "image":
                validate_image(path)
            else:
                raise PDFRenderError("PDF 그림 자산 종류가 올바르지 않습니다.")
            checked[visual_id] = (path, content)
            allowed.add(path.resolve().as_uri())
        fingerprint = _sha(_json_bytes({"lesson": lesson, "assets": assets, "template": PRINT_CSS,
                                       "template_version": 1, "fonts": [_sha(FONT_KR.read_bytes()), _sha(FONT_MONO.read_bytes())]}))
        manifest = material_dir / "render-manifest.json"
        destination = material_dir / "lesson.pdf"
        if manifest.is_file() and destination.is_file():
            try:
                cached = json.loads(manifest.read_text("utf-8"))
                if cached.get("render_hash") == fingerprint and cached.get("pdf_sha256") == _sha(destination.read_bytes()):
                    return destination
            except (ValueError, OSError):
                pass
        document = self._html(lesson, mapping, checked)
        html_path = material_dir / "lesson.html"
        _atomic_write(html_path, document.encode("utf-8"))
        allowed.add(html_path.resolve().as_uri())
        temporary = destination.with_name(f"lesson.{uuid.uuid4().hex}.tmp.pdf")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    try:
                        page = await browser.new_page(viewport={"width": 680, "height": 1024})
                        async def local_only(route):
                            if route.request.url in allowed:
                                await route.continue_()
                            else:
                                await route.abort()
                        await page.route("**/*", local_only)
                        await page.goto(html_path.resolve().as_uri(), wait_until="load")
                        await page.emulate_media(media="print")
                        await page.evaluate("""async () => {
                            await document.fonts.load('11pt "Lesson Sans"');
                            await document.fonts.load('9pt "Lesson Mono"');
                            await document.fonts.ready;
                            await Promise.all(Array.from(document.images).map(image => image.decode()));
                            if (!document.fonts.check('11pt "Lesson Sans"') || !document.fonts.check('9pt "Lesson Mono"'))
                                throw new Error('Local lesson fonts did not load');
                        }""")
                        # Preserve complete diagrams, refuse a candidate that doesn't fit.
                        overflowing = await page.locator("pre.diagram").evaluate_all("nodes => nodes.some(node => node.scrollWidth > node.clientWidth + 1)")
                        if overflowing:
                            raise PDFRenderError("ASCII 도식이 인쇄 폭을 넘습니다. 도식을 자르지 않았습니다.")
                        await page.pdf(path=str(temporary), format="A4", print_background=True,
                                       prefer_css_page_size=True, display_header_footer=True,
                                       header_template="<span></span>",
                                       footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#777">study-tui &nbsp; · &nbsp; <span class="pageNumber"></span> / <span class="totalPages"></span></div>')
                    finally:
                        await browser.close()
            if not temporary.read_bytes().startswith(b"%PDF-") or temporary.stat().st_size < 1000:
                raise PDFRenderError("PDF 출력이 완성되지 않았습니다.")
            temporary.replace(destination)
            _atomic_write(manifest, _json_bytes({"render_hash": fingerprint, "pdf_sha256": _sha(destination.read_bytes()),
                                                "template_version": 1, "paper": "A4", "answers": "separate page"}))
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _html(lesson: dict, mapping: dict, checked: dict) -> str:
        md = _markdown()
        escape = html.escape
        fragments = [f'<div class="eyebrow">STUDY-TUI · PART NOTES</div><h1>{escape(lesson["title"])}</h1>',
                     f'<div class="metadata">학습 분량 {lesson["minutes"]}분 · 개념 / 원리 / 예시 / 퀴즈 · 정답은 별도 페이지</div>']
        visuals = {visual["id"]: visual for visual in lesson["visuals"]}
        sources = {source["id"]: source for source in lesson["sources"]}
        seen = set()
        for section in lesson["sections"]:
            fragments += [f'<section><h2>{escape(section["title"])}</h2>', md.render(section["body_markdown"])]
            for visual_id in section["visual_ids"]:
                if visual_id in seen:
                    continue
                seen.add(visual_id)
                visual, asset = visuals[visual_id], mapping[visual_id]
                path, content = checked[visual_id]
                fragments.append('<figure>')
                if asset["kind"] == "ascii":
                    fragments.append(f'<pre class="diagram">{escape(content.decode("utf-8"))}</pre>')
                elif asset["kind"] == "svg":
                    # Inline inert SVG so it shares the packaged Korean font.
                    svg = re.sub(r"^\s*<\?xml[^?]*\?>", "", content.decode("utf-8"))
                    fragments.append(svg)
                else:
                    fragments.append(f'<img src="{escape(path.resolve().as_uri(), quote=True)}" alt="{escape(visual["alt_text"], quote=True)}">')
                fragments.append(f'<figcaption><strong>{escape(visual_id)}</strong> · {escape(visual["caption"])}</figcaption>')
                if asset["fallback_used"]:
                    fragments.append(f'<div class="fallback">{escape(asset["kind"].upper())} 대체 사용 · {escape(asset["fallback_reason"] or "원본을 사용할 수 없습니다.")}</div>')
                fragments.append('</figure>')
            if section["source_ids"]:
                fragments.append('<p class="source-note">관련 출처: ' + ", ".join(escape(sources[id]["title"]) for id in section["source_ids"]) + '</p>')
            fragments.append('</section>')
        fragments.append('<section><h2>이해도 확인 퀴즈</h2><p>종이에 풀거나 간단히 메모하세요. 정답 확인이나 답안 작성은 다음 파트로 이동하는 조건이 아닙니다.</p>')
        for index, quiz in enumerate(lesson["quizzes"], 1):
            fragments.append(f'<div class="quiz"><h3>문제 {index}</h3>{md.render(quiz["question"])}<div class="answer-space"></div></div>')
        fragments.append('</section><section class="answer-section"><h2>정답과 해설</h2>')
        for index, quiz in enumerate(lesson["quizzes"], 1):
            fragments.append(f'<div class="answer"><h3>문제 {index} 정답</h3>{md.render(quiz["answer"])}<h3>해설</h3>{md.render(quiz["explanation"])}</div>')
        fragments.append('</section><section><h2>출처</h2><ol>')
        for source in lesson["sources"]:
            url = source["url"]
            if urlparse(url).scheme.lower() not in {"http", "https"}:
                raise PDFRenderError("출처 URL은 HTTP(S)여야 합니다.")
            fragments.append(f'<li>{escape(source["title"])}<br><a href="{escape(url, quote=True)}">{escape(url)}</a></li>')
        fragments.append('</ol></section><section><h2>선택해서 더 공부할 주제</h2>')
        for followup in lesson["follow_ups"]:
            fragments.append(f'<h3>{escape(followup["topic"])}</h3><p>{escape(followup["reason"])}</p>')
        fragments.append('</section>')
        return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; font-src file:; img-src file:;">' + f'<title>{escape(lesson["title"])}</title><style>{_font_css()}{PRINT_CSS}</style></head><body>' + "\n".join(fragments) + '</body></html>'


async def render_visuals(lesson: dict, material_dir: Path) -> list[dict]:
    return await VisualRenderer().render_visuals(lesson, material_dir)
