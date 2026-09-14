"""Tests for the gateway turn-runner's Telegram sender resolution.

The dashboard path stamps its own ``auth_identity`` and bypasses this helper
(handled in the tui_gateway ``prompt.submit``). The Telegram adapter carries
only ``from_user.id`` and ``user_name``, so the gateway turn-runner resolves
the human sender through ``gateway.sender_identity.lookup_telegram_sender``
and feeds the result into ``agent.run_conversation(..., turn_sender_human=...)``.
This test exercises that resolver against the SessionSource contract.
"""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import _resolved_sender_human_for_source


def _source(platform=Platform.TELEGRAM, user_id=None, user_name=None, **extras):
    return SimpleNamespace(
        platform=platform, user_id=user_id, user_name=user_name, **extras)


class TestResolvedSenderHumanForSource:
    def test_known_telegram_id_resolves_to_full_sender(self):
        source = _source(user_id="8807405369", user_name="Jon Edwards")
        sender = _resolved_sender_human_for_source(source)
        assert sender == {
            "user_id": "8807405369", "email": "jon@tantaholdings.com",
            "display_name": "Jon Edwards", "org_id": "tanta-holdings",
            "provider": "telegram",
        }

    def test_known_janice_telegram_id_resolves(self):
        source = _source(user_id="8755788728", user_name="Janice")
        sender = _resolved_sender_human_for_source(source)
        assert sender is not None
        assert sender["user_id"] == "8755788728"
        assert sender["display_name"] == "Janice"
        assert sender["provider"] == "telegram"

    def test_unknown_telegram_id_returns_none(self):
        # Unmapped id → AGENTS.md absence rule applies; not a silent default.
        source = _source(user_id="9999999999", user_name="Unknown")
        assert _resolved_sender_human_for_source(source) is None

    def test_none_telegram_id_returns_none(self):
        source = _source(user_id=None, user_name="")
        assert _resolved_sender_human_for_source(source) is None

    def test_numeric_telegram_id_normalized(self):
        # ``SessionSource.user_id`` may arrive as int (Telegram sends ints);
        # the registry lookup accepts both shapes.
        source = _source(user_id=8807405369, user_name="Jon")
        assert _resolved_sender_human_for_source(source) is not None

    @pytest.mark.parametrize("platform", [
        Platform.SLACK, Platform.DISCORD, Platform.WHATSAPP, Platform.SIGNAL,
    ])
    def test_non_telegram_platform_skips_telegram_lookup(self, platform):
        # Only Telegram needs the resolver today — the dashboard path mints its
        # own identity at WS-upgrade and stamps it on the request itself. Other
        # transports don't carry a human sender at all (peer relay, cron) and
        # should fall through to the absence rule.
        source = _source(platform=platform, user_id="anyone", user_name="Someone")
        assert _resolved_sender_human_for_source(source) is None

    def test_returns_defensive_copy_not_registry_reference(self):
        # Mutating the returned sender must not poison the registry for the
        # next turn. The lookup helper copies; this test asserts the contract.
        source = _source(user_id="8807405369", user_name="Jon")
        sender = _resolved_sender_human_for_source(source)
        sender["display_name"] = "MUTATED"
        # Re-fetch — must show the original display name.
        source2 = _source(user_id="8807405369", user_name="Jon")
        fresh = _resolved_sender_human_for_source(source2)
        assert fresh["display_name"] == "Jon Edwards"
