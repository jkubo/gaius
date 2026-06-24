"""Regression: peer-agent (Grok/Codex) retire must PROMOTE to facts.db, not just stage.

Guards the 2026-06-19 gap where ``_retire_event_sessions`` staged peer events to
``~/.gaius/staged/grok-facts/`` but never called ``upsert_fact``, so 0 grok/codex
facts ever reached the searchable corpus despite the sessions being "ingested"
(9 grok sessions stranded 06-17→06-19). Also guards the domain-ranking fix.

Run:
    pytest tests/test_peer_promote.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

# Blank config so built-in default keywords are used (deterministic).
os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod  # noqa: E402
from gaius._core import (  # noqa: E402
    init_db,
    _retire_event_sessions,
    parse_grok_events,
    _discover_grok_sessions,
    tag_domains_from_specs,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the live DB or staging dir."""
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")
    monkeypatch.setattr(_gaius_mod, "STAGING_DIR", tmp_path / "staged")


def _grok_session(root: Path, uuid: str, answer: str,
                  query: str = "What threats are active right now?") -> Path:
    sess = root / "%2Fhome%2Fjkubo%2Fgrok-sweeps" / uuid
    sess.mkdir(parents=True, exist_ok=True)
    with open(sess / "chat_history.jsonl", "w") as f:
        f.write(json.dumps({"type": "user",
                            "content": f"<user_query>{query}</user_query>"}) + "\n")
        f.write(json.dumps({"type": "assistant", "content": answer}) + "\n")
    return sess


def test_peer_retire_promotes_to_corpus(tmp_path):
    """The event must land in facts.db, not merely in staging."""
    conn = init_db(_gaius_mod.DB_PATH)
    answer = ("Active threat: a malware campaign distributing infostealer payloads "
              "via a supply-chain backdoor; multiple ransomware leak-site posts. " * 3)
    _grok_session(tmp_path / "grok", "019eded0-test", answer)

    n = _retire_event_sessions(tmp_path / "grok", parse_grok_events, "grok-facts",
                               "grok", conn, discover_fn=_discover_grok_sessions)
    assert n >= 1, "session should yield >= 1 event"

    rows = conn.execute(
        "SELECT fact_text, source FROM facts WHERE source = 'grok'"
    ).fetchall()
    assert rows, "peer retire staged but did NOT promote to facts.db (the #2 regression)"
    assert any("infostealer" in r[0] for r in rows)


def test_peer_retire_idempotent(tmp_path):
    """Re-running must not duplicate: session-UUID dedup skips processed sessions."""
    conn = init_db(_gaius_mod.DB_PATH)
    answer = ("A new advanced persistent threat campaign uses a rootkit and a "
              "cobalt strike beacon for command and control of victims. " * 3)
    _grok_session(tmp_path / "grok", "019edaaa-test", answer)
    args = (tmp_path / "grok", parse_grok_events, "grok-facts", "grok", conn)

    _retire_event_sessions(*args, discover_fn=_discover_grok_sessions)
    first = conn.execute("SELECT count(*) FROM facts WHERE source='grok'").fetchone()[0]
    _retire_event_sessions(*args, discover_fn=_discover_grok_sessions)
    second = conn.execute("SELECT count(*) FROM facts WHERE source='grok'").fetchone()[0]

    assert first >= 1, "first run should promote"
    assert second == first, "re-running peer retire must not duplicate facts"


def test_tag_domains_ranks_by_hit_count():
    """Best-match wins (not first-in-dict); single-match and no-match unchanged."""
    specs = {
        "networking": ["dns", "route", "proxy", "tunnel"],
        "security": ["malware", "infostealer", "campaign", "adversary"],
    }
    # 1 networking hit ('dns') vs 4 security hits → security must rank first
    text = "a malware campaign by an adversary using dns tunnel for C2 plus infostealer"
    assert tag_domains_from_specs(text, specs)[0] == "security"
    # single-match and no-match behaviour is unchanged
    assert tag_domains_from_specs("only dns and route discussed", specs) == ["networking"]
    assert tag_domains_from_specs("nothing relevant here", specs) == []


def test_all_tag_domains_callers_pass_two_args():
    """Source guard: every tag_domains_from_specs(...) call must supply domain_specs.

    cmd_ansible and cmd_aliases once passed a single arg, which raised TypeError on
    their live (non-dry-run) upsert path (the function has two required params and no
    defaults). Fixed 2026-06-19; this keeps the whole call class honest.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(_gaius_mod.__file__).read_text())
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "tag_domains_from_specs"):
            has_specs = len(node.args) >= 2 or any(kw.arg == "domain_specs" for kw in node.keywords)
            if not has_specs:
                bad.append(node.lineno)
    assert not bad, f"tag_domains_from_specs called with <2 args at _core.py lines {bad}"
