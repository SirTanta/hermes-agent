"""Tests for agent.turn_sender: the per-turn verified-human sender carried from
the authenticated dashboard session to the agent's prompt assembly and tool hooks.

The companion to ``test_turn_author.py`` (which covers the bot-author case for
relayed DMs); this module covers the human sender that the dashboard stamps at
WS-upgrade auth time and that the Telegram adapter stamps via
``gateway.sender_identity.lookup_telegram_sender``.
"""

import pytest

from agent.turn_sender import (
    SENDER_HUMAN_ENV,
    parse_sender_human,
    sender_human_env,
    sender_human_from_env,
    sender_human_marker,
    take_sender_human_from_env,
)


class TestParseSenderHuman:
    @pytest.mark.parametrize("raw", [
        None, 42, [], "not json", '"a string"', "[1, 2]", b"\xff",
        {}, {"email": "x@example.com"},  # user_id + provider both missing → None
        {"user_id": "u1"},  # provider missing
        {"provider": "google"},  # user_id missing
        {"user_id": "", "provider": "google"},  # empty user_id
        {"user_id": "u1", "provider": ""},  # empty provider
    ])
    def test_garbage_or_incomplete_returns_none(self, raw):
        assert parse_sender_human(raw) is None

    def test_minimal_dashboard_auth_identity(self):
        # Exact shape produced by web_server_chat._stamp_identity from a WS ticket.
        raw = {"user_id": "abc123", "provider": "google"}
        assert parse_sender_human(raw) == {
            "user_id": "abc123", "email": None, "display_name": None,
            "org_id": "", "provider": "google",
        }

    def test_full_envelope(self):
        raw = {
            "user_id": "u1", "provider": "telegram", "email": "a@b.com",
            "display_name": "Jon Edwards", "org_id": "tanta-holdings",
        }
        assert parse_sender_human(raw) == raw

    def test_json_string_round_trip(self):
        raw = '{"user_id": "u1", "provider": "p", "email": "a@b"}'
        assert parse_sender_human(raw) == {
            "user_id": "u1", "email": "a@b", "display_name": None,
            "org_id": "", "provider": "p",
        }

    def test_overlong_fields_are_capped(self):
        # 320-char cap protects the marker line from runaway markers
        long = "x" * 1024
        raw = {"user_id": long, "provider": "google", "display_name": long, "email": long}
        parsed = parse_sender_human(raw)
        assert all(len(v) <= 320 for v in parsed.values() if isinstance(v, str))


class TestEnvCarrier:
    def test_round_trip_through_env(self):
        sender = {"user_id": "u1", "provider": "telegram", "display_name": "Jon"}
        env = sender_human_env(sender)
        assert set(env) == {SENDER_HUMAN_ENV}
        assert sender_human_from_env(env) == {
            "user_id": "u1", "email": None, "display_name": "Jon",
            "org_id": "", "provider": "telegram",
        }

    def test_take_removes_the_variable(self):
        # ``take_*`` mirrors ``take_turn_author_from_env`` so subprocesses do not inherit.
        sender = {"user_id": "u1", "provider": "telegram"}
        env = dict(sender_human_env(sender), OTHER="kept")
        assert take_sender_human_from_env(env) == {
            "user_id": "u1", "email": None, "display_name": None,
            "org_id": "", "provider": "telegram",
        }
        assert env == {"OTHER": "kept"}
        assert take_sender_human_from_env(env) is None

    def test_missing_env_returns_none(self):
        assert sender_human_from_env({}) is None
        assert take_sender_human_from_env({}) is None


class TestSenderHumanMarker:
    def test_marker_with_email(self):
        sender = {"user_id": "u1", "email": "jon@x.com", "provider": "telegram"}
        marker = sender_human_marker(sender)
        assert "[authenticated sender:" in marker
        assert "jon@x.com" in marker
        assert "user_id=u1" in marker
        assert "provider=telegram" in marker

    def test_marker_without_email(self):
        sender = {"user_id": "u1", "email": None, "provider": "google"}
        marker = sender_human_marker(sender)
        assert "user_id=u1" in marker
        assert "provider=google" in marker
        # No email → falls back to user_id+provider form
        assert "@" not in marker

    def test_marker_absent_for_unbound_sender(self):
        # Empty marker → the agent sees no header → applies absence rule.
        assert sender_human_marker(None) == ""
        assert sender_human_marker({}) == ""
        assert sender_human_marker("not a dict") == ""
