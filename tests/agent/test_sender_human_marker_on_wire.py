"""End-to-end: the verified-human sender stamped via ``turn_sender_human`` reaches the provider messages list.

Step 5 of the authenticated-sender-attribution fix (skill
``skills/operations/human-agent-workspaces``) mechanical proof: when
``AIAgent.run_conversation`` is invoked with ``turn_sender_human=<dict>``
the dict is normalised onto ``agent._sender_human`` and the per-turn
marker derived from it is prepended to the user message body that the
provider receives. The transcript (persisted user row) stays clean text.

This test exercises the full path with a mocked OpenAI client, so the
assertion is on the messages list that actually goes out on the wire.
The CLI half (env var → run_conversation kwarg) lives in
``tests/cli/test_quiet_turn_sender_human.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.turn_sender import current_turn_sender_human, sender_human_marker


SENDER = {
    "user_id": "8807405369",
    "provider": "telegram",
    "email": "jon@tantaholdings.com",
    "display_name": "Jon Edwards",
    "org_id": "tanta-holdings",
}


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client, mirroring the fixture in
    ``test_thinking_prefill_trailing_turn.py``."""
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _final_response(text="Done."):
    from tests.run_agent.test_run_agent import _mock_response
    return _mock_response(content=text, finish_reason="stop")


def _sent_messages(create_mock, call_index=0):
    call = create_mock.call_args_list[call_index]
    return call.kwargs.get("messages") or call.args[0].get("messages")


def _extract_user_content(messages):
    """Return a list of every user message's textual content (string or first text part)."""
    out = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(part.get("text", ""))
                    break
    return out


class TestSenderHumanMarkerOnWire:
    """When turn_sender_human is set, the marker rides the user message content."""

    def test_marker_reaches_provider_messages(self, loop_agent):
        """The marker derived from the verified sender appears in the user message sent to the provider."""
        loop_agent.client.chat.completions.create.side_effect = [_final_response()]
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("hello world", turn_sender_human=SENDER)

        sent = _sent_messages(loop_agent.client.chat.completions.create, 0)
        user_texts = _extract_user_content(sent)
        assert user_texts, f"No user message reached the wire: {sent}"
        joined = "\n".join(user_texts)
        # Marker substring assertions — the marker is generated from
        # sender_human_marker() and prepended to the user message body.
        assert "[authenticated sender:" in joined, (
            f"Sender marker missing from wire user message: {user_texts!r}"
        )
        assert "jon@tantaholdings.com" in joined
        assert "user_id=8807405369" in joined
        assert "provider=telegram" in joined
        # The clean user text is still on the wire (marker is prepended, not replacing).
        assert "hello world" in joined

    def test_current_turn_sender_human_returns_dict_during_turn(self, loop_agent):
        """``current_turn_sender_human(agent)`` returns the cached dict while the turn runs.

        Sets the dict via build_turn_context (the same path the dashboard
        uses) so an in-turn consumer (memory hook, tool callback, future
        audit log) can read who is on the other end of the conversation.
        """
        captured = {}

        def record_create(**kwargs):
            # Read the agent's cached sender AT THE MOMENT the provider is called.
            captured["sender"] = current_turn_sender_human(loop_agent)
            return _final_response()

        loop_agent.client.chat.completions.create.side_effect = record_create
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("hi", turn_sender_human=SENDER)

        assert captured["sender"] == SENDER

    def test_current_turn_sender_human_is_none_without_sender(self, loop_agent):
        """No turn_sender_human kwarg → cached sender is None → absence rule applies."""
        captured = {}

        def record_create(**kwargs):
            captured["sender"] = current_turn_sender_human(loop_agent)
            return _final_response()

        loop_agent.client.chat.completions.create.side_effect = record_create
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("hi")

        assert captured["sender"] is None

    def test_marker_absent_when_no_sender(self, loop_agent):
        """A turn with no turn_sender_human does NOT inject a marker — absence rule applies."""
        loop_agent.client.chat.completions.create.side_effect = [_final_response()]
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("hello")

        sent = _sent_messages(loop_agent.client.chat.completions.create, 0)
        user_texts = _extract_user_content(sent)
        assert user_texts == ["hello"]
        assert not any("[authenticated sender:" in t for t in user_texts)

    def test_marker_uses_user_id_form_when_no_email(self, loop_agent):
        """A sender without an email still produces a marker (user_id+provider form)."""
        no_email = {
            "user_id": "u-abc",
            "provider": "google",
            # email / display_name / org_id absent on purpose
        }
        loop_agent.client.chat.completions.create.side_effect = [_final_response()]
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("ping", turn_sender_human=no_email)

        sent = _sent_messages(loop_agent.client.chat.completions.create, 0)
        joined = "\n".join(_extract_user_content(sent))
        assert "[authenticated sender:" in joined
        assert "user_id=u-abc" in joined
        assert "provider=google" in joined
        # No email branch: the marker must NOT include an "@".
        assert "@" not in joined
        assert "ping" in joined

    def test_persisted_transcript_row_stays_clean_text(self, loop_agent):
        """The persisted user row's content is the clean text — marker goes only to the model.

        Architecture: ``run_conversation(user_message, ...)`` defaults
        ``persist_user_message=None``. ``_bind_turn_identity`` sets
        ``agent._persist_user_message_override = None`` from that. Then
        ``_stage_turn_user_message`` runs, prepends the marker to the live
        ``user_msg["content"]``, AND (since the CLI path did not pre-supply
        an override) re-pins the override to the clean text so the durable
        row's ``content`` is the clean text and the marker-prefixed bytes
        ride only on the wire via the ``api_content`` sidecar.

        This test pins BOTH halves of that contract:
        - Wire content carries the marker (model sees the attribution).
        - Persist override carries the clean text (transcript stays clean).

        The override assertion is unconditional (Motoko's P2 tightening on
        the previous version of this test, which gated the assertion behind
        an ``isinstance`` check).
        """
        loop_agent.client.chat.completions.create.side_effect = [_final_response()]
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("clean text", turn_sender_human=SENDER)

        # The persist override is the clean text — same value as user_message.
        # This is what durable_user_row_content writes to the DB row's content.
        override = getattr(loop_agent, "_persist_user_message_override", None)
        assert override == "clean text", (
            f"Persist override must be the clean text the user typed, "
            f"so the transcript row stays clean. Got {override!r}"
        )
        # And the override must NOT carry the marker.
        assert "[authenticated sender:" not in (override or ""), (
            "Persist override leaked the marker — the transcript row would "
            "display [authenticated sender: ...] to the user"
        )

        # Wire content (provider messages) DOES carry the marker — that's
        # the model-facing half of the contract. Verified separately by
        # test_marker_reaches_provider_messages; restated here so a single
        # failure points the debugger at the right boundary.
        sent = _sent_messages(loop_agent.client.chat.completions.create, 0)
        wire_texts = _extract_user_content(sent)
        assert any("[authenticated sender:" in t for t in wire_texts), (
            f"Wire user message missing the marker — model-facing contract broken. {wire_texts!r}"
        )

    def test_garbage_sender_does_not_produce_marker(self, loop_agent):
        """A turn_sender_human that fails parse_sender_human validation yields no marker.

        Defensive: a bad kwarg (e.g. a dispatcher's wrong-shape serialisation)
        must not silently leak into the prompt as a half-formed marker.
        """
        garbage = {"email": "leak@evil.example"}  # no user_id, no provider
        loop_agent.client.chat.completions.create.side_effect = [_final_response()]
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("plain", turn_sender_human=garbage)

        sent = _sent_messages(loop_agent.client.chat.completions.create, 0)
        joined = "\n".join(_extract_user_content(sent))
        assert "[authenticated sender:" not in joined, (
            f"Garbage sender produced a marker on the wire: {joined!r}"
        )
        assert joined.strip() == "plain"


def test_sender_human_marker_helper_contract():
    """Smoke check on the marker builder itself (already covered by tests/agent/test_turn_sender.py;
    repeated here so the wire-path test stays self-contained as a debugging entry point)."""
    assert "[authenticated sender:" in sender_human_marker(SENDER)
    assert "jon@tantaholdings.com" in sender_human_marker(SENDER)
    assert sender_human_marker(None) == ""
    assert sender_human_marker({}) == ""