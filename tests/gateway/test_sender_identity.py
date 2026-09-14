"""Tests for gateway.sender_identity: the Telegram-id → verified-human registry.

Single source of truth for telegram_id → {name, email, provider, user_id} in the
gateway path. New human principals are added by appending rows to
``TELEGRAM_SENDER_REGISTRY``; this test asserts the lookup contract and the
extensibility contract (a new row IS picked up, a missing row IS NOT silently
defaulted to a hardcoded if/else).
"""

import pytest

from gateway.sender_identity import (
    TELEGRAM_SENDER_REGISTRY,
    lookup_telegram_sender,
)


class TestLookupTelegramSender:
    def test_known_jon_id_returns_sender(self):
        sender = lookup_telegram_sender("8807405369")
        assert sender == {
            "user_id": "8807405369",
            "provider": "telegram",
            "display_name": "Jon Edwards",
            "email": "jon@tantaholdings.com",
            "org_id": "tanta-holdings",
        }

    def test_known_janice_id_returns_sender(self):
        sender = lookup_telegram_sender("8755788728")
        assert sender is not None
        assert sender["display_name"] == "Janice"
        assert sender["provider"] == "telegram"
        assert sender["email"].endswith("@tantaholdings.com")

    def test_unknown_id_returns_none(self):
        # The agent's AGENTS.md absence rule applies when no mapping exists.
        assert lookup_telegram_sender("1234567890") is None

    @pytest.mark.parametrize("bad_input", [None, "", "  ", 0])
    def test_garbage_inputs_return_none(self, bad_input):
        assert lookup_telegram_sender(bad_input) is None

    def test_numeric_id_normalized_to_str(self):
        # Telegram sends ``from_user.id`` as int; the lookup accepts both.
        assert lookup_telegram_sender(8807405369) is not None

    def test_whitespace_stripped(self):
        assert lookup_telegram_sender("  8807405369  ") is not None

    def test_lookup_returns_copy_not_registry_reference(self):
        # A caller mutating the result must not poison the next turn's lookup.
        sender = lookup_telegram_sender("8807405369")
        sender["display_name"] = "MUTATED"
        fresh = lookup_telegram_sender("8807405369")
        assert fresh["display_name"] == "Jon Edwards"

    def test_registry_is_extensible(self):
        # Demonstrates the contract: adding a row to the dict makes the lookup work.
        # Not a mutation — we just confirm the lookup honors whatever the dict holds
        # at call time, so future "add another principal" changes do not require a
        # new if/else.
        original = dict(TELEGRAM_SENDER_REGISTRY)
        try:
            TELEGRAM_SENDER_REGISTRY["9999999999"] = {
                "user_id": "9999999999", "provider": "telegram",
                "display_name": "Test User", "email": "test@example.com",
                "org_id": "",
            }
            assert lookup_telegram_sender("9999999999")["display_name"] == "Test User"
        finally:
            TELEGRAM_SENDER_REGISTRY.clear()
            TELEGRAM_SENDER_REGISTRY.update(original)
