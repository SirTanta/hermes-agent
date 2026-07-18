"""
Regression suite for kanban clean-exit / archived-repromotion bugs.

Bug 1 — double-count (t_9c5ca2dd):
  detect_crashed_workers incremented consecutive_failures inside the
  UPDATE then called _record_task_failure which incremented it AGAIN,
  causing the circuit breaker to trip one occurrence early.

  Fix: remove the pre-increment; _record_task_failure is the single
  counter-increment site.  Protocol violations use failure_limit=1 so
  the breaker still trips on the first clean exit.

Bug 2 — archived re-promotion (t_9c5ca2dd):
  archive_task() called recompute_ready() to unblock children, but
  recompute_ready's SELECT ... WHERE status IN ('todo', 'blocked')
  included archived tasks, causing an archived task to be promoted
  back to 'ready' after being deliberately archived.

  Fix: skip 'archived' and 'done' tasks in recompute_ready.
"""

from __future__ import annotations

import os
import sqlite3
import time
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def board_path(tmp_path):
    return str(tmp_path / "board.db")


@pytest.fixture
def board(board_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    board_name = "testboard"
    kb.connect(board=board_name, db_path=Path(board_path))
    monkeypatch.setattr(
        "hermes_cli.kanban_db.get_current_board", lambda: board_name
    )
    conn = kb.connect(board=board_name, db_path=Path(board_path))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_status(conn, task_id):
    row = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else {"status": None, "consecutive_failures": None}


def _register_exit(conn, pid, exit_code):
    """Fake a worker exit in the reap registry.

    ``exit_code`` is the raw exit code (0 for clean, 1+ for error).
    The registry stores it as a waitpid status word (code << 8).
    This matches how the existing test suite fakes exits.
    """
    from hermes_cli.kanban_db import _record_worker_exit
    _record_worker_exit(pid, exit_code << 8)


# ---------------------------------------------------------------------------
# Bug 1 — no double-count on clean_exit
# ---------------------------------------------------------------------------

class TestCleanExitNoDoubleCount:
    """verify consecutive_failures increments by exactly 1 per clean exit."""

    def test_clean_exit_increments_by_one_not_two(self, board, monkeypatch):
        """
        Simulate a worker that exits cleanly (rc=0) without calling
        kanban_complete / kanban_block.  consecutive_failures must go from
        0 to 1 on the FIRST occurrence, and the breaker must trip (task
        goes blocked) immediately because effective_limit=1 for protocol
        violations.

        Before the fix: failures = row['consecutive_failures'] + 1 in the
        UPDATE (stored as 1) then _record_task_failure incremented again
        (row read as 1, stored as 2) → breaker tripped on second occurrence.
        After fix: single increment → breaker trips on first occurrence.
        """
        from hermes_cli import kanban_db as kb

        # Create a task in 'running' with a fake PID and claim_lock
        task_id = kb.create_task(
            board, title="clean-exit test", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        pid = 99999

        # Use actual host prefix so detect_crashed_workers host_local check passes
        import socket
        host = socket.gethostname()
        lock = f"{host}:{pid}"

        # Put it in running state with our fake pid
        # Use current time so grace-period check passes (delta >> 30s grace)
        now = int(time.time())
        # Set started_at to 60s ago so task is outside the 30s grace window
        started_at = now - 60
        conn = board
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=?, consecutive_failures=0 WHERE id=?",
            (pid, lock, started_at, task_id),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, started_at) "
            "VALUES (?, 'running', ?, ?)",
            (task_id, lock, started_at),
        )
        conn.commit()

        # Verify starting state
        assert _raw_status(conn, task_id)["consecutive_failures"] == 0

        # Fake a clean exit (WIFEXITED, status=0)
        _register_exit(conn, pid, 0)

        # Run detect_crashed_workers
        crashed = kb.detect_crashed_workers(conn)

        # Task should be auto-blocked (protocol violation trips breaker immediately)
        assert task_id in crashed or task_id in getattr(
            kb.detect_crashed_workers, "_last_auto_blocked", []
        )
        final = _raw_status(conn, task_id)
        assert final["status"] == "blocked"

        # critical assertion: counter went 0 → 1, not 0 → 2
        assert final["consecutive_failures"] == 1, (
            f"expected 1, got {final['consecutive_failures']} — "
            "double-count detected"
        )


# ---------------------------------------------------------------------------
# Bug 2 — archived tasks must not be re-promoted by recompute_ready
# ---------------------------------------------------------------------------

class TestArchivedNoRepromotion:
    """verify recompute_ready skips archived and done tasks."""

    def test_recompute_ready_skips_archived(self, board):
        """
        A task in 'archived' status must not be re-promoted to 'ready'
        even when recompute_ready is called (e.g. after archiving a parent).
        """
        from hermes_cli import kanban_db as kb

        # Parent done + child archived → recompute_ready must NOT promote child
        parent_id = kb.create_task(
            board, title="parent", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        child_id = kb.create_task(
            board, title="child", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        kb.link_tasks(board, parent_id, child_id)

        conn = board
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_id,))
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (child_id,))
        conn.commit()

        # Run recompute_ready — should promote parent children, not the archived child
        promoted = kb.recompute_ready(conn)

        # Child must still be archived
        final = _raw_status(conn, child_id)
        assert final["status"] == "archived", (
            f"expected archived, got {final['status']} — "
            "archived task was re-promoted"
        )

    def test_recompute_ready_skips_done(self, board):
        """
        A task in 'done' status must not be re-promoted either.
        'done' is also terminal — work is finished.
        """
        from hermes_cli import kanban_db as kb

        parent_id = kb.create_task(
            board, title="parent", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        # Create child already in 'done' (manually set after create)
        child_id = kb.create_task(
            board, title="child", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        kb.link_tasks(board, parent_id, child_id)

        conn = board
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_id,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child_id,))
        conn.commit()

        promoted = kb.recompute_ready(conn)

        final = _raw_status(conn, child_id)
        assert final["status"] == "done", (
            f"expected done, got {final['status']} — "
            "done task was re-promoted"
        )

    def test_archive_task_does_not_repromote_itself(self, board):
        """
        End-to-end: archiving a blocked task and then calling archive_task
        (which internally calls recompute_ready) must not re-promote
        the archived task.
        """
        from hermes_cli import kanban_db as kb

        task_id = kb.create_task(
            board, title="archive-repromote test", assignee="worker-a",
            created_by="test", workspace_kind="scratch",
        )
        conn = board
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        conn.commit()

        # Archive the task — internally calls recompute_ready
        ok = kb.archive_task(conn, task_id)
        assert ok, "archive_task returned False"

        final = _raw_status(conn, task_id)
        assert final["status"] == "archived", (
            f"expected archived, got {final['status']} — "
            "archive_task failed to keep task archived after recompute_ready"
        )
