"""Tests for kanban lifecycle plugin hooks.

Verifies that claim/complete/block transitions fire the
kanban_task_claimed / kanban_task_completed / kanban_task_blocked plugin
hooks AFTER the board DB change is committed, with the documented kwargs,
and that a misbehaving hook callback never breaks the transition.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Register capturing callbacks for the three kanban lifecycle hooks.

    Patches the plugin manager's _hooks dict directly (the same registry
    invoke_hook reads) and restores it afterward.
    """
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"):
        mgr._hooks.setdefault(hook, []).append(
            lambda _h=hook, **kw: events.append((_h, kw))
        )
    try:
        yield events
    finally:
        mgr._hooks = saved


def test_hooks_are_registered_as_valid():
    """The three lifecycle hook names are part of VALID_HOOKS."""
    assert "kanban_task_claimed" in VALID_HOOKS
    assert "kanban_task_completed" in VALID_HOOKS
    assert "kanban_task_blocked" in VALID_HOOKS


def test_claim_fires_hook(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_claimed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert "profile_name" in kw
    assert kw["run_id"] is not None


def test_complete_fires_hook_with_summary(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="all done")
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_completed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["summary"] == "all done"
    assert kw["assignee"] == "worker"


def test_block_fires_hook_with_reason(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="needs human")
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_blocked"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["reason"] == "needs human"


def test_no_hook_on_failed_transition(kanban_home, captured_hooks):
    """complete_task on an unclaimed/nonexistent task fires no hook."""
    conn = kb.connect()
    try:
        # Completing a task that doesn't exist returns False without firing.
        assert kb.complete_task(conn, "t_doesnotexist", summary="x") is False
    finally:
        conn.close()
    assert [e for e in captured_hooks if e[0] == "kanban_task_completed"] == []


def test_misbehaving_hook_does_not_break_transition(kanban_home, monkeypatch):
    """A hook callback that raises must not break the board transition."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    mgr._hooks.setdefault("kanban_task_completed", []).append(_boom)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            # Despite the raising hook, completion succeeds and persists.
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


def _hook_report_row(transition: str, hook: str, task_id: str, fields: dict, *, source: str) -> dict:
    return {
        "transition": transition,
        "hook": hook,
        "task_id": task_id,
        "board": fields.get("board"),
        "assignee": fields.get("assignee"),
        "reason": fields.get("reason"),
        "summary": fields.get("summary"),
        "source": source,
    }


def test_full_lifecycle_hook_coverage_end_to_end(kanban_home, monkeypatch):
    captured: list[dict] = []

    def sink(event: str, task_id: str, **fields):
        captured.append({"hook": event, "task_id": task_id, "fields": fields})

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", sink)

    report: list[dict] = []
    board_slug = f"pytest-qa-hook-{os.getpid()}"
    monkeypatch.setenv("HERMES_HOME", "/home/mulagent/.hermes")
    monkeypatch.setattr(Path, "home", lambda: Path("/home/mulagent"))
    db_path = Path("/home/mulagent/.hermes/kanban/boards") / board_slug / "kanban.db"
    if db_path.parent.exists():
        shutil.rmtree(db_path.parent)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board_slug)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(db_path.parent / "workspaces" / "pytest"))
    kb.init_db()
    kb.set_current_board(board_slug)

    conn = kb.connect()
    try:
        board = board_slug
        db_path = kb.kanban_db_path()

        main_id = kb.create_task(
            conn,
            title="full lifecycle",
            body="exercise every lifecycle hook",
            assignee="worker",
            triage=True,
        )
        created = [e for e in captured if e["hook"] == "kanban_task_created" and e["task_id"] == main_id]
        assert len(created) == 1, json.dumps(captured, indent=2)
        created = created[0]
        assert created["fields"]["board"] == board
        assert created["fields"]["assignee"] == "worker"
        report.append(_hook_report_row("created/triage", "kanban_task_created", main_id, created["fields"], source="sink"))

        assert kb.specify_triage_task(
            conn,
            main_id,
            title="full lifecycle spec",
            body="specified",
            assignee="worker",
            author="specifier",
        )
        todo = next(e for e in captured if e["hook"] == "kanban_task_todo" and e["task_id"] == main_id)
        assert todo["fields"]["board"] == board
        assert todo["fields"]["assignee"] == "worker"
        report.append(_hook_report_row("triage -> todo", "kanban_task_todo", main_id, todo["fields"], source="sink"))

        assert kb.schedule_task(conn, main_id, reason="time box")
        scheduled = next(e for e in captured if e["hook"] == "kanban_task_scheduled" and e["task_id"] == main_id)
        assert scheduled["fields"]["board"] == board
        assert scheduled["fields"]["assignee"] == "worker"
        assert scheduled["fields"]["reason"] == "time box"
        report.append(_hook_report_row("todo -> scheduled", "kanban_task_scheduled", main_id, scheduled["fields"], source="sink"))

        assert kb.unblock_task(conn, main_id)
        ready = next(e for e in captured if e["hook"] == "kanban_task_ready" and e["task_id"] == main_id)
        assert ready["fields"]["board"] == board
        assert ready["fields"]["assignee"] == "worker"
        report.append(_hook_report_row("scheduled -> ready", "kanban_task_ready", main_id, ready["fields"], source="sink"))

        claimed = kb.claim_task(conn, main_id)
        assert claimed is not None
        claimed_evt = next(e for e in captured if e["hook"] == "kanban_task_claimed" and e["task_id"] == main_id)
        assert claimed_evt["fields"]["board"] == board
        assert claimed_evt["fields"]["assignee"] == "worker"
        assert claimed_evt["fields"]["run_id"] is not None
        report.append(_hook_report_row("ready -> running", "kanban_task_claimed", main_id, claimed_evt["fields"], source="sink"))

        conn.commit()
        conn.close()
        conn = kb.connect()

        qa_submit = shutil.which("hermes-qa-submit")
        assert qa_submit, "hermes-qa-submit must be on PATH for the QA handoff path"
        env = os.environ.copy()
        env["HERMES_KANBAN_DB"] = str(db_path)
        env["HERMES_HOME"] = str(kanban_home)
        env["HERMES_KANBAN_BOARD"] = board
        result = subprocess.run(
            [qa_submit, main_id, "evidence: full lifecycle"],
            check=False,
            text=True,
            capture_output=True,
            env=env,
            cwd=str(kanban_home),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "queued for Motoko QA" in result.stdout
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?",
            (main_id,),
        ).fetchone()
        assert row["status"] == "qa_review"
        assert row["assignee"] == "motoko"

        qa_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'lifecycle_hook' ORDER BY id DESC LIMIT 1",
            (main_id,),
        ).fetchone()
        assert qa_event is not None
        qa_payload = json.loads(qa_event["payload"])
        assert qa_payload["hook"] == "kanban_task_qa_submitted"
        assert qa_payload["board"] == board
        assert qa_payload["assignee"] == "motoko"
        assert qa_payload["context"]["submitted_by"] == "worker"
        assert qa_payload["context"]["evidence"] == "evidence: full lifecycle"
        report.append({
            "transition": "running -> qa_submitted",
            "hook": qa_payload["hook"],
            "task_id": main_id,
            "board": qa_payload["board"],
            "assignee": qa_payload["assignee"],
            "reason": qa_payload["reason"],
            "summary": None,
            "source": "db",
        })

        claimed = kb.claim_qa_review_task(conn, main_id, claimer="motoko")
        assert claimed is not None
        motoko_claim = next(
            e for e in captured
            if e["hook"] == "kanban_task_claimed" and e["task_id"] == main_id
        )
        assert motoko_claim["fields"]["board"] == board
        assert motoko_claim["fields"]["run_id"] is not None
        report.append(_hook_report_row("qa_submitted -> qa_approved", "kanban_task_claimed", main_id, motoko_claim["fields"], source="sink"))

        assert kb.complete_task(conn, main_id, summary="QA approved")
        completed = next(
            e for e in captured
            if e["hook"] == "kanban_task_completed" and e["task_id"] == main_id and e["fields"].get("assignee") == "motoko"
        )
        assert completed["fields"]["board"] == board
        assert completed["fields"]["summary"] == "QA approved"
        report.append(_hook_report_row("qa_approved -> done", "kanban_task_completed", main_id, completed["fields"], source="sink"))

        blocked_id = kb.create_task(conn, title="blocked branch", assignee="worker")
        assert kb.block_task(conn, blocked_id, reason="needs input", kind="needs_input")
        blocked = next(e for e in captured if e["hook"] == "kanban_task_blocked" and e["task_id"] == blocked_id)
        assert blocked["fields"]["board"] == board
        assert blocked["fields"]["assignee"] == "worker"
        assert blocked["fields"]["reason"] == "needs input"
        report.append(_hook_report_row("running -> blocked", "kanban_task_blocked", blocked_id, blocked["fields"], source="sink"))

        archived_id = kb.create_task(conn, title="archived branch", assignee="worker")
        assert kb.archive_task(conn, archived_id)
        archived = next(e for e in captured if e["hook"] == "kanban_task_archived" and e["task_id"] == archived_id)
        assert archived["fields"]["board"] == board
        assert archived["fields"]["assignee"] == "worker"
        report.append(_hook_report_row("running -> archived", "kanban_task_archived", archived_id, archived["fields"], source="sink"))

        expected = [
            ("created/triage", "kanban_task_created", main_id),
            ("triage -> todo", "kanban_task_todo", main_id),
            ("todo -> scheduled", "kanban_task_scheduled", main_id),
            ("scheduled -> ready", "kanban_task_ready", main_id),
            ("ready -> running", "kanban_task_claimed", main_id),
            ("running -> qa_submitted", "kanban_task_qa_submitted", main_id),
            ("qa_submitted -> qa_approved", "kanban_task_claimed", main_id),
            ("qa_approved -> done", "kanban_task_completed", main_id),
            ("running -> blocked", "kanban_task_blocked", blocked_id),
            ("running -> archived", "kanban_task_archived", archived_id),
        ]
        for transition, hook, task_id in expected:
            if hook == "kanban_task_qa_submitted":
                matches = [r for r in report if r["transition"] == transition and r["hook"] == hook and r["task_id"] == task_id]
            else:
                matches = [r for r in report if r["transition"] == transition and r["hook"] == hook and r["task_id"] == task_id]
            assert len(matches) == 1, json.dumps(report, indent=2)
            row = matches[0]
            assert row["board"] == board, json.dumps(report, indent=2)
            assert row["assignee"], json.dumps(report, indent=2)
            if transition in {"todo -> scheduled", "running -> blocked", "running -> qa_submitted"}:
                assert row["reason"], json.dumps(report, indent=2)
            if transition in {"qa_approved -> done"}:
                assert row["summary"] == "QA approved", json.dumps(report, indent=2)
    finally:
        conn.close()

    print(json.dumps(report, indent=2))
