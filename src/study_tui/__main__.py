from __future__ import annotations

import argparse
import json
from pathlib import Path

from platformdirs import user_data_path

from . import __version__
from .app import StudyApp
from .codex import CodexGenerator
from .demo import DemoGenerator
from .rendering import VisualRenderer, PDFExporter
from .storage import Repository


def main() -> None:
    parser = argparse.ArgumentParser(description="주제를 직접 정하는 로컬 백엔드 학습 TUI")
    parser.add_argument("--version",action="version",version=f"study-tui {__version__}")
    parser.add_argument("--data-dir",type=Path,default=user_data_path("study-tui",appauthor=False))
    parser.add_argument("--demo",action="store_true",help="AI 호출 없는 고정 DB 인덱스 예제")
    parser.add_argument("--codex",default="codex",help="Codex CLI 실행 파일")
    parser.add_argument("--search",choices=["cached","live"],default=None)
    parser.add_argument("--plan-timeout",type=int,default=None)
    parser.add_argument("--lesson-timeout",type=int,default=None)
    parser.add_argument("--no-images",action="store_true",help="네이티브 그림 대신 사전 작성 후보 사용")
    args=parser.parse_args()
    data_root=args.data_dir.expanduser().resolve()
    data_root.mkdir(parents=True,exist_ok=True)
    settings_path=data_root/"settings.json"
    settings={"search_mode":"cached","plan_timeout":180,"lesson_timeout":600,"image_timeout":240,"pdf_timeout":120,"native_images":True}
    if settings_path.exists():
        try:
            saved=json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(saved,dict):
                raise ValueError
            settings.update(saved)
        except (ValueError,OSError):
            parser.error("settings.json에서 설정 객체를 읽을 수 없습니다")
    else:
        settings_path.write_text(json.dumps(settings,indent=2),encoding="utf-8")
    for key in ("plan_timeout","lesson_timeout","image_timeout","pdf_timeout"):
        if not isinstance(settings.get(key),int) or settings[key] <= 0:
            parser.error(f"settings.json의 {key}는 양의 정수여야 합니다")
    if settings.get("search_mode") not in {"cached","live"}:
        parser.error("settings.json search_mode는 cached 또는 live여야 합니다")
    for key in ("plan_timeout","lesson_timeout"):
        value=getattr(args,key)
        if value is not None and value <= 0:
            parser.error(f"--{key.replace('_','-')}는 양수여야 합니다")
    repo=Repository(data_root)
    generator=DemoGenerator() if args.demo else CodexGenerator(data_root,executable=args.codex,
        search_mode=args.search or settings["search_mode"],plan_timeout=args.plan_timeout or settings["plan_timeout"],
        lesson_timeout=args.lesson_timeout or settings["lesson_timeout"],image_timeout=settings["image_timeout"])
    image_generator=None if args.demo or args.no_images or not settings["native_images"] else generator.generate_image
    renderer=VisualRenderer(image_generator=image_generator,timeout_seconds=settings["image_timeout"])
    exporter=PDFExporter(timeout_seconds=settings["pdf_timeout"])
    try:
        StudyApp(repo,generator,renderer,exporter,demo=args.demo).run()
    finally:
        repo.close()


if __name__ == "__main__":
    main()
