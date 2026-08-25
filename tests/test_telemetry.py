"""tool_events capture — args hashing, redaction, fail-open, CLI entry.

Covers the tool_events table (2026-07-31 agent-observability §2.1): canonical-
JSON hashing, credential redaction BEFORE storage, the fail-open contract, and
the `python3 -m gaius.telemetry tool-event` entry the gaius-observe hook calls.
All tests use a tmp DB via monkeypatched _DB_PATH or the GAIUS_TELEMETRY_DB
env override — the real ~/.gaius/telemetry.db is never touched.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

from gaius import telemetry as t

FAKE_GH = "ghp_FAKE123abcdefghijklmnopqrstuv0123"
FAKE_ANT = "sk-ant-FAKEapi03-abcdefghijklmnop"


def _fresh(tmp_path, monkeypatch):
    """Point telemetry at a tmp DB and drop the cached module connection."""
    monkeypatch.setattr(t, "_DB_PATH", tmp_path / "telemetry.db")
    monkeypatch.setattr(t, "_conn", None)


def _rows(tmp_path):
    c = sqlite3.connect(str(tmp_path / "telemetry.db"))
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM tool_events ORDER BY id")]


# ── table + insert ───────────────────────────────────────────────────────────
def test_tool_event_row_lands(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tool_input = {"command": "ls -la /tmp"}
    t.log_tool_event("sess-1", "Bash", tool_input,
                     source="hook", event="pre", project="abc123def456")
    rows = _rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "sess-1"
    assert r["tool_name"] == "Bash"
    assert r["source"] == "hook"
    assert r["event"] == "pre"
    assert r["project"] == "abc123def456"
    assert r["ts"] > 0
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    assert r["args_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert "ls -la /tmp" in r["args_redacted"]


def test_hash_is_key_order_independent():
    a = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    b = {"nested": {"x": 1, "y": 2}, "a": 1, "b": 2}
    assert t.hash_args(a) == t.hash_args(b)
    assert t.hash_args(a) != t.hash_args({"a": 1})


# ── redaction: patterns run BEFORE storage ───────────────────────────────────
def test_redaction_github_token_never_stored(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("sess-2", "Bash", {"command": f"export GH_TOKEN={FAKE_GH}"})
    r = _rows(tmp_path)[0]
    assert FAKE_GH not in r["args_redacted"]
    assert "[REDACTED]" in r["args_redacted"]
    # hash is of the pre-redaction payload — still deterministic, still opaque
    assert FAKE_GH not in r["args_sha256"]


def test_redaction_known_secret_shapes():
    cases = [
        FAKE_ANT,
        "github_pat_11ABCDEFG0123456789abcdefgh",
        "xoxb-1234567890-abcdefghijk",
        "AKIAIOSFODNN7EXAMPLE",
        "tskey-auth-kFAKEfake12345",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl",
        "Bearer abcdef0123456789abcdef",
    ]
    for secret in cases:
        out = t.redact_args({"command": f"curl -H 'X: {secret}' https://x"})
        assert secret not in out, f"unredacted: {secret[:12]}…"
        assert "[REDACTED]" in out


def test_redaction_private_key_block():
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEFAKEFAKE\n-----END OPENSSH PRIVATE KEY-----"
    out = t.redact_args({"content": key})
    assert "FAKEFAKEFAKE" not in out


def test_redaction_sensitive_key_names():
    out = t.redact_args({"api_key": "plainvalue123", "vault_pass": "hunter2hunter2",
                         "file_path": "/etc/hosts"})
    assert "plainvalue123" not in out
    assert "hunter2hunter2" not in out
    assert "/etc/hosts" in out          # non-sensitive values survive


def test_redaction_key_value_assignment():
    out = t.redact_args({"command": "mysql --password=supersecretpw1 -h db"})
    assert "supersecretpw1" not in out


def test_redacted_summary_truncated():
    out = t.redact_args({"content": "x" * 10_000})
    assert len(out) < 700
    assert "chars]" in out


# ── fail-open contract ───────────────────────────────────────────────────────
def test_log_tool_event_fail_open(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(t, "_get_conn", _boom)
    # must not raise
    t.log_tool_event("sess-3", "Bash", {"command": "ls"})


def test_log_tool_event_unserializable_input(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("sess-4", "Weird", {"blob": b"\x00\x01", "s": {1, 2}})
    assert len(_rows(tmp_path)) == 1   # default=str fallback, still logged


# ── before/after state capture (BA-1, 2026-08-13) ────────────────────────────
def test_state_and_result_hashes_land(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("sess-ba", "Edit", {"file_path": "/x", "old_string": "a"},
                     event="post", state_sha256="deadbeef" * 8,
                     result_sha256="cafef00d" * 8)
    r = _rows(tmp_path)[0]
    assert r["state_sha256"] == "deadbeef" * 8
    assert r["result_sha256"] == "cafef00d" * 8


def test_state_fields_default_null(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("sess-ba2", "Bash", {"command": "ls"})
    r = _rows(tmp_path)[0]
    assert r["state_sha256"] is None
    assert r["result_sha256"] is None


def test_migration_adds_columns_to_preexisting_db(tmp_path, monkeypatch):
    """A DB created before BA-1 (no state/result columns, existing rows) must
    gain the columns via ALTER TABLE and keep its rows — the real
    ~/.gaius/telemetry.db has 46k+ pre rows CREATE TABLE IF NOT EXISTS
    cannot touch."""
    db = tmp_path / "telemetry.db"
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE tool_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, session_id TEXT, tool_name TEXT,
        args_sha256 TEXT, args_redacted TEXT,
        source TEXT DEFAULT 'hook', event TEXT DEFAULT 'pre', project TEXT)""")
    c.execute("INSERT INTO tool_events (ts, session_id, tool_name) VALUES (1.0, 'old', 'Bash')")
    c.commit()
    c.close()
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("sess-mig", "Edit", {"file_path": "/y"},
                     event="post", state_sha256="abc123", result_sha256="def456")
    rows = _rows(tmp_path)
    assert len(rows) == 2                       # pre-existing row survives
    assert rows[0]["session_id"] == "old"
    assert rows[0]["state_sha256"] is None      # NULL = "not captured then"
    assert rows[1]["state_sha256"] == "abc123"
    assert rows[1]["result_sha256"] == "def456"


def test_failed_migration_degrades_to_legacy_insert(tmp_path, monkeypatch):
    """If the ALTERs never landed (locked/readonly DB at init), rows must still
    be logged with the legacy column list — NOT silently dropped for the life
    of the cached connection (the long-lived MCP server never re-inits)."""
    _fresh(tmp_path, monkeypatch)
    t.log_tool_event("warm", "Bash", {"command": "true"})   # init schema normally
    monkeypatch.setattr(t, "_BA1_COLS_OK", False)
    t.log_tool_event("degraded", "Edit", {"file_path": "/x"},
                     event="post", state_sha256="zz", result_sha256="yy",
                     call_id="toolu_x")
    rows = _rows(tmp_path)
    assert len(rows) == 2                       # the row landed anyway
    assert rows[1]["session_id"] == "degraded"
    assert rows[1]["state_sha256"] is None      # new fields dropped, row kept
    assert rows[1]["call_id"] is None


# ── CLI entry (what gaius-observe pipes into) ────────────────────────────────
def _run_cli(tmp_path, payload, args=("pre", "proj12345678"), extra_env=None):
    env = {**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")}
    env.pop("GAIUS_STATE_SHA256", None)   # isolate from any ambient hook env
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "tool-event", *args],
        input=json.dumps(payload).encode(), env=env,
        capture_output=True, timeout=30,
    )


def test_cli_claude_envelope(tmp_path):
    proc = _run_cli(tmp_path, {
        "session_id": "sess-cli", "cwd": "/tmp", "tool_name": "Bash",
        "tool_input": {"command": f"git push https://x:{FAKE_GH}@github.com/x/y"},
    })
    assert proc.returncode == 0
    assert proc.stdout == b"" and proc.stderr == b""   # silent — hook contract
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-cli"
    assert rows[0]["tool_name"] == "Bash"
    assert rows[0]["project"] == "proj12345678"
    assert FAKE_GH not in rows[0]["args_redacted"]


def test_cli_grok_envelope(tmp_path):
    proc = _run_cli(tmp_path, {
        "sessionId": "sess-grok", "cwd": "/tmp", "toolName": "run_terminal_command",
        "toolInput": {"command": "echo hi"},
    })
    assert proc.returncode == 0
    rows = _rows(tmp_path)
    assert rows[0]["session_id"] == "sess-grok"
    assert rows[0]["tool_name"] == "run_terminal_command"


def test_cli_pre_post_pair_share_args_hash(tmp_path):
    """The BA-1 join contract: a pre row and its post row carry the SAME
    args_sha256 (hash covers tool_input only — the post envelope's added
    tool_response must not perturb it), so (session_id, args_sha256) pairs
    before/after with no new plumbing."""
    tool_input = {"file_path": "/tmp/f.txt", "content": "hello"}
    pre_env = {"session_id": "sess-pair", "tool_name": "Write",
               "tool_input": tool_input, "tool_use_id": "toolu_pair1"}
    post_env = {**pre_env, "tool_response": {"success": True, "filePath": "/tmp/f.txt"}}
    assert _run_cli(tmp_path, pre_env, args=("pre", "p1")).returncode == 0
    assert _run_cli(tmp_path, post_env, args=("post", "p1"),
                    extra_env={"GAIUS_STATE_SHA256": "f" * 64}).returncode == 0
    rows = _rows(tmp_path)
    assert len(rows) == 2
    pre, post = rows
    assert (pre["event"], post["event"]) == ("pre", "post")
    assert pre["args_sha256"] == post["args_sha256"]        # the fallback join key
    assert pre["call_id"] == post["call_id"] == "toolu_pair1"   # the exact key
    assert post["result_sha256"] == t.hash_args({"success": True, "filePath": "/tmp/f.txt"})
    assert post["state_sha256"] == "f" * 64                 # from GAIUS_STATE_SHA256
    assert pre["result_sha256"] is None                     # no response pre-call


def test_cli_response_hashed_never_stored(tmp_path):
    """tool_response content must never land in the DB — only its sha256."""
    marker = "UNIQUE-RESPONSE-CONTENT-xyzzy"
    proc = _run_cli(tmp_path, {
        "session_id": "s", "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/r"},
        "tool_response": {"output": marker},
    }, args=("post", "p2"))
    assert proc.returncode == 0
    r = _rows(tmp_path)[0]
    assert len(r["result_sha256"]) == 64
    dump = json.dumps(r)
    assert marker not in dump


def test_cli_grok_camelcase_response(tmp_path):
    proc = _run_cli(tmp_path, {
        "sessionId": "sess-grok-post", "toolName": "edit_file",
        "toolInput": {"path": "/tmp/g"}, "toolResponse": {"ok": True},
    }, args=("post", "p3"))
    assert proc.returncode == 0
    r = _rows(tmp_path)[0]
    assert r["result_sha256"] == t.hash_args({"ok": True})


def test_cli_garbage_stdin_exits_zero(tmp_path):
    env = {**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")}
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "tool-event", "pre"],
        input=b"not json at all {{{", env=env, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == b""
