"""BA-6 profile provenance — hash-only, fail-open, no content exfil.

All tests use an isolated HOME + tmp telemetry DB. The real
~/.gaius/telemetry.db and ~/.claude/ are never touched.
"""
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from gaius import profile as p
from gaius import telemetry as t

MARKER = "UNIQUE-PROFILE-SECRET-ghp_FAKESECRETVALUE99"
FAKE_TOKEN = "ghp_FAKE123abcdefghijklmnopqrstuv0123"


def _home(tmp_path):
    """Isolated HOME. Tests MUST put cwd under this path so the walk
    stops at home (a sibling cwd would climb to /tmp and /)."""
    h = tmp_path / "home"
    (h / ".claude" / "skills").mkdir(parents=True)
    (h / ".claude").mkdir(exist_ok=True)
    return h


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "_DB_PATH", tmp_path / "telemetry.db")
    monkeypatch.setattr(t, "_conn", None)


def _sess_rows(tmp_path):
    c = sqlite3.connect(str(tmp_path / "telemetry.db"))
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM session_profiles")]


def test_hash_stable_and_key_order_independent(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", "hello\n")
    a = p.build_profile(cwd=cwd, home=home, model="opus")
    b = p.build_profile(cwd=cwd, home=home, model="opus")
    assert p.profile_sha256(cwd=cwd, home=home, model="opus") == \
        p.profile_sha256(cwd=cwd, home=home, model="opus")
    assert a == b
    assert len(p.profile_sha256(cwd=cwd, home=home, model="opus")) == 64


def test_claude_md_content_change_changes_hash(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", "v1\n")
    h1 = p.profile_sha256(cwd=cwd, home=home, model="opus")
    _write(cwd / "CLAUDE.md", "v2\n")
    h2 = p.profile_sha256(cwd=cwd, home=home, model="opus")
    assert h1 != h2


def test_skill_body_ignored_name_counted(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    skill = home / ".claude" / "skills" / "ops"
    _write(skill / "SKILL.md", "body v1 with " + MARKER)
    h1 = p.profile_sha256(cwd=cwd, home=home, model="opus")
    _write(skill / "SKILL.md", "body v2 DIFFERENT " + MARKER)
    h2 = p.profile_sha256(cwd=cwd, home=home, model="opus")
    assert h1 == h2
    names = p.collect_skill_names(cwd, home=home)
    assert names == ["ops"]
    extra = home / ".claude" / "skills" / "sre"
    _write(extra / "SKILL.md", "other")
    h3 = p.profile_sha256(cwd=cwd, home=home, model="opus")
    assert h3 != h1


def test_mcp_env_and_headers_never_in_document(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    _write(home / ".claude.json", json.dumps({
        "mcpServers": {
            "gaius": {
                "type": "stdio",
                "command": "/opt/python",
                "args": ["-m", "gaius.mcp_server"],
                "env": {"GITHUB_TOKEN": FAKE_TOKEN, "vault_pass": "hunter2"},
            },
            "orch": {
                "type": "http",
                "url": "https://user:s3cret@orch.example.com/mcp?token=abc",
                "headers": {"Authorization": "Bearer " + FAKE_TOKEN},
            },
        }
    }))
    doc = p.build_profile(cwd=cwd, home=home, model="opus")
    blob = json.dumps(doc)
    assert FAKE_TOKEN not in blob
    assert "hunter2" not in blob
    assert "s3cret" not in blob
    assert "Authorization" not in blob
    assert "env" not in blob
    assert "headers" not in blob
    urls = [m.get("url") for m in doc["mcp"]]
    assert "https://orch.example.com/mcp" in urls
    assert any("user:" in (u or "") for u in urls) is False


def test_mcp_secret_shaped_arg_redacted(tmp_path):
    cfg = {"type": "stdio", "command": "x", "args": ["--token=abc123", "ok"]}
    out = p.mcp_structural("n", cfg)
    assert out["args"][0] == "[REDACTED]"
    assert out["args"][1] == "ok"


def test_mcp_command_token_redacted():
    out = p.mcp_structural("n", {
        "type": "stdio",
        "command": FAKE_TOKEN,
        "args": ["sk-ant-FAKEapi03-abcdefghijklmnop"],
    })
    assert FAKE_TOKEN not in json.dumps(out)
    assert "sk-ant-" not in json.dumps(out)
    assert out["command"] == "[REDACTED]"
    assert out["args"][0] == "[REDACTED]"


def test_walk_stops_at_home_not_root(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj" / "nested"
    cwd.mkdir(parents=True)
    _write(cwd / "CLAUDE.md", "inner\n")
    _write(home / "CLAUDE.md", "home-level\n")
    _write(tmp_path / "CLAUDE.md", "OUTSIDE\n")  # sibling of home — must NOT be hashed
    files = p.collect_instruction_files(cwd, home=home)
    texts = {f.read_text() for f in files}
    assert "OUTSIDE\n" not in texts
    assert "inner\n" in texts
    assert "home-level\n" in texts


def test_walk_outside_home_does_not_climb_to_root(tmp_path):
    home = _home(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "CLAUDE.md", "proj\n")
    _write(tmp_path / "CLAUDE.md", "parent-of-outside\n")
    files = p.collect_instruction_files(outside, home=home)
    texts = {f.read_text() for f in files if f.exists()}
    assert "proj\n" in texts
    assert "parent-of-outside\n" not in texts


def test_model_and_sampling_change_hash(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    _write(home / ".claude" / "settings.json", json.dumps({
        "model": "opus", "effortLevel": "low",
    }))
    h1 = p.profile_sha256(cwd=cwd, home=home)
    _write(home / ".claude" / "settings.json", json.dumps({
        "model": "sonnet", "effortLevel": "low",
    }))
    h2 = p.profile_sha256(cwd=cwd, home=home)
    _write(home / ".claude" / "settings.json", json.dumps({
        "model": "sonnet", "effortLevel": "high",
    }))
    h3 = p.profile_sha256(cwd=cwd, home=home)
    assert h1 != h2 != h3
    assert h1 != h3


def test_inventory_never_contains_file_body(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", MARKER)
    doc = p.build_profile(cwd=cwd, home=home, model="opus")
    digest = p.profile_sha256(cwd=cwd, home=home, model="opus")
    text = p.inventory_lines(doc, digest)
    assert MARKER not in text
    assert MARKER not in json.dumps(doc)
    assert digest in text


def test_merge_otel_resource_pin_wins():
    d = "a" * 64
    pin = "b" * 64
    assert p.merge_otel_resource("", d) == f"kub0.profile.sha256={d}"
    assert p.merge_otel_resource("fleet=kub0", d) == f"fleet=kub0,kub0.profile.sha256={d}"
    pinned = f"fleet=kub0,kub0.profile.sha256={pin}"
    assert p.merge_otel_resource(pinned, d) == pinned
    invalid = "fleet=kub0,kub0.profile.sha256=bbbb"
    assert p.merge_otel_resource(invalid, d) == f"fleet=kub0,kub0.profile.sha256={d}"


def test_over_cap_sentinel(tmp_path):
    f = tmp_path / "big"
    f.write_bytes(b"x")
    # Don't write 64MB; poke the helper with a monkeypatched stat via a tiny file
    # and a lowered cap.
    old = p.HASH_CAP
    p.HASH_CAP = 0   # 1-byte file must exceed a 0-byte cap
    try:
        assert p.file_sha256(f).startswith("over-cap:")
    finally:
        p.HASH_CAP = old


def test_log_session_profile_digest_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_session_profile("sess-1", "c" * 64, cwd="/tmp/proj",
                          harness="claude", model="opus", source="hook")
    rows = _sess_rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "sess-1"
    assert r["profile_sha256"] == "c" * 64
    dump = json.dumps(r)
    assert MARKER not in dump


def test_log_session_profile_upsert(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t.log_session_profile("sess-1", "a" * 64, source="hook")
    t.log_session_profile("sess-1", "b" * 64, source="hook")
    rows = _sess_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["profile_sha256"] == "b" * 64


def test_log_session_profile_skips_unknown(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t._get_conn()  # create schema so the SELECT below is defined
    t.log_session_profile("unknown", "a" * 64)
    t.log_session_profile("", "a" * 64)
    t.log_session_profile("sess", "")
    assert _sess_rows(tmp_path) == []


def test_log_session_profile_fail_open(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(t, "_get_conn", _boom)
    t.log_session_profile("sess", "a" * 64)  # must not raise


def test_session_profiles_created_on_preexisting_db(tmp_path, monkeypatch):
    db = tmp_path / "telemetry.db"
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE tool_events (
        id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, tool_name TEXT)""")
    c.execute("INSERT INTO tool_events (ts, session_id, tool_name) VALUES (1,'old','Bash')")
    c.commit()
    c.close()
    _fresh(tmp_path, monkeypatch)
    t.log_session_profile("sess-mig", "d" * 64)
    rows = _sess_rows(tmp_path)
    assert rows[0]["profile_sha256"] == "d" * 64
    c = sqlite3.connect(str(db))
    n = c.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0]
    assert n == 1


def _run_cli(tmp_path, cmd, payload=None, extra_env=None, cwd=None):
    env = {**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", *cmd],
        input=(json.dumps(payload).encode() if payload is not None else None),
        env=env, cwd=str(cwd or tmp_path),
        capture_output=True, timeout=30,
    )


def test_cli_session_start_claude_envelope(tmp_path, monkeypatch):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", "x\n")
    env = {
        "HOME": str(home),
        "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "session-start"],
        input=json.dumps({
            "session_id": "sess-cli", "cwd": str(cwd), "source": "startup",
        }).encode(),
        env={**os.environ, **env},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == b""
    # Isolated HOME means the child process hashes under that HOME.
    rows = _sess_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-cli"
    assert rows[0]["harness"] == "claude"
    assert len(rows[0]["profile_sha256"]) == 64
    assert MARKER not in json.dumps(dict(rows[0]))


def test_cli_session_start_grok_envelope(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "session-start"],
        input=json.dumps({
            "sessionId": "sess-grok", "cwd": str(cwd),
        }).encode(),
        env={**os.environ, "HOME": str(home),
             "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    rows = _sess_rows(tmp_path)
    assert rows[0]["session_id"] == "sess-grok"
    assert rows[0]["harness"] == "grok"


def test_cli_session_start_secret_not_in_db_or_stdio(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", MARKER)
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "session-start"],
        input=json.dumps({"session_id": "s", "cwd": str(cwd)}).encode(),
        env={**os.environ, "HOME": str(home),
             "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert MARKER.encode() not in proc.stdout
    assert MARKER.encode() not in proc.stderr
    dump = json.dumps(_sess_rows(tmp_path))
    assert MARKER not in dump


def test_cli_garbage_session_start_exits_zero(tmp_path):
    proc = _run_cli(tmp_path, ["session-start"])
    # empty stdin is not json
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "session-start"],
        input=b"not json {{{",
        env={**os.environ, "GAIUS_TELEMETRY_DB": str(tmp_path / "telemetry.db")},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_cli_profile_hash_is_hex_only(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    _write(cwd / "CLAUDE.md", MARKER)
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "profile-hash",
         "--cwd", str(cwd)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode().strip()
    assert len(out) == 64 and all(c in "0123456789abcdef" for c in out)
    assert MARKER.encode() not in proc.stdout


def test_cli_profile_env_honors_kub0_pin(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    pin = "e" * 64
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "profile-env", "--cwd", str(cwd)],
        env={**os.environ, "HOME": str(home),
             "KUB0_PROFILE_SHA256": pin,
             "OTEL_RESOURCE_ATTRIBUTES": "fleet=kub0"},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == f"fleet=kub0,kub0.profile.sha256={pin}"


def test_cli_profile_env_merges(tmp_path):
    home = _home(tmp_path)
    cwd = home / "proj"
    cwd.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "gaius.telemetry", "profile-env", "--cwd", str(cwd)],
        env={**os.environ, "HOME": str(home),
             "OTEL_RESOURCE_ATTRIBUTES": "fleet=kub0"},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    line = proc.stdout.decode().strip()
    assert line.startswith("fleet=kub0,kub0.profile.sha256=")
    digest = line.split("=", 2)[-1] if line.count("=") >= 2 else ""
    # last kv
    digest = line.rsplit("kub0.profile.sha256=", 1)[1]
    assert len(digest) == 64


# test_stamp_hook_fail_open_and_silent moved to tests/test_profile_devonly.py:
# the stamp hook lives at the memory-repo root, outside the publish surface.
