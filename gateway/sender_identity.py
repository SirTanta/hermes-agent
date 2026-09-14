"""Verified-human-sender identity for gateway-routed turns.

Shared concept for every transport that delivers a turn from a human
principal: a small ``{user_id, email, display_name, org_id, provider}``
dict (the shape ``agent.turn_sender.parse_sender_human`` accepts).

The dashboard path already has a server-minted identity
(``WSTransport.auth_identity``); this module covers transports that do NOT
mint their own identity internally (Telegram DMs land here with only a
``telegram_id`` + ``user_name``). The lookup is a small, explicit table
keyed on the transport's stable user id (``telegram_id`` today), so a new
channel plugs in by registering its own mapping rather than by forking
``prompt.submit`` plumbing.

Design notes
------------

- The agent contract is the ``turn_sender_human`` kwarg
  (``agent/turn_sender.py``). Both transports produce the SAME shape, so
  the prompt-builder / marker logic never branches on transport.
- The registry is intentionally tiny: a dict literal plus a couple of
  helpers. No DB lookup, no async I/O, no plugin discovery — this runs on
  every inbound Telegram DM and must not gate on slow paths.
- Unmapped ids return ``None``; the agent's AGENTS.md absence rule
  applies. That is the same outcome a CLI chat turn gets today, and it
  is the safe default. A future authenticated-sender mechanism for
  Telegram (Telegram Login Widget, 2FA from a paired device, ...) replaces
  this module without changing the contract.
"""

from __future__ import annotations

from typing import Dict, Optional


# Stable telegram user_id -> verified human sender.
# Add new principals here. ``provider="telegram"`` is the canonical token
# for a sender mapped from a Telegram ``from_user.id`` (NOT a bot user; the
# adapter's auth path rejects is_bot=True before it reaches the gateway
# runner, so a bot-shaped user_id can never mint a sender).
TELEGRAM_SENDER_REGISTRY: Dict[str, Dict[str, str]] = {
    # Jon Edwards (Tanta Holdings). user_id is the Telegram numeric id; the
    # registry stores it as a str for stable JSON serialization through the
    # env-carrier that the in-process / isolated-compute paths consume.
    "8807405369": {
        "user_id": "8807405369",
        "provider": "telegram",
        "display_name": "Jon Edwards",
        "email": "jon@tantaholdings.com",
        "org_id": "tanta-holdings",
    },
    # Janice (second human principal). Same shape; future Janices add
    # another row, not another if/else branch.
    "8755788728": {
        "user_id": "8755788728",
        "provider": "telegram",
        "display_name": "Janice",
        "email": "janice@tantaholdings.com",
        "org_id": "tanta-holdings",
    },
}


def lookup_telegram_sender(telegram_user_id: Optional[str]) -> Optional[Dict[str, str]]:
    """Return the verified sender for a Telegram ``from_user.id``, or ``None``.

    ``telegram_user_id`` may be a numeric id (int) or string; the lookup
    normalizes to a stripped string. ``None`` and unmapped ids return
    ``None`` so the caller can fall through to the absence rule.
    """
    if telegram_user_id is None:
        return None
    key = str(telegram_user_id).strip()
    if not key:
        return None
    sender = TELEGRAM_SENDER_REGISTRY.get(key)
    if sender is None:
        return None
    # Defensive copy so a caller that mutates the result does not poison
    # the registry for the next turn.
    return dict(sender)
