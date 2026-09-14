"""``hermes chat -Q`` passes the dispatcher's HERMES_SENDER_HUMAN to ``run_conversation`` as ``turn_sender_human``.

Step 5 of the authenticated-sender-attribution fix (skill
``skills/operations/human-agent-workspaces``): the CLI one-shot path
``_run_quiet_single_query`` MUST consume ``HERMES_SENDER_HUMAN`` from the
*child process* environment that a dashboard-style dispatcher forks, hand
the parsed dict through to ``run_conversation(..., turn_sender_human=...)``,
and remove the env var before the turn starts (so a tool subprocess started
during the turn does not inherit it — same authority rule as
``HERMES_TURN_AUTHOR``).

The env var is read-only from the parent's view: a JSON client cannot mint
it. Only the parent's server-stamped identity propagates into the child, so
this test exercises the dispatcher's hand-off, not a client attack.

The deeper assertions — that ``current_turn_sender_human()`` returns the
dict during the turn and that the marker actually reaches the provider
messages list — live in ``tests/agent/test_sender_human_marker_on_wire.py``,
which drives ``run_conversation`` end-to-end with a mocked client. This
file is the CLI smoke half: prove the env var is read, the parsed sender
reaches ``run_conversation``, and a junk value is silently dropped.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import cli
from agent.turn_sender import SENDER_HUMAN_ENV, sender_human_env


SENDER = {
    "user_id": "8807405369",
    "provider": "telegram",
    "email": "jon@tantaholdings.com",
    "display_name": "Jon Edwards",
    "org_id": "tanta-holdings",
}


@pytest.fixture(autouse=True)
def _plain_one_shot_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


def _run(monkeypatch, env_value):
    """One quiet turn with HERMES_SENDER_HUMAN set to ``env_value`` (None unsets); returns the recorded call kwargs."""
    if env_value is None:
        monkeypatch.delenv(SENDER_HUMAN_ENV, raising=False)
    else:
        monkeypatch.setenv(SENDER_HUMAN_ENV, env_value)

    recorded = []

    def record(**kwargs):
        recorded.append(kwargs)
        return {"final_response": "ok"}

    agent = SimpleNamespace(run_conversation=record, session_id="s-1")
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(
            SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"),
            "hello",
        )
    assert exc.value.code == 0
    return recorded


def test_quiet_one_shot_passes_turn_sender_human_from_env(monkeypatch, capsys):
    """The dispatcher-stamped sender reaches run_conversation as turn_sender_human."""
    # Use sender_human_env to build the env entry — same path the parent
    # dispatcher uses to seed the child's env, so this test asserts on
    # the exact bytes-on-the-wire format, not a hand-written JSON literal.
    env_value = sender_human_env(SENDER)[SENDER_HUMAN_ENV]
    recorded = _run(monkeypatch, env_value)
    assert recorded == [{
        "user_message": "hello",
        "conversation_history": [],
        "turn_sender_human": SENDER,
    }]
    assert capsys.readouterr().out.strip() == "ok"


def test_quiet_one_shot_consumes_the_variable_before_the_turn(monkeypatch):
    """Tool subprocesses spawned during the turn must not see the dispatcher's sender."""
    seen = {}

    def run_conversation(**kwargs):
        seen["env"] = os.environ.get(SENDER_HUMAN_ENV)
        return {"final_response": "ok"}

    # Drive _run_quiet_single_query directly with a custom agent that
    # delegates to our recording spy; the env var must be popped before
    # the spy runs (take_* mirrors turn_author's contract).
    monkeypatch.setenv(SENDER_HUMAN_ENV, sender_human_env(SENDER)[SENDER_HUMAN_ENV])
    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1")
    with pytest.raises(SystemExit):
        cli._run_quiet_single_query(
            SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"),
            "hello",
        )
    assert seen["env"] is None
    assert SENDER_HUMAN_ENV not in os.environ


def test_quiet_one_shot_without_sender_env_does_not_pass_kwarg(monkeypatch):
    """A plain CLI chat (no dispatcher, no env) must not mint a sender — absence rule applies."""
    recorded = _run(monkeypatch, None)
    assert recorded == [{"user_message": "hello", "conversation_history": []}]
    # The kwarg guard in _run_quiet_single_query only adds turn_sender_human
    # when (a) a parsed sender exists AND (b) run_conversation accepts the
    # kwarg, so a plain CLI chat gets an empty kwargs dict.
    assert "turn_sender_human" not in recorded[0]


def test_quiet_one_shot_garbage_sender_does_not_pass_kwarg(monkeypatch):
    """A junk HERMES_SENDER_HUMAN (missing user_id+provider) does NOT mint a sender.

    Safety guarantee: the env var is a server-stamped side channel, but a
    malformed value (e.g. a parent bug that shipped the wrong shape) must
    not silently win. The agent's AGENTS.md absence rule applies instead —
    the same outcome an unauthenticated CLI chat gets.
    """
    bad = '{"email": "leak@evil.example"}'  # no user_id, no provider
    recorded = _run(monkeypatch, bad)
    assert recorded == [{"user_message": "hello", "conversation_history": []}]
    assert "turn_sender_human" not in recorded[0]


def test_quiet_one_shot_with_partial_sender_does_not_pass_kwarg(monkeypatch):
    """A sender missing only one required field (provider) is also rejected.

    Mirrors the test_turn_sender.py parametrize cases: parse_sender_human
    requires BOTH user_id AND provider. A value with only one is treated as
    garbage and dropped — never silently coerced.
    """
    # user_id only — provider missing.
    bad = '{"user_id": "u1"}'
    recorded = _run(monkeypatch, bad)
    assert "turn_sender_human" not in recorded[0]