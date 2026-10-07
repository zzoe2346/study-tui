"""Short SQLite transactions and atomic, confined local material files."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from uuid import uuid4

from .models import ContentError, stable_json, validate_lesson, validate_plan


class StorageError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, title TEXT NOT NULL,
 objectives_json TEXT NOT NULL, scope TEXT NOT NULL, plan_json TEXT NOT NULL,
 plan_revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','active','completed')),
 current_part_id TEXT REFERENCES parts(id), parent_course_id TEXT REFERENCES courses(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parts (
 id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), ordinal INTEGER NOT NULL,
 outline_json TEXT NOT NULL, input_hash TEXT NOT NULL DEFAULT '',
 active_material_id TEXT REFERENCES materials(id), UNIQUE(course_id,ordinal));
CREATE TABLE IF NOT EXISTS materials (
 id TEXT PRIMARY KEY, part_id TEXT NOT NULL REFERENCES parts(id), revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staging','ready','failed')), input_hash TEXT NOT NULL,
 lesson_path TEXT NOT NULL, content_hash TEXT NOT NULL, schema_version INTEGER NOT NULL,
 prompt_version TEXT NOT NULL, cli_version TEXT, reported_model TEXT, search_mode TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(part_id,revision));
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id), visual_id TEXT NOT NULL,
 requested_kind TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('svg','image','ascii')),
 source_path TEXT NOT NULL, rendered_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='ready'), fallback_used INTEGER NOT NULL,
 fallback_reason TEXT, UNIQUE(material_id,visual_id));
CREATE TABLE IF NOT EXISTS progress (
 part_id TEXT PRIMARY KEY REFERENCES parts(id), material_id TEXT REFERENCES materials(id),
 status TEXT NOT NULL CHECK(status IN ('not_started','studying','completed')),
 section_index INTEGER NOT NULL DEFAULT 0, scroll_y REAL NOT NULL DEFAULT 0,
 answers_json TEXT NOT NULL DEFAULT '{}',
 answers_revealed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, course_id TEXT REFERENCES courses(id), part_id TEXT REFERENCES parts(id),
 kind TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, error_category TEXT, job_dir TEXT NOT NULL,
 output_material_id TEXT REFERENCES materials(id));
PRAGMA user_version = 2;
"""


class Repository:
    def __init__(self, data_root: Path | str):
        self.data_root = Path(data_root).expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        db_path = self.resolve("study.sqlite3")
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.executescript(SCHEMA)
        if "scroll_y" not in {row["name"] for row in self.db.execute("PRAGMA table_info(progress)")}:
            self.db.execute("ALTER TABLE progress ADD COLUMN scroll_y REAL NOT NULL DEFAULT 0")
        with self.db:
            self.db.execute("UPDATE jobs SET status='interrupted',phase='interrupted',finished_at=? "
                            "WHERE status IN ('running','queued')", (now(),))

    def close(self) -> None:
        self.db.close()

    def resolve(self, relative_path: str | Path) -> Path:
        raw = str(relative_path)
        pure = PurePosixPath(raw)
        if (not raw or pure.is_absolute() or "\\" in raw or ":" in raw
                or any(part in {"", ".", ".."} for part in raw.split("/"))):
            raise StorageError("자료 경로는 루트 안의 상대 경로여야 합니다.")
        target = self.data_root.joinpath(*pure.parts)
        probe = self.data_root
        for part in pure.parts:
            probe = probe / part
            if probe.is_symlink():
                raise StorageError("자료 경로의 심볼릭 링크는 허용하지 않습니다.")
        if not target.resolve().is_relative_to(self.data_root):
            raise StorageError("자료 루트 밖 경로는 허용하지 않습니다.")
        return target

    def relative(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.data_root).as_posix()
        except ValueError as exc:
            raise StorageError("자료 루트 밖 경로입니다.") from exc
        self.resolve(rel)
        return rel

    def write_json(self, relative_path: str, value: object) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after directory creation so an existing link is never followed.
        path = self.resolve(relative_path)
        temp: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=".tmp-", delete=False) as stream:
                temp = stream.name
                stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
        return path

    def read_json(self, relative_path: str) -> object:
        path = self.resolve(relative_path)
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise StorageError("자료 파일이 없거나 크기 제한을 넘습니다.")
        return json.loads(path.read_text("utf-8"))

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("plan", "outline", "objectives", "answers"):
            if f"{key}_json" in value:
                value[key] = json.loads(value.pop(f"{key}_json"))
        for key in ("answers_revealed", "fallback_used"):
            if key in value:
                value[key] = bool(value[key])
        return value

    def _required(self, table: str, item_id: str) -> dict:
        # table names are private constants, IDs remain bound parameters.
        row = self._row(self.db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone())
        if row is None:
            raise StorageError("저장된 항목을 찾을 수 없습니다.")
        return row

    def list_courses(self) -> list[dict]:
        return [self._row(row) for row in self.db.execute("SELECT * FROM courses ORDER BY updated_at DESC")]

    def get_course(self, course_id: str) -> dict:
        return self._required("courses", course_id)

    def get_plan(self, course_id: str) -> dict:
        return validate_plan(self.get_course(course_id)["plan"])

    def list_parts(self, course_id: str) -> list[dict]:
        return [self._row(row) for row in self.db.execute("SELECT * FROM parts WHERE course_id=? ORDER BY ordinal", (course_id,))]

    def get_part(self, part_id: str) -> dict:
        return self._required("parts", part_id)

    def save_draft(self, plan: dict, course_id: str | None = None,
                   parent_course_id: str | None = None) -> str:
        plan = validate_plan(plan)
        if course_id:
            course = self.get_course(course_id)
            if course["status"] != "draft":
                raise StorageError("시작한 과정은 고정됩니다. 새 과정을 만드세요.")
            revision = course["plan_revision"] + 1
        else:
            course_id, revision = str(uuid4()), 1
            if parent_course_id:
                self.get_course(parent_course_id)
        self.write_json(f"courses/{course_id}/plan-r{revision}.json", plan)
        stamp = now()
        with self.db:
            if revision == 1:
                self.db.execute("INSERT INTO courses VALUES (?,?,?,?,?,?,?,'draft',NULL,?,?,?)",
                                (course_id, plan["topic"], plan["title"], stable_json(plan["objectives"]),
                                 plan["scope"], stable_json(plan), revision, parent_course_id, stamp, stamp))
            else:
                self.db.execute("UPDATE courses SET topic=?,title=?,objectives_json=?,scope=?,plan_json=?,"
                                "plan_revision=?,updated_at=? WHERE id=?",
                                (plan["topic"], plan["title"], stable_json(plan["objectives"]), plan["scope"],
                                 stable_json(plan), revision, stamp, course_id))
        return course_id

    def start_course(self, course_id: str) -> str:
        course = self.get_course(course_id)
        if course["status"] != "draft":
            if course["current_part_id"]:
                return course["current_part_id"]
            raise StorageError("완료한 과정입니다.")
        plan = validate_plan(course["plan"])
        first = None
        with self.db:
            for outline in plan["parts"]:
                part_id = str(uuid4())
                first = first or part_id
                self.db.execute("INSERT INTO parts(id,course_id,ordinal,outline_json) VALUES(?,?,?,?)",
                                (part_id, course_id, outline["ordinal"], stable_json(outline)))
                self.db.execute("INSERT INTO progress(part_id,status,updated_at) VALUES(?,'not_started',?)",
                                (part_id, now()))
            self.db.execute("UPDATE courses SET status='active',current_part_id=?,updated_at=? WHERE id=?",
                            (first, now(), course_id))
        return first

    def get_progress(self, part_id: str) -> dict:
        value = self._row(self.db.execute("SELECT * FROM progress WHERE part_id=?", (part_id,)).fetchone())
        if value is None:
            raise StorageError("파트 진행을 찾을 수 없습니다.")
        return value

    def save_progress(self, part_id: str, *, section_index: int | None = None,
                      scroll_y: float | None = None,
                      answers: dict[str, str] | None = None,
                      answers_revealed: bool | None = None) -> dict:
        progress = self.get_progress(part_id)
        material = self.get_active_material(part_id)
        if section_index is not None:
            if not isinstance(section_index, int) or section_index < 0:
                raise StorageError("읽기 위치가 잘못되었습니다.")
            progress["section_index"] = section_index
        if scroll_y is not None:
            if not isinstance(scroll_y, (float, int)) or not math.isfinite(scroll_y) or scroll_y < 0:
                raise StorageError("스크롤 위치가 잘못되었습니다.")
            progress["scroll_y"] = float(scroll_y)
        if answers is not None:
            if not isinstance(answers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in answers.items()):
                raise StorageError("답 메모는 문제 ID와 문자열이어야 합니다.")
            if not material and answers:
                raise StorageError("완성된 문제 자료가 필요합니다.")
            if material:
                ids = {quiz["id"] for quiz in self.load_lesson(material["id"])["quizzes"]}
                if not set(answers) <= ids:
                    raise StorageError("현재 자료에 없는 문제의 답 메모입니다.")
            if len(stable_json(answers)) > 128 * 1024:
                raise StorageError("답 메모가 너무 큽니다.")
            progress["answers"] = answers
        if answers_revealed is not None:
            progress["answers_revealed"] = bool(answers_revealed)
        with self.db:
            self.db.execute("UPDATE progress SET section_index=?,scroll_y=?,answers_json=?,answers_revealed=?,updated_at=? WHERE part_id=?",
                            (progress["section_index"], progress["scroll_y"], stable_json(progress["answers"]), int(progress["answers_revealed"]), now(), part_id))
            self.db.execute("UPDATE courses SET updated_at=? WHERE id=(SELECT course_id FROM parts WHERE id=?)", (now(), part_id))
        return self.get_progress(part_id)

    def stage_material(self, part_id: str, lesson: dict, input_hash: str, *,
                       prompt_version: str = "1", cli_version: str | None = None,
                       reported_model: str | None = None, search_mode: str = "cached") -> dict:
        lesson = validate_lesson(lesson)
        part = self.get_part(part_id)
        material_id = str(uuid4())
        revision = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM materials WHERE part_id=?", (part_id,)).fetchone()[0]
        relative = f"courses/{part['course_id']}/parts/{part_id}/materials/{material_id}/lesson.json"
        self.write_json(relative, lesson)
        with self.db:
            self.db.execute("INSERT INTO materials VALUES(?,?,?,'staging',?,?,?,?,?,?,?,?,?)",
                            (material_id, part_id, revision, input_hash, relative, digest(lesson), 1,
                             prompt_version, cli_version, reported_model, search_mode, now()))
        return self.get_material(material_id)

    def get_material(self, material_id: str) -> dict:
        material = self._required("materials", material_id)
        material["assets"] = self.get_assets(material_id)
        return material

    def get_active_material(self, part_id: str) -> dict | None:
        part = self.get_part(part_id)
        return self.get_material(part["active_material_id"]) if part["active_material_id"] else None

    def get_assets(self, material_id: str) -> list[dict]:
        return [self._row(row) for row in self.db.execute("SELECT * FROM assets WHERE material_id=? ORDER BY visual_id", (material_id,))]

    def load_lesson(self, material_id: str) -> dict:
        material = self._required("materials", material_id)
        lesson = validate_lesson(self.read_json(material["lesson_path"]))
        if digest(lesson) != material["content_hash"]:
            raise StorageError("저장된 본문 해시가 다릅니다.")
        return lesson

    def valid_material(self, material_id: str) -> bool:
        try:
            material = self.get_material(material_id)
            if material["state"] != "ready":
                return False
            lesson = self.load_lesson(material_id)
            assets = material["assets"]
            if {asset["visual_id"] for asset in assets} != {v["id"] for v in lesson["visuals"]}:
                return False
            for asset in assets:
                source = self.resolve(asset["source_path"])
                rendered = self.resolve(asset["rendered_path"])
                if not source.is_file() or not rendered.is_file() or file_hash(rendered) != asset["sha256"]:
                    return False
            return True
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def find_definition(self, part_id: str, input_hash: str) -> dict | None:
        rows = self.db.execute("SELECT id FROM materials WHERE part_id=? AND input_hash=? ORDER BY revision DESC", (part_id, input_hash))
        for row in rows:
            try:
                self.load_lesson(row["id"])
                return self.get_material(row["id"])
            except (OSError, ValueError):
                continue
        return None

    def _asset(self, material: dict, value: dict) -> dict:
        asset = dict(value)
        base = PurePosixPath(material["lesson_path"]).parent
        for key in ("source_path", "rendered_path"):
            raw = str(asset[key])
            # Renderer paths are material-relative; already stored assets are root-relative.
            relative = raw if raw.startswith(str(base) + "/") else str(base / raw)
            path = self.resolve(relative)
            if not path.is_file() or path.stat().st_size == 0 or path.stat().st_size > 32 * 1024 * 1024:
                raise StorageError("필수 그림 파일이 없거나 크기 제한을 넘습니다.")
            asset[key] = relative
        actual_hash = file_hash(self.resolve(asset["rendered_path"]))
        if asset.get("sha256") and asset["sha256"] != actual_hash:
            raise StorageError("그림 해시가 다릅니다.")
        if asset.get("status") != "ready" or asset.get("kind") not in {"ascii", "svg", "image"}:
            raise StorageError("완성되지 않은 그림입니다.")
        asset["sha256"] = actual_hash
        asset["fallback_used"] = bool(asset.get("fallback_used", False))
        asset["fallback_reason"] = asset.get("fallback_reason")
        return asset

    def _insert_asset(self, material_id: str, asset: dict) -> None:
        self.db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid4()), material_id, asset["visual_id"], asset["requested_kind"], asset["kind"],
                         asset["source_path"], asset["rendered_path"], asset["sha256"], "ready",
                         int(asset["fallback_used"]), asset["fallback_reason"]))

    def ready_material(self, material_id: str, assets: list[dict]) -> dict:
        material = self.get_material(material_id)
        lesson = self.load_lesson(material_id)
        required = {v["id"]: v["kind"] for v in lesson["visuals"]}
        if len(assets) != len(required) or {a["visual_id"] for a in assets} != set(required):
            raise StorageError("모든 필수 그림의 원본 또는 대체가 필요합니다.")
        prepared = [self._asset(material, asset) for asset in assets]
        if any(asset["requested_kind"] != required[asset["visual_id"]] for asset in prepared):
            raise StorageError("그림 원본 종류가 본문 정의와 다릅니다.")
        previous = self.get_active_material(material["part_id"])
        progress = self.get_progress(material["part_id"])
        reset_answers = False
        if previous and previous["id"] != material_id:
            self.write_json(str(PurePosixPath(previous["lesson_path"]).parent / "progress-snapshot.json"), progress)
            try:
                reset_answers = self.load_lesson(previous["id"])["quizzes"] != lesson["quizzes"]
            except (OSError, ValueError):
                # A corrupt old lesson cannot prevent adopting a complete new one.
                # Archive its notes, but don't attach them to unverified questions.
                reset_answers = True
        with self.db:
            self.db.execute("DELETE FROM assets WHERE material_id=?", (material_id,))
            for asset in prepared:
                self._insert_asset(material_id, asset)
            self.db.execute("UPDATE materials SET state='ready' WHERE id=?", (material_id,))
            self.db.execute("UPDATE parts SET active_material_id=?,input_hash=? WHERE id=?",
                            (material_id, material["input_hash"], material["part_id"]))
            self.db.execute("UPDATE progress SET material_id=?,status=CASE WHEN status='completed' THEN status ELSE 'studying' END,"
                            "section_index=?,scroll_y=?,answers_json=?,answers_revealed=?,updated_at=? WHERE part_id=?",
                            (material_id, 0 if previous and previous["id"] != material_id else progress["section_index"],
                             0 if previous and previous["id"] != material_id else progress["scroll_y"],
                             "{}" if reset_answers else stable_json(progress["answers"]),
                             0 if reset_answers else int(progress["answers_revealed"]), now(), material["part_id"]))
        return self.get_material(material_id)

    def fail_material(self, material_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE materials SET state='failed' WHERE id=? AND state!='ready'", (material_id,))

    def replace_asset(self, material_id: str, asset: dict) -> dict:
        material = self.get_material(material_id)
        if material["state"] != "ready":
            raise StorageError("완성된 자료의 그림만 교체할 수 있습니다.")
        existing = {a["visual_id"] for a in material["assets"]}
        if asset["visual_id"] not in existing:
            raise StorageError("자료에 없는 그림입니다.")
        requested = next(v["kind"] for v in self.load_lesson(material_id)["visuals"] if v["id"] == asset["visual_id"])
        if asset.get("requested_kind") != requested:
            raise StorageError("그림 원본 종류가 본문 정의와 다릅니다.")
        prepared = self._asset(material, asset)
        with self.db:
            self.db.execute("DELETE FROM assets WHERE material_id=? AND visual_id=?", (material_id, asset["visual_id"]))
            self._insert_asset(material_id, prepared)
        manifest = self.resolve(str(PurePosixPath(material["lesson_path"]).parent / "render-manifest.json"))
        manifest.unlink(missing_ok=True)
        return self.get_material(material_id)

    def choose_next(self, part_id: str) -> str | None:
        part = self.get_part(part_id)
        course = self.get_course(part["course_id"])
        next_row = self.db.execute("SELECT id FROM parts WHERE course_id=? AND ordinal=?", (part["course_id"], part["ordinal"] + 1)).fetchone()
        next_id = next_row["id"] if next_row else None
        with self.db:
            self.db.execute("UPDATE progress SET status='completed',updated_at=? WHERE part_id=?", (now(), part_id))
            if course["status"] == "completed":
                self.db.execute("UPDATE courses SET updated_at=? WHERE id=?", (now(), part["course_id"]))
            else:
                self.db.execute("UPDATE courses SET current_part_id=?,status=?,updated_at=? WHERE id=?",
                                (next_id, "active" if next_id else "completed", now(), part["course_id"]))
        return next_id

    def begin_job(self, course_id: str | None, part_id: str | None, kind: str) -> str:
        if part_id is not None:
            if self.get_part(part_id)["course_id"] != course_id:
                raise StorageError("작업의 파트와 과정이 일치하지 않습니다.")
        elif course_id is not None:
            self.get_course(course_id)
        job_id = str(uuid4())
        relative = f"jobs/{job_id}"
        self.resolve(relative).mkdir(parents=True, exist_ok=True)
        with self.db:
            self.db.execute("INSERT INTO jobs(id,course_id,part_id,kind,status,phase,started_at,job_dir) VALUES(?,?,?,?,'running','starting',?,?)",
                            (job_id, course_id, part_id, kind, now(), relative))
        return job_id

    def finish_job(self, job_id: str, status: str, *, material_id: str | None = None,
                   error_category: str | None = None) -> None:
        if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise StorageError("작업 종료 상태가 잘못되었습니다.")
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,phase=?,finished_at=?,output_material_id=?,error_category=? WHERE id=?",
                            (status, status, now(), material_id, error_category, job_id))

    def list_jobs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY started_at DESC")]
