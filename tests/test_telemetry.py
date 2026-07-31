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


# ── CLI entry (what gaius-observe pipes into) ────────────────────────────────
def _run_cli(tmp_path, payload, args=("pre", "proj12345678")):
    env = {**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")}
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


def test_cli_garbage_stdin_exits_zero(tmp_path):
    env = {**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")}
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "tool-event", "pre"],
        input=b"not json at all {{{", env=env, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == b""
