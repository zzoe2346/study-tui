"""Exercise the CLI boundary using real subprocesses and controlled JSONL."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from study_tui.codex import CodexGenerator, GenerationFailure
from study_tui.demo import DEMO_LESSONS, DEMO_PLAN
from study_tui.models import LESSON_SCHEMA


FAKE_SOURCE = r'''
import json, os, pathlib, signal, subprocess, sys, time
args = sys.argv[1:]
base = pathlib.Path(__file__).parent
with (base / "invocations.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "api_key_present": "OPENAI_API_KEY" in os.environ,
                          "codex_key_present": "CODEX_API_KEY" in os.environ}) + "\n")
if args == ["login", "status"]:
    print(os.environ.get("FAKE_AUTH", "Logged in using ChatGPT"))
    sys.exit(0)
if args == ["--version"]:
    print("codex-cli test.0")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
job = pathlib.Path(args[args.index("--cd") + 1])
final = pathlib.Path(args[args.index("--output-last-message") + 1])
prompt = sys.stdin.read()
(base / "prompt.txt").write_text(prompt)
if mode in ("quota", "auth", "network"):
    messages = {"quota": "429 usage limit SECRET_TOKEN", "auth": "401 unauthorized SECRET_TOKEN",
                "network": "DNS network disconnected SECRET_TOKEN"}
    print(json.dumps({"type": "turn.failed", "error": {"message": messages[mode]}}), flush=True)
    sys.exit(1)
if mode in ("block", "orphan"):
    heartbeat = str(base / "heartbeat")
    child_code = "import pathlib,time; p=pathlib.Path(" + repr(heartbeat) + ");\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.03)"
    child = subprocess.Popen([sys.executable, "-c", child_code])
    (base / "pids.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid}))
    print(json.dumps({"type": "turn.started"}), flush=True)
    if mode == "orphan":
        sys.exit(0)
    while True:
        time.sleep(0.05)
if mode == "scalar_event":
    print("null", flush=True)
    print("[]", flush=True)
print("non-json CLI noise", flush=True)
print(json.dumps({"type": "future.unknown", "safe": True}), flush=True)
print(json.dumps({"type": "thread.started", "thread_id": "fixture"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if mode == "error_then_completed":
    print(json.dumps({"type": "error", "message": "network generation failed"}), flush=True)
if mode == "invalid_json":
    final.write_text("{ unfinished")
elif mode == "schema":
    final.write_text(json.dumps({"title": "schema-invalid"}))
else:
    final.write_text((base / "payload.json").read_text())
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "not the final artifact"}}), flush=True)
if mode != "missing_completed":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    binary = tmp_path / "codex-fixture"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_SOURCE)
    binary.chmod(0o755)
    (tmp_path / "payload.json").write_text(json.dumps(DEMO_PLAN, ensure_ascii=False))
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("CODEX_API_KEY", "not-a-real-key")
    monkeypatch.setenv("FAKE_AUTH", "Logged in using ChatGPT")
    monkeypatch.setenv("FAKE_MODE", "ok")
    return binary


def invocations(fake_cli):
    return [json.loads(line) for line in (fake_cli.parent / "invocations.jsonl").read_text().splitlines()]


async def wait_pids(fake_cli):
    path = fake_cli.parent / "pids.json"
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.02)
    return json.loads(path.read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def assert_stopped(pids, fake_cli):
    # A killed orphan may briefly remain a zombie; prove it no longer executes
    # through its heartbeat, without privileged process inventory tools.
    assert not alive(pids["parent"])
    heartbeat = fake_cli.parent / "heartbeat"
    before = heartbeat.read_text() if heartbeat.exists() else None
    await asyncio.sleep(0.2)
    after = heartbeat.read_text() if heartbeat.exists() else None
    assert after == before, "Codex child kept executing after cancellation/timeout"


def cleanup(pids):
    for pid in pids.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_real_subprocess_final_file_not_jsonl_and_subscription_environment(fake_cli, tmp_path):
    messages = []
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli), on_event=messages.append)
    topic = "B-tree; $(touch forbidden) `nothing` 한글"
    plan = await generator.generate_plan(topic)
    assert plan == DEMO_PLAN
    calls = invocations(fake_cli)
    assert [call["args"] for call in calls[:2]] == [["login", "status"], ["--version"]]
    assert calls[2]["args"][0] == "exec"
    assert '--sandbox' in calls[2]["args"]
    assert calls[2]["args"][calls[2]["args"].index('--sandbox') + 1] == 'read-only'
    assert all(not call["api_key_present"] and not call["codex_key_present"] for call in calls)
    assert topic in (fake_cli.parent / "prompt.txt").read_text()
    assert not (fake_cli.parent / "forbidden").exists()
    assert messages
    diagnostics = json.loads(next((tmp_path / "data" / "jobs").glob("*/diagnostics.json")).read_text())
    assert diagnostics["status"] == "succeeded"
    assert "future.unknown" in diagnostics["event_types"]
    assert "prompt" not in diagnostics
    assert "output_tokens" not in diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["Logged in using an API key", "Not logged in", "Logged in using something else"])
async def test_auth_rejected_before_exec_without_forcing_logout(fake_cli, tmp_path, monkeypatch, auth):
    monkeypatch.setenv("FAKE_AUTH", auth)
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    with pytest.raises(GenerationFailure) as caught:
        await generator.generate_plan("인덱스")
    assert caught.value.category == "auth"
    assert not caught.value.retryable
    assert [call["args"] for call in invocations(fake_cli)] == [["login", "status"]]
    assert not (tmp_path / "data" / "jobs").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quota", "auth", "network", "invalid_json", "missing_completed"])
async def test_errors_fail_once_and_diagnostics_exclude_raw_secret(fake_cli, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_MODE", mode)
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    with pytest.raises(GenerationFailure) as caught:
        await generator.generate_plan("인덱스")
    expected = {"invalid_json": "schema", "missing_completed": "cli"}.get(mode, mode)
    assert caught.value.category == expected
    assert "SECRET_TOKEN" not in str(caught.value)
    assert sum(call["args"][0] == "exec" for call in invocations(fake_cli)) == 1
    diagnostics = next((tmp_path / "data" / "jobs").glob("*/diagnostics.json")).read_text()
    assert "SECRET_TOKEN" not in diagnostics
    assert '"status": "succeeded"' not in diagnostics


@pytest.mark.asyncio
async def test_schema_failure_normalized_at_adapter_boundary(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "schema")
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    with pytest.raises(GenerationFailure) as caught:
        await generator.generate_plan("인덱스")
    assert caught.value.category == "schema"
    diagnostics = json.loads(next((tmp_path / "data" / "jobs").glob("*/diagnostics.json")).read_text())
    assert diagnostics["status"] == "schema"


@pytest.mark.asyncio
async def test_error_event_disallows_later_completed_result(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error_then_completed")
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    with pytest.raises(GenerationFailure) as caught:
        await generator.generate_plan("인덱스")
    assert caught.value.category == "network"


@pytest.mark.asyncio
async def test_nonobject_jsonl_is_ignored_as_non_event(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "scalar_event")
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    assert await generator.generate_plan("인덱스") == DEMO_PLAN


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "url", "reference"])
async def test_cli_transport_schema_retains_local_url_and_reference_validation(fake_cli, tmp_path, invalid):
    original_schema = copy.deepcopy(LESSON_SCHEMA)
    lesson = copy.deepcopy(DEMO_LESSONS[1])
    if invalid == "url":
        lesson["sources"][0]["url"] = "ftp://example.org/source"
    elif invalid == "reference":
        lesson["sections"][0]["source_ids"] = ["s999"]
    (fake_cli.parent / "payload.json").write_text(json.dumps(lesson, ensure_ascii=False))
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    if invalid:
        with pytest.raises(GenerationFailure) as caught:
            await generator.generate_lesson(DEMO_PLAN, 1)
        assert caught.value.category == "schema"
    else:
        assert await generator.generate_lesson(DEMO_PLAN, 1) == lesson
    transport = json.loads(next((tmp_path / "data" / "jobs").glob("*/schema.json")).read_text())
    assert transport["$defs"]["source"]["properties"]["url"].get("format") is None
    assert LESSON_SCHEMA == original_schema
    assert LESSON_SCHEMA["$defs"]["source"]["properties"]["url"]["format"] == "uri"


@pytest.mark.asyncio
async def test_cancel_cleans_up_child_process_group(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "block")
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli))
    task = asyncio.create_task(generator.generate_plan("인덱스"))
    pids = await wait_pids(fake_cli)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_stopped(pids, fake_cli)
        diagnostics = json.loads(next((tmp_path / "data" / "jobs").glob("*/diagnostics.json")).read_text())
        assert diagnostics["status"] == "cancelled"
    finally:
        cleanup(pids)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["block", "orphan"])
async def test_timeout_cleans_child_even_if_cli_parent_exited(fake_cli, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_MODE", mode)
    generator = CodexGenerator(tmp_path / "data", executable=str(fake_cli), plan_timeout=0.35)
    task = asyncio.create_task(generator.generate_plan("인덱스"))
    pids = await wait_pids(fake_cli)
    try:
        with pytest.raises(GenerationFailure) as caught:
            await task
        assert caught.value.category == "timeout"
        await assert_stopped(pids, fake_cli)
    finally:
        cleanup(pids)
