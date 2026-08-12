"""Attribution threading (Praefectus multi-corpus Phase 3 precondition).

mcp_server used to pin agent="operator", collapsing every routed writer into
one agent and defeating upsert_fact's multi-writer corroboration (it counts
DISTINCT agents/sessions). These tests prove the fix threads real identity
while preserving legacy behavior by default. (The http_adapter half of this
suite moved out with the adapter.)
"""

import os

# Force clean config BEFORE importing gaius — mirrors tests/test_core.py:23. This module is
# collected before test_core (alphabetical), so without this its `from gaius import _core` would
# load the operator's real ~/.gaius/config.yaml into _core's cached globals and pollute later
# tests' clean-default assertions (AGENT_THRESHOLDS, _DEFAULT_PRINCIPAL).
os.environ["GAIUS_CONFIG"] = "/dev/null"

import hashlib
import importlib.util
import json

import pytest

from gaius import _core

# These tests exercise an optional-extra surface (mcp_server needs [mcp]). The dev
# extra deliberately ships only pytest — skip, don't fail, when the extra isn't
# installed so the base suite stays green.
requires_mcp = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional [mcp] extra not installed")


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


