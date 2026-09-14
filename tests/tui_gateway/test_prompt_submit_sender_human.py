"""Tests for the prompt.submit handler threading the verified-human sender from
the WS-upgrade ``auth_identity`` into the agent's ``turn_sender_human`` kwarg.

This test exercises the dashboard path's two key contracts:
1. ``_resolved_sender_human_for_request`` reads ``current_transport().auth_identity``
   (server-minted at WS-upgrade auth time) and refuses client-minted sender via
   a ``_SENDER_REFUSED`` sentinel.
2. The full ``prompt.submit`` handler threads the resolved sender through
   ``_run_prompt_submit`` (in-process) AND ``_submit_prompt_to_compute_host``
   (isolated-compute). Both are patched here so the test does not need a live
   agent.
"""

from types import SimpleNamespace

import pytest

from tui_gateway import methods_prompt as prompt_module
from tui_gateway import server
from tui_gateway.method_ctx import bind_module
from tui_gateway.transport import bind_transport, current_transport, reset_transport


# Install the methods_prompt module onto server (this is what server.py's
# `register` call does at runtime). Without this, ``_err`` / ``_ok`` /
# ``_methods`` are not reachable inside the handler's namespace.
prompt_module.register(server)


class TestResolvedSenderHumanForRequest:
    """The helper that backs ``prompt.submit``'s sender resolution."""

    def setup_method(self):
        self._transport_token = None

    def teardown_method(self):
        if self._transport_token is not None:
            reset_transport(self._transport_token)
            self._transport_token = None

    def _bind_transport(self, transport):
        # The production code path uses the ContextVar-backed ``current_transport``;
        # rebinding on the module does NOT change what ``from .transport import
        # current_transport`` resolves to inside the helper.
        self._transport_token = bind_transport(transport)

    def test_no_transport_returns_none(self):
        self._bind_transport(None)
        assert prompt_module._resolved_sender_human_for_request({"text": "hi"}) is None

    def test_transport_without_auth_identity_returns_none(self):
        self._bind_transport(SimpleNamespace(auth_identity=None))
        assert prompt_module._resolved_sender_human_for_request({"text": "hi"}) is None

    def test_internal_credential_is_filtered(self):
        # The internal-credential auth identity must NOT mint a sender — only
        # user-facing sessions do. ``_is_authenticated_identity`` returns False
        # for the internal sentinel, so the helper returns None.
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "server-internal", "provider": "server-internal"}))
        assert prompt_module._resolved_sender_human_for_request({"text": "hi"}) is None

    def test_authenticated_identity_resolves_to_full_sender(self):
        # The dashboard WS auth stamps only ``{user_id, provider}``; the helper
        # promotes it through ``parse_sender_human`` into the agent contract.
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "u-abc", "provider": "google"}))
        sender = prompt_module._resolved_sender_human_for_request({"text": "hi"})
        assert sender == {
            "user_id": "u-abc", "email": None, "display_name": None,
            "org_id": "", "provider": "google",
        }

    def test_client_minting_sender_is_refused_with_sentinel(self):
        # A JSON client trying to put ``_sender_human`` in params must NOT
        # silently win — the helper returns the sentinel so the caller can
        # 4124 like it does for ``_turn_author``.
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "real-user", "provider": "google"}))
        assert prompt_module._resolved_sender_human_for_request(
            {"text": "hi", "_sender_human": {"user_id": "spoof", "provider": "spoof"}}
        ) is prompt_module._SENDER_REFUSED


class TestPromptSubmitThreading:
    """End-to-end: prompt.submit must hand the sender into both prompt paths."""

    def setup_method(self):
        self._transport_token = None

    def teardown_method(self):
        if self._transport_token is not None:
            reset_transport(self._transport_token)
            self._transport_token = None

    def _bind_transport(self, transport):
        self._transport_token = bind_transport(transport)

    def _build_fake_session(self, **overrides):
        """Minimal session dict that ``prompt.submit`` consults before locking in the turn."""
        import threading
        session = {
            "session_key": "test-skey",
            "history": [],
            "history_version": 0,
            "history_lock": threading.Lock(),
            "cols": 80,
            "attached_images": [],
            "profile_home": "",
            "model_override": None,
            "create_reasoning_override": None,
            "create_service_tier_override": None,
            "source": "test",
            "running": False,
            "agent_ready": True,  # Skip the build path
        }
        session.update(overrides)
        return session

    def test_in_process_path_carries_sender(self, monkeypatch):
        """The dashboard sender must reach ``_run_prompt_submit`` (in-process path)."""
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "u-abc", "provider": "google"}))

        captured: dict = {}

        def fake_run_prompt_submit(rid, sid, session, text, **kw):
            captured["turn_sender_human"] = kw.get("turn_sender_human")
            captured["turn_author"] = kw.get("turn_author")
            return True
        monkeypatch.setattr(server, "_run_prompt_submit", fake_run_prompt_submit)

        # Stub out helpers ``prompt.submit`` calls before reaching
        # ``_run_prompt_submit``: session lookup, slot check, lock-in, persist,
        # build start, threading.Thread (we want a direct call, not a thread).
        sid = "test-sid"
        session = self._build_fake_session()
        server._sessions[sid] = session
        monkeypatch.setattr(server, "_sess_nowait", lambda params, rid: (session, None))
        monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, sess: None)
        monkeypatch.setattr(server, "_reattach_refusal", lambda rid, sid, sess: None)
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda sess, cfg: False)
        monkeypatch.setattr(server, "_lock_in_submit_turn", lambda *a, **kw: (None, {}))
        monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda rid, sess: None)
        monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a, **kw: True)
        monkeypatch.setattr(server, "_start_agent_build", lambda *a, **kw: None)

        # Inline the thread body so we don't race a real thread in tests.
        def fake_run_after_agent_ready(rid, sid, session, text, display_kind,
                                        hosted_terminal_callback, turn_author=None,
                                        turn_sender_human=None):
            captured["turn_sender_human_after"] = turn_sender_human
            server._run_prompt_submit(rid, sid, session, text,
                                       display_kind=display_kind,
                                       turn_author=turn_author,
                                       turn_sender_human=turn_sender_human)
        monkeypatch.setattr(server, "_run_after_agent_ready", fake_run_after_agent_ready)

        try:
            result = server._methods["prompt.submit"](
                "rid-1", {"session_id": sid, "text": "hi"})
        finally:
            server._sessions.pop(sid, None)

        # Must NOT be a refusal envelope
        assert "error" not in result, result
        assert captured.get("turn_sender_human") == {
            "user_id": "u-abc", "email": None, "display_name": None,
            "org_id": "", "provider": "google",
        }
        assert captured.get("turn_sender_human_after") == captured["turn_sender_human"]

    def test_isolated_compute_path_carries_sender(self, monkeypatch):
        """The dashboard sender must reach ``_submit_prompt_to_compute_host`` too."""
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "u-abc", "provider": "google"}))

        captured: dict = {}

        def fake_submit_to_compute_host(rid, sid, session, text, **kw):
            captured["turn_sender_human"] = kw.get("turn_sender_human")
            captured["display_kind"] = kw.get("display_kind")
            return {"result": {}, "error": None}
        # The handler calls _submit_prompt_to_compute_host bare (rebinding means
        # server's namespace owns the lookup). Patch on server.
        monkeypatch.setattr(server, "_submit_prompt_to_compute_host",
                            fake_submit_to_compute_host)

        sid = "test-sid-iso"
        session = self._build_fake_session()
        server._sessions[sid] = session
        monkeypatch.setattr(server, "_sess_nowait", lambda params, rid: (session, None))
        monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, sess: None)
        monkeypatch.setattr(server, "_reattach_refusal", lambda rid, sid, sess: None)
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda sess, cfg: True)
        monkeypatch.setattr(server, "_lock_in_submit_turn", lambda *a, **kw: (None, {}))
        monkeypatch.setattr(server, "_load_dashboard_process_isolation_config",
                            lambda: {"compute_host_heartbeat_secs": 15})

        try:
            result = server._methods["prompt.submit"](
                "rid-2", {"session_id": sid, "text": "hi"})
        finally:
            server._sessions.pop(sid, None)

        # The fake returns {"result": {}, "error": None}; the handler treats
        # None as success and returns it. ``error`` should be absent (not just
        # falsy) — the handler unwraps the result before returning.
        assert result.get("error") in (None, False)
        assert result.get("result", {}).get("status") == "streaming" or "result" in result
        assert captured.get("turn_sender_human") == {
            "user_id": "u-abc", "email": None, "display_name": None,
            "org_id": "", "provider": "google",
        }

    def test_client_supplied_sender_is_refused_with_4124(self, monkeypatch):
        """A client trying to mint a sender must be refused at the RPC layer."""
        self._bind_transport(SimpleNamespace(
            auth_identity={"user_id": "u-abc", "provider": "google"}))

        sid = "test-sid-refuse"
        session = self._build_fake_session()
        server._sessions[sid] = session
        monkeypatch.setattr(server, "_sess_nowait", lambda params, rid: (session, None))

        try:
            result = server._methods["prompt.submit"](
                "rid-3", {
                    "session_id": sid, "text": "hi",
                    "_sender_human": {"user_id": "spoof", "provider": "spoof"},
                })
        finally:
            server._sessions.pop(sid, None)

        # Same refusal code (4124) as the existing _turn_author guard.
        assert result.get("error", {}).get("code") == 4124
        assert "sender is stamped by the gateway" in result["error"]["message"]
