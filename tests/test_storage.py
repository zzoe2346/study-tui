import copy
import hashlib
import json
from pathlib import Path

import pytest

from study_tui.demo import DEMO_LESSONS, DEMO_PLAN
from study_tui.storage import Repository, StorageError


@pytest.fixture
def repo(tmp_path):
    storage = Repository(tmp_path / "data")
    yield storage
    storage.close()


def start(repo):
    course = repo.save_draft(DEMO_PLAN)
    return course, repo.start_course(course)


def assets_for(repo, material):
    lesson = repo.load_lesson(material["id"])
    parent = repo.resolve(material["lesson_path"]).parent
    assets = []
    for visual in lesson["visuals"]:
        source = visual["source"] if visual["kind"] == "ascii" else visual["fallbacks"][-1]["source"]
        path = parent / "visuals" / f"{visual['id']}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, "utf-8")
        assets.append({"visual_id": visual["id"], "requested_kind": visual["kind"], "kind": "ascii",
                       "source_path": f"visuals/{visual['id']}.txt", "rendered_path": f"visuals/{visual['id']}.txt",
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "ready",
                       "fallback_used": visual["kind"] != "ascii", "fallback_reason": "test primary unavailable"})
    return assets


def ready(repo, part_id, lesson=None):
    material = repo.stage_material(part_id, lesson or DEMO_LESSONS[1], "hash")
    return repo.ready_material(material["id"], assets_for(repo, material))


def test_draft_revisions_then_fixed_plan_and_finite_parts(repo):
    course = repo.save_draft(DEMO_PLAN)
    updated = copy.deepcopy(DEMO_PLAN)
    updated["title"] = "직접 수정한 제목"
    assert repo.save_draft(updated, course) == course
    assert repo.get_course(course)["plan_revision"] == 2
    assert repo.resolve(f"courses/{course}/plan-r1.json").exists()
    first = repo.start_course(course)
    assert len(repo.list_parts(course)) == 2
    assert repo.start_course(course) == first
    with pytest.raises(StorageError, match="고정"):
        repo.save_draft(DEMO_PLAN, course)
    assert repo.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", "a/../escape", "./escape", "a//b", "C:/x", "a\\b"])
def test_path_boundary_rejects_absolute_parent_and_ambiguous_paths(repo, path):
    with pytest.raises(StorageError):
        repo.resolve(path)


def test_path_boundary_rejects_links_even_links_inside_root(repo):
    repo.resolve("real").mkdir()
    (repo.data_root / "link").symlink_to(repo.data_root / "real", target_is_directory=True)
    with pytest.raises(StorageError, match="심볼릭"):
        repo.resolve("link/asset.txt")


def test_partial_or_mismatched_assets_cannot_replace_previous_ready_material(repo):
    _, part = start(repo)
    previous = ready(repo, part)
    candidate = repo.stage_material(part, DEMO_LESSONS[1], "new-hash")
    assets = assets_for(repo, candidate)
    with pytest.raises(StorageError, match="모든 필수"):
        repo.ready_material(candidate["id"], assets[:1])
    assets[0]["sha256"] = "0" * 64
    with pytest.raises(StorageError, match="해시"):
        repo.ready_material(candidate["id"], assets)
    assert repo.get_active_material(part)["id"] == previous["id"]
    assert repo.get_material(candidate["id"])["state"] == "staging"


def test_progress_resume_hash_integrity_and_unknown_quiz_notes(repo):
    _, part = start(repo)
    material = ready(repo, part)
    repo.save_progress(part, section_index=2, scroll_y=19.5, answers={"q1": "종이에 풀이"}, answers_revealed=True)
    reopened = Repository(repo.data_root)
    try:
        progress = reopened.get_progress(part)
        assert (progress["section_index"], progress["scroll_y"], progress["answers"], progress["answers_revealed"]) == (2, 19.5, {"q1": "종이에 풀이"}, True)
        assert reopened.valid_material(material["id"])
        with pytest.raises(StorageError, match="없는 문제"):
            reopened.save_progress(part, answers={"q999": "wrong lesson"})
        path = reopened.resolve(material["assets"][0]["rendered_path"])
        path.write_text("tampered", "utf-8")
        assert not reopened.valid_material(material["id"])
    finally:
        reopened.close()


def test_revision_swap_archives_old_notes_resets_changed_questions_keeps_completion(repo):
    course, part = start(repo)
    old = ready(repo, part)
    repo.save_progress(part, section_index=3, scroll_y=44, answers={"q1": "old note"}, answers_revealed=True)
    repo.choose_next(part)
    new_lesson = copy.deepcopy(DEMO_LESSONS[1])
    new_lesson["quizzes"][0]["question"] = "변경된 질문"
    ready(repo, part, new_lesson)
    progress = repo.get_progress(part)
    assert progress["status"] == "completed"
    assert progress["answers"] == {} and not progress["answers_revealed"]
    assert progress["section_index"] == progress["scroll_y"] == 0
    snapshot = repo.resolve(str(Path(old["lesson_path"]).parent / "progress-snapshot.json"))
    assert json.loads(snapshot.read_text("utf-8"))["answers"] == {"q1": "old note"}
    assert repo.get_course(course)["current_part_id"] != part


def test_same_questions_keep_notes_and_asset_retry_invalidates_only_pdf(repo):
    _, part = start(repo)
    ready(repo, part)
    repo.save_progress(part, answers={"q1": "keep me"}, answers_revealed=True)
    new = ready(repo, part)
    assert repo.get_progress(part)["answers"] == {"q1": "keep me"}
    base = repo.resolve(new["lesson_path"]).parent
    (base / "render-manifest.json").write_text("{}", "utf-8")
    before = repo.get_progress(part)
    repo.replace_asset(new["id"], new["assets"][0])
    assert not (base / "render-manifest.json").exists()
    assert repo.get_progress(part) == before


def test_quiz_skip_completes_finite_course_review_preserves_completion_and_followup_parent(repo):
    course, first = start(repo)
    second = repo.choose_next(first)
    assert second is not None
    assert repo.choose_next(second) is None
    assert repo.get_course(course)["status"] == "completed"
    assert repo.get_progress(first)["answers"] == {}
    assert not repo.get_progress(first)["answers_revealed"]
    repo.choose_next(first)  # Review navigation never reopens the finished course.
    assert repo.get_course(course)["status"] == "completed"
    assert repo.get_course(course)["current_part_id"] is None
    child = repo.save_draft(DEMO_PLAN, parent_course_id=course)
    assert child != course and repo.get_course(child)["parent_course_id"] == course
    assert repo.get_course(child)["status"] == "draft"


def test_reopen_marks_abandoned_job_interrupted_and_keeps_ready_material(repo):
    course, part = start(repo)
    material = ready(repo, part)
    repo.begin_job(course, part, "lesson")
    reopened = Repository(repo.data_root)
    try:
        assert reopened.list_jobs()[0]["status"] == "interrupted"
        assert reopened.get_active_material(part)["id"] == material["id"]
    finally:
        reopened.close()


def test_part_picker_jump_does_not_complete_unvisited_parts_and_next_uses_remaining_order(repo):
    plan = copy.deepcopy(DEMO_PLAN)
    plan["parts"] = [dict(copy.deepcopy(plan["parts"][0]), ordinal=i) for i in range(1, 5)]
    course = repo.save_draft(plan)
    repo.start_course(course)
    first, second, third, fourth = [part["id"] for part in repo.list_parts(course)]

    repo.select_part(fourth)
    assert repo.choose_next(fourth) == first
    assert repo.get_course(course)["status"] == "active"
    assert repo.get_progress(first)["status"] == "not_started"
    assert repo.get_progress(fourth)["status"] == "completed"

    repo.select_part(second)
    assert repo.choose_next(second) == third  # Later incomplete parts come first.
    assert repo.choose_next(third) == first  # Completed fourth is skipped; wrap.
    assert repo.choose_next(first) is None
    assert repo.get_course(course)["status"] == "completed"
    assert all(repo.get_progress(part)["status"] == "completed" for part in (first, second, third, fourth))
    assert all(not repo.get_progress(part)["answers_revealed"] for part in (first, second, third, fourth))

    repo.select_part(first)
    assert repo.choose_next(first) == second  # Finished-course review keeps ordinal navigation.
    assert repo.get_course(course)["status"] == "completed"
