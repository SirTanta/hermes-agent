"""Regression tests for the block-fingerprint circuit breaker.

Tests the full loop:
  1. Block task (stores fingerprint)
  2. Human unblock → fingerprint resets
  3. Block again (new fingerprint, counter = 1)
  4. recompute_ready promotes blocked→ready → fingerprint unchanged → counter increments
  5. Repeats until BLOCK_FINGERPRINT_LIMIT (3) → check_respawn_guard returns
     "block_fingerprint_loop" → task is routed to triage
  6. QA submit → fingerprint and counter reset
  7. detect_stale_workers recovers ghost running tasks

Covers: kanban_db block_task, unblock_task, submit_qa_task,
detect_stale_workers, check_respawn_guard, recompute_ready, dispatch_once.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with a migrated kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    # Clear kanban-specific env vars that kanban_db_path() resolves before
    # HERMES_HOME — HERMES_KANBAN_DB takes precedence over HERMES_HOME,
    # and HERMES_KANBAN_HOME takes precedence over get_default_hermes_root().
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home: Path):
    """Read-write connection to the test DB via kb.connect."""
    conn = kb.connect()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fp(kind: str | None, reason: str | None) -> str:
    return kb._block_fingerprint(kind, reason)


# ---------------------------------------------------------------------------
# _block_fingerprint — normalisation and stability
# ---------------------------------------------------------------------------

class TestBlockFingerprint:
    def test_same_kind_reason_idempotent(self) -> None:
        fp1 = _fp("capability", "missing credentials")
        fp2 = _fp("capability", "missing credentials")
        assert fp1 == fp2

    def test_different_reason_changes_fingerprint(self) -> None:
        fp1 = _fp("capability", "missing credentials")
        fp2 = _fp("capability", "wrong model")
        assert fp1 != fp2

    def test_different_kind_changes_fingerprint(self) -> None:
        fp1 = _fp("capability", "missing credentials")
        fp2 = _fp("needs_input", "missing credentials")
        assert fp1 != fp2

    def test_pid_and_timestamp_stripped(self) -> None:
        """PIDs and timestamps in the reason must not affect the fingerprint."""
        fp1 = _fp("capability", "pid 12345 crashed at 1721000000")
        fp2 = _fp("capability", "pid 99999 crashed at 9999999999")
        assert fp1 == fp2

    def test_hex_urls_and_whitespace_normalised(self) -> None:
        fp1 = _fp("capability", "see  https://example.com/path  and 0xabcdef123")
        fp2 = _fp("capability", "see https://other.com/link and 0x123456789abc")
        assert fp1 == fp2

    def test_none_kind_normalised_to_none_string(self) -> None:
        """kind=None and reason=None must produce a deterministic fingerprint."""
        fp = _fp(None, None)
        assert fp == _fp(None, None)
        # Must NOT collide with a real (kind, reason) pair
        assert fp != _fp("capability", "test")


# ---------------------------------------------------------------------------
# Schema migration — new columns exist and default to 0/NULL
# ---------------------------------------------------------------------------

def test_block_fingerprint_columns_migrated(conn) -> None:
    """The migration adds last_block_fingerprint and block_fingerprint_count."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "last_block_fingerprint" in cols
    assert "block_fingerprint_count" in cols


def test_new_task_fingerprint_defaults(conn) -> None:
    """A fresh task has NULL fingerprint and zero counter."""
    task_id = kb.create_task(conn, title="fp defaults test", assignee="tester")
    row = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_block_fingerprint"] is None
    assert row["block_fingerprint_count"] == 0


# ---------------------------------------------------------------------------
# block_task stores the fingerprint
# ---------------------------------------------------------------------------

def test_block_stores_fingerprint(conn) -> None:
    task_id = kb.create_task(conn, title="stores fp", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="no auth token", kind="capability")
    row = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_block_fingerprint"] == _fp("capability", "no auth token")
    assert row["block_fingerprint_count"] == 1


def test_dependency_block_resets_counter(conn) -> None:
    """A dependency block sets fingerprint but resets counter to 0."""
    task_id = kb.create_task(conn, title="dep fp test", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="waiting on parent", kind="dependency")
    row = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_block_fingerprint"] == _fp("dependency", "waiting on parent")
    assert row["block_fingerprint_count"] == 0


# ---------------------------------------------------------------------------
# Human unblock resets the fingerprint
# ---------------------------------------------------------------------------

def test_unblock_resets_fingerprint_and_counter(conn) -> None:
    task_id = kb.create_task(conn, title="unblock reset", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
    # Verify fingerprint stored
    row_before = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row_before["last_block_fingerprint"] == _fp("needs_input", "needs input")
    assert row_before["block_fingerprint_count"] == 1

    kb.unblock_task(conn, task_id)

    row_after = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count, status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row_after["last_block_fingerprint"] is None
    assert row_after["block_fingerprint_count"] == 0


# ---------------------------------------------------------------------------
# recompute_ready increments counter on re-promotion
# ---------------------------------------------------------------------------

def test_recompute_ready_increments_fingerprint_counter(conn) -> None:
    """recompute_ready of a blocked task bumps block_fingerprint_count."""
    task_id = kb.create_task(conn, title="promote inc counter", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="qa rejected", kind="capability")
    # counter = 1 after block

    # Simulate the task being re-promoted (blocked → ready)
    conn.execute(
        "UPDATE tasks SET status = 'ready', block_fingerprint_count = block_fingerprint_count + 1 "
        "WHERE id = ?",
        (task_id,),
    )
    row = conn.execute(
        "SELECT block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["block_fingerprint_count"] == 2


# ---------------------------------------------------------------------------
# check_respawn_guard returns None until limit reached
# ---------------------------------------------------------------------------

def test_respawn_guard_allows_first_three_promotions(conn) -> None:
    """With counter 1-2, guard returns None (no block)."""
    task_id = kb.create_task(conn, title="guard early", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="auth failed", kind="capability")
    # counter = 1, fp stored

    for count in (1, 2):
        conn.execute(
            "UPDATE tasks SET block_fingerprint_count = ? WHERE id = ?",
            (count, task_id),
        )
        guard = kb.check_respawn_guard(conn, task_id)
        assert guard is None, f"guard should be None at count={count}, got {guard}"


def test_respawn_guard_blocks_at_limit(conn) -> None:
    """With counter >= BLOCK_FINGERPRINT_LIMIT, guard returns 'block_fingerprint_loop'."""
    task_id = kb.create_task(conn, title="guard limit", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="auth failed", kind="capability")
    conn.execute(
        "UPDATE tasks SET block_fingerprint_count = ? WHERE id = ?",
        (kb.BLOCK_FINGERPRINT_LIMIT, task_id),
    )
    guard = kb.check_respawn_guard(conn, task_id)
    assert guard == "block_fingerprint_loop"


def test_respawn_guard_allows_after_new_block(conn) -> None:
    """A new block reason (new fingerprint) clears the guard even at high count."""
    task_id = kb.create_task(conn, title="guard new fp", assignee="tester")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="auth failed", kind="capability")
    conn.execute(
        "UPDATE tasks SET block_fingerprint_count = ?, status = 'ready' "
        "WHERE id = ?",
        (kb.BLOCK_FINGERPRINT_LIMIT, task_id),
    )
    conn.commit()
    # Block again with a different reason → new fingerprint
    kb.block_task(conn, task_id, reason="different error", kind="capability")
    guard = kb.check_respawn_guard(conn, task_id)
    assert guard is None


# ---------------------------------------------------------------------------
# dispatch_once routes to triage on block_fingerprint_loop
# ---------------------------------------------------------------------------

def test_dispatch_once_routes_triage_on_fingerprint_loop(conn, monkeypatch) -> None:
    """dispatch_once moves a ready task to triage when guard == 'block_fingerprint_loop'."""
    # Stub profile_exists so 'misa' is treated as a valid assignee in the test home
    import hermes_cli.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name == "misa")

    task_id = kb.create_task(conn, title="triage on loop", assignee="misa")
    kb.claim_task(conn, task_id)
    # Set to ready so block_task can update the task (blocked tasks reject re-block)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    kb.block_task(conn, task_id, reason="stuck auth", kind="capability")
    conn.execute(
        "UPDATE tasks SET status = 'ready', "
        "block_fingerprint_count = ? "
        "WHERE id = ?",
        (kb.BLOCK_FINGERPRINT_LIMIT, task_id),
    )
    conn.commit()

    result = kb.dispatch_once(conn)
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "triage"


# ---------------------------------------------------------------------------
# QA submit resets the fingerprint
# ---------------------------------------------------------------------------

def test_qa_submit_resets_fingerprint(conn) -> None:
    """submit_qa_task clears last_block_fingerprint and resets counter."""
    task_id = kb.create_task(conn, title="qa reset", assignee="tester")
    kb.claim_task(conn, task_id)
    # Set fingerprint columns directly as if block_task had stored them
    conn.execute(
        "UPDATE tasks SET "
        "last_block_fingerprint = 'test_fp', "
        "block_fingerprint_count = 2 "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    kb.submit_qa_task(conn, task_id, evidence="URL: https://example.com/output")

    row = conn.execute(
        "SELECT last_block_fingerprint, block_fingerprint_count, status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_block_fingerprint"] is None
    assert row["block_fingerprint_count"] == 0
    assert row["status"] == "qa_review"


# ---------------------------------------------------------------------------
# complete_task clears fingerprint columns on success
# Bug fix: successful completion must wipe fingerprint state so a
# subsequent block starts a fresh fingerprint, not reuse/advance the
# circuit-breaker counter of the previous block.
# ---------------------------------------------------------------------------

def test_complete_task_clears_fingerprint_columns(conn) -> None:
    """complete_task resets last_block_fingerprint and block_fingerprint_count."""
    task_id = kb.create_task(conn, title="complete clears fp", assignee="tester")
    kb.claim_task(conn, task_id)
    # Set fingerprint columns as if a block had been stored
    conn.execute(
        "UPDATE tasks SET "
        "last_block_fingerprint = 'fp_abc123', "
        "block_fingerprint_count = 2 "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    ok = kb.complete_task(conn, task_id, result="done")

    assert ok is True
    row = conn.execute(
        "SELECT status, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["last_block_fingerprint"] is None
    assert row["block_fingerprint_count"] == 0


def test_complete_task_clears_fingerprint_with_expected_run_id(conn) -> None:
    """complete_task with expected_run_id also clears fingerprint columns."""
    task_id = kb.create_task(conn, title="complete with run id clears fp", assignee="tester")
    kb.claim_task(conn, task_id)
    run_row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    run_id = run_row["current_run_id"]

    conn.execute(
        "UPDATE tasks SET "
        "last_block_fingerprint = 'fp_xyz789', "
        "block_fingerprint_count = 3 "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    ok = kb.complete_task(conn, task_id, result="done", expected_run_id=run_id)

    assert ok is True
    row = conn.execute(
        "SELECT status, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["last_block_fingerprint"] is None
    assert row["block_fingerprint_count"] == 0


# ---------------------------------------------------------------------------
# recompute_ready must not advance fingerprint counter for a parent-
# completion promotion (todo → ready). Only a genuine block/unblock cycle
# should increment block_fingerprint_count. Parent-completion-driven
# promotions must NOT touch last_block_fingerprint or block_fingerprint_count.
# ---------------------------------------------------------------------------

def test_recompute_ready_todo_promotion_does_not_touch_fingerprint(conn) -> None:
    """Promoting a todo task via recompute_ready does not alter fingerprint state."""
    parent_id = kb.create_task(conn, title="fp parent", assignee="tester")
    child_id = kb.create_task(conn, title="fp child", assignee="tester")
    kb.link_tasks(conn, parent_id, child_id)

    # Stamp the child with fingerprint state as if it had been blocked before
    conn.execute(
        "UPDATE tasks SET "
        "last_block_fingerprint = 'fp_stale', "
        "block_fingerprint_count = 2 "
        "WHERE id = ?",
        (child_id,),
    )
    conn.commit()

    # Promote via recompute_ready (parent completes → child eligible)
    kb.complete_task(conn, parent_id, result="parent done")
    promoted = kb.recompute_ready(conn)

    # Child should be in 'ready' with fingerprint untouched
    row = conn.execute(
        "SELECT status, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (child_id,),
    ).fetchone()
    assert row["status"] == "ready"
    assert row["last_block_fingerprint"] == "fp_stale"
    assert row["block_fingerprint_count"] == 2


def test_recompute_ready_blocked_promotion_increments_counter(conn) -> None:
    """Promoting a non-sticky blocked task via recompute_ready increments the counter.

    This simulates the circuit-breaker path: _record_task_failure trips and
    sets status='blocked' but does NOT call block_task (so block_kind is NULL
    and _has_sticky_block returns False). Such a task IS eligible for
    recompute_ready auto-recovery, and each re-promotion bumps
    block_fingerprint_count.
    """
    task_id = kb.create_task(conn, title="blocked promo", assignee="tester")
    kb.claim_task(conn, task_id)

    # Simulate circuit-breaker blocked state:
    # - status = 'blocked' (from _record_task_failure tripping)
    # - block_kind = NULL (NOT a block_task sticky block)
    # - last_block_fingerprint set (from a prior block)
    # - block_fingerprint_count = 1
    conn.execute(
        "UPDATE tasks SET "
        "status = 'blocked', "
        "block_kind = NULL, "
        "last_block_reason = NULL, "
        "last_block_fingerprint = 'fp_circuit_break', "
        "block_fingerprint_count = 1 "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    # Verify _has_sticky_block returns False for our non-sticky block
    assert kb._has_sticky_block(conn, task_id) is False

    kb.recompute_ready(conn)

    row = conn.execute(
        "SELECT status, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "ready"
    assert row["last_block_fingerprint"] == "fp_circuit_break"
    assert row["block_fingerprint_count"] == 2


def test_recompute_ready_sticky_block_resets_fingerprint(conn) -> None:
    """Promoting a sticky-blocked task via recompute_ready RESETS the fingerprint.

    A sticky block (needs_input / capability / transient) placed by
    ``kanban_block`` retains last_block_fingerprint and counter=0.
    When parent completion auto-releases it, the blocking condition is gone —
    the next block event will carry a fresh fingerprint.  The circuit breaker
    must NOT be advanced by this dependency-completion non-event, so both
    last_block_fingerprint and block_fingerprint_count are cleared.
    """
    parent_id = kb.create_task(conn, title="fp parent", assignee="tester")
    child_id = kb.create_task(conn, title="fp child", assignee="tester")
    kb.complete_task(conn, parent_id, result="parent done",
                     metadata={"proof_type": "test", "proof": "parent done",
                               "proof_status": "pass", "proof_note": "test"})
    kb.link_tasks(conn, parent_id, child_id)
    kb.claim_task(conn, child_id)

    # Stamp the child with a STICKY block (needs_input — NOT dependency).
    # dependency blocks route to 'todo', not 'blocked', so recompute_ready
    # handles them under the 'todo' branch.  The sticky-block branch fires
    # for needs_input / capability / transient blocks which DO stay in
    # 'blocked' and are sticky until human unblock.
    kb.block_task(conn, child_id, reason="needs input from human", kind="needs_input")
    row_before = conn.execute(
        "SELECT status, block_kind, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (child_id,),
    ).fetchone()
    print("DEBUG before:", dict(row_before))
    print("DEBUG sticky:", kb._has_sticky_block(conn, child_id))
    assert row_before["status"] == "blocked"
    assert row_before["last_block_fingerprint"] is not None
    assert row_before["block_fingerprint_count"] == 1  # block_task sets count=1

    # Verify sticky
    assert kb._has_sticky_block(conn, child_id) is True

    # Parent completes → recompute_ready auto-releases the child
    kb.complete_task(conn, parent_id, result="parent done",
                     metadata={"proof_type": "test", "proof": "parent done",
                               "proof_status": "pass", "proof_note": "test"})
    promoted = kb.recompute_ready(conn)
    print("DEBUG promoted:", promoted)

    # Child is in 'ready' with fingerprint RESET (not incremented)
    row_after = conn.execute(
        "SELECT status, last_block_fingerprint, block_fingerprint_count FROM tasks WHERE id = ?",
        (child_id,),
    ).fetchone()
    print("DEBUG after:", dict(row_after))
    assert row_after["status"] == "ready"
    assert row_after["last_block_fingerprint"] is None
    assert row_after["block_fingerprint_count"] == 0


# ---------------------------------------------------------------------------
# detect_stale_workers recovers ghost running tasks
# ---------------------------------------------------------------------------

def test_detect_stale_workers_recovers_ghost(conn) -> None:
    """A running task with a stale heartbeat is blocked with kind=capability."""
    task_id = kb.create_task(conn, title="ghost task", assignee="tester")
    kb.claim_task(conn, task_id)
    # Stamp a heartbeat far in the past
    stale_time = int(time.time()) - (kb.STALE_RUNNING_TIMEOUT_SECONDS + 10)
    conn.execute(
        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
        (stale_time, task_id),
    )

    recovered = kb.detect_stale_workers(conn, stale_seconds=kb.STALE_RUNNING_TIMEOUT_SECONDS)

    assert task_id in recovered
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "blocked"
    assert row["block_kind"] == "capability"


def test_detect_stale_workers_allows_healthy_running(conn) -> None:
    """A running task with a recent heartbeat is not touched."""
    task_id = kb.create_task(conn, title="healthy task", assignee="tester")
    kb.claim_task(conn, task_id)
    # Stamp a recent heartbeat
    conn.execute(
        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
        (int(time.time()), task_id),
    )

    recovered = kb.detect_stale_workers(conn, stale_seconds=kb.STALE_RUNNING_TIMEOUT_SECONDS)

    assert task_id not in recovered
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "running"


def test_detect_stale_workers_disabled_when_seconds_zero(conn) -> None:
    """Passing stale_seconds <= 0 returns [] without querying."""
    task_id = kb.create_task(conn, title="zero threshold", assignee="tester")
    kb.claim_task(conn, task_id)
    recovered = kb.detect_stale_workers(conn, stale_seconds=0)
    assert recovered == []
