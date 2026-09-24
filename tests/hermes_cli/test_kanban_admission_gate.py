"""Behavioral contract tests for the optional Kanban admission-control gate.

The production script is intentionally untracked and must never be invoked by
unit tests. These tests model only the dispatcher contract at its public seam:
``kanban_db.dispatch_once`` resolves a gate path from
``HERMES_ADMISSION_CONTROL_SCRIPT`` (defaulting to a host-local script that
is never present in CI), invokes it via ``subprocess.run`` with no args,
``capture_output=True``, ``text=True``, ``timeout=15``, and either
short-circuits the tick (non-zero exit) or proceeds (clean exit, missing
script, or operational failure).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def _make_gate_executable(monkeypatch):
    monkeypatch.setattr(kb.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(kb.os, "access", lambda _path, _mode: True)


def test_nonzero_gate_blocks_dispatch_and_surfaces_stderr(conn, monkeypatch):
    """A rejecting gate halts the tick before any dispatcher work proceeds."""
    gate_path = "/synthetic/admission-control"
    monkeypatch.setenv("HERMES_ADMISSION_CONTROL_SCRIPT", gate_path)
    _make_gate_executable(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=23, stdout="", stderr="disk budget exceeded\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = kb.dispatch_once(conn)

    assert result.admission_blocked is True
    assert result.admission_reason == "disk budget exceeded"
    assert calls == [([gate_path], {"capture_output": True, "text": True, "timeout": 15})]


def test_env_override_is_used_and_successful_gate_allows_tick(conn, monkeypatch):
    """The configured override, rather than the live default, is executed."""
    gate_path = "/synthetic/override-gate"
    monkeypatch.setenv("HERMES_ADMISSION_CONTROL_SCRIPT", gate_path)
    _make_gate_executable(monkeypatch)
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = kb.dispatch_once(conn)

    assert result.admission_blocked is False
    assert result.admission_reason == ""
    assert seen == [[gate_path]]


def test_absent_gate_is_non_blocking_and_not_invoked(conn, monkeypatch):
    """A missing optional gate preserves the historical non-blocking tick."""
    monkeypatch.setenv("HERMES_ADMISSION_CONTROL_SCRIPT", "/synthetic/missing-gate")
    monkeypatch.setattr(kb.os.path, "isfile", lambda _path: False)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("missing gate must not be invoked")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    result = kb.dispatch_once(conn)

    assert result.admission_blocked is False
    assert result.admission_reason == ""