"""Versioned generation contracts shared by the CLI and local validators."""
from importlib.resources import files
import json

PLAN_SCHEMA = json.loads(files("study_tui").joinpath("schemas/plan.json").read_text("utf-8"))
LESSON_SCHEMA = json.loads(files("study_tui").joinpath("schemas/lesson.json").read_text("utf-8"))

