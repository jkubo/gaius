"""Attribution threading (Praefectus multi-corpus Phase 3 precondition).

The write paths used to hardcode the writer identity — http_adapter pinned
session_uuid="http-adapter" and mcp_server pinned agent="operator" — which
collapsed every routed writer into one session/agent and defeated upsert_fact's
multi-writer corroboration (it counts DISTINCT agents/sessions). These tests
prove the fix threads real identity while preserving legacy behavior by default.
"""

import os

# Force clean config BEFORE importing gaius — mirrors tests/test_core.py:23. This module is
# collected before test_core (alphabetical), so without this its `from gaius import _core` would
# load the operator's real ~/.gaius/config.yaml into _core's cached globals and pollute later
# tests' clean-default assertions (AGENT_THRESHOLDS, _DEFAULT_PRINCIPAL).
os.environ["GAIUS_CONFIG"] = "/dev/null"

import asyncio
import hashlib
import importlib.util
import json

import pytest

from gaius import _core

# These tests exercise optional-extra surfaces (mcp_server needs [mcp]; http_adapter
# needs [http]). The dev extra deliberately ships only pytest — skip, don't fail,
# when the extras aren't installed so the base suite stays green.
requires_mcp = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional [mcp] extra not installed")
requires_http = pytest.mark.skipif(
    importlib.util.find_spec("httpx") is None or importlib.util.find_spec("fastapi") is None
    or importlib.util.find_spec("http_adapter") is None,
    reason="optional [http] extra not installed / http_adapter not in this build")


def _fact_row(db_path, fact_text):
    fk = hashlib.sha256(fact_text.encode()).hexdigest()[:32]
    conn = _core.init_db(db_path)
    return conn.execute(
        "SELECT agents, sessions, confirmation_count FROM facts "
        "WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fk,),
    ).fetchone()


@requires_mcp
class TestMCPAttribution:
    def test_distinct_sessions_corroborate_distinctly(self, tmp_path, monkeypatch):
        db = tmp_path / "facts.db"
        monkeypatch.setattr(_core, "DB_PATH", db)
        monkeypatch.setenv("GAIUS_AGENT", "agent-x")
        from gaius import mcp_server

        text = "The widget cache TTL is sixty seconds in the default profile."
        mcp_server.gaius_fact_add(text, "jdt", source="sess-a")
        mcp_server.gaius_fact_add(text, "jdt", source="sess-b")

        row = _fact_row(db, text)
        assert set(json.loads(row["sessions"])) == {"sess-a", "sess-b"}, "writers collapsed"
        assert "agent-x" in json.loads(row["agents"]), "GAIUS_AGENT not threaded"
        assert row["confirmation_count"] >= 2

    def test_default_preserves_legacy_identity(self, tmp_path, monkeypatch):
        db = tmp_path / "facts.db"
        monkeypatch.setattr(_core, "DB_PATH", db)
        monkeypatch.delenv("GAIUS_AGENT", raising=False)
        monkeypatch.delenv("GAIUS_SESSION_UUID", raising=False)
        from gaius import mcp_server

        text = "Legacy default fact about block storage on the default nodes."
        mcp_server.gaius_fact_add(text, "storage")  # source defaults to "session"

        row = _fact_row(db, text)
        assert json.loads(row["agents"]) == ["operator"], "default agent changed"
        assert json.loads(row["sessions"]) == ["session"], "default session changed"


@requires_http
class TestAdapterAttribution:
    def test_threads_body_identity(self, tmp_path, monkeypatch):
        db = tmp_path / "facts.db"
        monkeypatch.setattr(_core, "DB_PATH", db)
        import http_adapter
        monkeypatch.setattr(http_adapter, "_check_auth", lambda req: None)

        text = "Adapter-routed fact: the customer dedup key is the lowercased email address."
        b1 = http_adapter.FactAddRequest(fact_text=text, domain="jdt", agent="alice@example", session_uuid="s-a")
        b2 = http_adapter.FactAddRequest(fact_text=text, domain="jdt", agent="bob@example", session_uuid="s-b")
        asyncio.run(http_adapter.fact_add(object(), b1))
        asyncio.run(http_adapter.fact_add(object(), b2))

        row = _fact_row(db, text)
        assert set(json.loads(row["sessions"])) == {"s-a", "s-b"}, "adapter collapsed sessions"
        assert set(json.loads(row["agents"])) == {"alice@example", "bob@example"}, "adapter ignored body.agent"

    def test_default_preserves_legacy(self, tmp_path, monkeypatch):
        db = tmp_path / "facts.db"
        monkeypatch.setattr(_core, "DB_PATH", db)
        import http_adapter
        monkeypatch.setattr(http_adapter, "_check_auth", lambda req: None)

        text = "Legacy adapter fact with no explicit identity fields supplied by the caller."
        body = http_adapter.FactAddRequest(fact_text=text, domain="general")  # source defaults to "session"
        asyncio.run(http_adapter.fact_add(object(), body))

        row = _fact_row(db, text)
        assert json.loads(row["sessions"]) == ["http-adapter"], "default session changed"
        assert json.loads(row["agents"]) == ["session"], "default agent changed"
