"""Authenticated human sender of a turn.

Companion to ``turn_author`` (which names the *bot* author of a relayed DM).
Where ``turn_author`` is set by the relay dispatcher and accepted from one
external gateway-controlled field, ``turn_sender`` is set by the **server**
from the verified dashboard session, never from a client parameter. A
relayed DM, a CLI chat, or a cron callback all read ``HERMES_SENDER_HUMAN``
from the *child process* environment that the dispatcher launches; a
JSON client cannot mint it. Same authority rule as the bot author
explicit-refusal in ``tui_gateway/methods_bot_relay.py``.

The contract per ``skills/operations/human-agent-workspaces`` Step 5:
**identify the sender from authenticated session context; never
default-assume any human identity when sender context is absent or
unverified.** This module exposes:

- ``parse_sender_human(raw)`` — normalize a dict or JSON string into
  ``{user_id, email, display_name, org_id, provider}``; None on garbage.
- ``sender_human_from_env(environ=os.environ)`` — read
  ``HERMES_SENDER_HUMAN`` if set; never raises.
- ``take_sender_human_from_env(environ=os.environ)`` — read once and
  remove from environ so a child process started during this turn does
  not inherit it (mirrors ``take_turn_author_from_env``).
- ``sender_human_env(sender)`` — build the env entry a parent
  dispatcher merges into a child's env when forking.
- ``current_turn_sender_human()`` — read the cached sender on
  ``agent._sender_human`` (set in ``build_turn_context``).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, MutableMapping, Optional

SENDER_HUMAN_ENV = "HERMES_SENDER_HUMAN"

_MAX_FIELD_LEN = 320


def _clean_optional(value: Any) -> Optional[str]:
    """Strip, cap, and return a string field; None for empty/garbage."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:_MAX_FIELD_LEN]


def parse_sender_human(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize a dict or JSON string into a sender dict.

    Returns ``{user_id, email, display_name, org_id, provider}``; ``None``
    if the input is not parseable or carries no user identity at all
    (a verified sender always carries ``user_id`` AND ``provider`` at
    minimum — ``email`` may be missing for some identity providers).
    """
    try:
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, Mapping):
            return None
        sender = {
            "user_id": _clean_optional(raw.get("user_id")),
            "email": _clean_optional(raw.get("email")),
            "display_name": _clean_optional(raw.get("display_name")),
            "org_id": _clean_optional(raw.get("org_id")) or "",
            "provider": _clean_optional(raw.get("provider")),
        }
        # A verified sender MUST carry user_id + provider; email is
        # helpful but not required (e.g. some IdPs mint opaque ids).
        if sender["user_id"] is None or sender["provider"] is None:
            return None
        return sender
    except Exception:
        return None


def sender_human_from_env(environ: Mapping[str, str] = os.environ) -> Optional[Dict[str, Any]]:
    """The verified human sender the gateway stamped into ``HERMES_SENDER_HUMAN``, or None."""
    return parse_sender_human(environ.get(SENDER_HUMAN_ENV))


def take_sender_human_from_env(environ: MutableMapping[str, str] = os.environ) -> Optional[Dict[str, Any]]:
    """Read and remove ``HERMES_SENDER_HUMAN`` so subprocesses started during the turn
    do not inherit it (mirrors ``take_turn_author_from_env``)."""
    return parse_sender_human(environ.pop(SENDER_HUMAN_ENV, None))


def sender_human_env(sender: Dict[str, Any]) -> Dict[str, str]:
    """The environment entry a parent dispatcher merges into a child's env."""
    return {SENDER_HUMAN_ENV: json.dumps(sender, separators=(",", ":"))}


def current_turn_sender_human(agent) -> Optional[Dict[str, Any]]:
    """The sender the gateway stamped onto ``agent._sender_human`` for the current turn.

    Set in ``build_turn_context`` from ``turn_sender_human``; reset per turn
    the same way ``agent._turn_author`` is reset, so a cached gateway agent
    never carries a previous turn's sender forward.
    """
    return getattr(agent, "_sender_human", None)


def sender_human_marker(sender: Optional[Dict[str, Any]]) -> str:
    """A short, deterministic header line for the user message body when a verified
    sender is bound to the turn. Empty string when no sender is bound (the agent
    sees no attribution header at all and must apply the absence rule).

    Format: ``[authenticated sender: <email> (user_id=<user_id>, provider=<provider>)]``
    — or, when no email is available, ``[authenticated sender: user_id=<user_id>
    (provider=<provider>)]``. The marker is appended to the user message body so
    it does NOT mutate the cached system prompt (prompt-cache invariant).
    """
    if not isinstance(sender, dict):
        return ""
    user_id = sender.get("user_id") or ""
    provider = sender.get("provider") or ""
    # An unvalidated dict (e.g. an empty ``{}`` that slipped past ``parse_sender_human``)
    # must not produce a malformed header — the absence rule applies instead.
    if not user_id or not provider:
        return ""
    email = sender.get("email") or ""
    if email:
        return f"[authenticated sender: {email} (user_id={user_id}, provider={provider})]"
    return f"[authenticated sender: user_id={user_id} (provider={provider})]"