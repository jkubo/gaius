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
    sess = root / "%2Fhome%2Fuser%2Fsweeps" / uuid
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


def _iter_package_calls(func_name):
    """Yield (relpath, ast.Call) for every call to `func_name` across the WHOLE package.

    Deliberately not a single file. The original version of the guard below ASTed only
    ``_core.py``; when cmd_ansible/cmd_aliases moved to ``ingest.py`` in the 2026-08
    modularization the walk started matching zero Call nodes, so its `bad` list was
    always empty and the assert went permanently, silently green. A refactor must not
    be able to un-audit the boundary it moved — hence package-wide, plus the
    anti-vacuity assertions in each caller.
    """
    import ast
    from pathlib import Path

    pkg_dir = Path(_gaius_mod.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == func_name):
                yield f"{path.name}:{node.lineno}", node


def test_all_tag_domains_callers_pass_two_args():
    """Source guard: every tag_domains_from_specs(...) call must supply domain_specs.

    cmd_ansible and cmd_aliases once passed a single arg, which raised TypeError on
    their live (non-dry-run) upsert path (the function has two required params and no
    defaults). Fixed 2026-06-19; this keeps the whole call class honest.
    """
    seen, bad = [], []
    for site, node in _iter_package_calls("tag_domains_from_specs"):
        seen.append(site)
        has_specs = (len(node.args) >= 2
                     or any(kw.arg == "domain_specs" for kw in node.keywords))
        if not has_specs:
            bad.append(site)
    assert seen, "guard matched 0 call sites — it has gone VACUOUS, repoint it"
    assert not bad, f"tag_domains_from_specs called with <2 args at {bad}"


def test_all_upsert_fact_callers_bind():
    """Source guard: every upsert_fact(...) call must satisfy the real signature.

    Guards GA-01 (2026-08-13): ``upsert_fact`` gained a required positional
    ``session_uuid`` at slot 6, but all five ingest.py call sites passed their args
    by keyword and omitted it. Nothing failed at import — each site raised TypeError
    only when reached, and two of them (the playbook-summary and gen_corpus legs) sat
    inside ``except Exception: pass``, so ``gaius ansible`` / ``gaius aliases`` printed
    "Extracted N facts" and exited 0 while writing zero rows. ``--dry-run`` skips the
    call entirely and so masked it.

    Bind-checks against ``inspect.signature`` rather than a hardcoded arg list, so it
    keeps holding when the signature changes again.
    """
    import ast
    import inspect

    from gaius._core import upsert_fact

    sig = inspect.signature(upsert_fact)
    seen, bad, skipped = [], [], []
    for site, node in _iter_package_calls("upsert_fact"):
        # *args / **kwargs unpacking cannot be resolved statically — record, don't guess.
        if any(isinstance(a, ast.Starred) for a in node.args) or \
           any(kw.arg is None for kw in node.keywords):
            skipped.append(site)
            continue
        seen.append(site)
        try:
            sig.bind(*[None] * len(node.args),
                     **{kw.arg: None for kw in node.keywords})
        except TypeError as e:
            bad.append(f"{site} ({e})")
    assert seen, "guard matched 0 call sites — it has gone VACUOUS, repoint it"
    assert not bad, ("upsert_fact call sites do not match its signature: "
                     + "; ".join(bad))
