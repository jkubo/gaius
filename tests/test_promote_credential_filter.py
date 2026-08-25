"""GA-02 — Claude-session mining must not write credential-shaped text into facts.db.

``gaius/retire.py`` imported ``CREDENTIAL_PATTERNS`` at module top but had ZERO call
sites for it in 1637 lines, while every peer parser in ``gaius/parsers.py``
(:232, :266, :292, :319, :401, :528, :597) filters on it before emitting an event.
Two Claude-specific paths were therefore unguarded:

  1. ``_promote_mined_to_facts`` — filtered only ``_is_noise`` before upsert.
  2. ``cmd_retire --claude-shim`` — ``parse_claude_events`` filters boilerplate and
     ``_is_noise`` but, unlike its siblings in that module, not credentials. Its
     ``outcome`` field is up to 2000 chars of RAW tool_result, so a guard that
     checked only ``fact_text`` (a 500-char summary line) would still leak.

Blast radius is why this matters more than one row: facts.db is S3-synced nightly and
injected into future sessions of every agent, so a hit persists cross-agent and
cross-machine.

Run:
    pytest tests/test_promote_credential_filter.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod  # noqa: E402
from gaius._core import init_db, _promote_mined_to_facts  # noqa: E402
from gaius.extract import CREDENTIAL_PATTERNS  # noqa: E402
from gaius.retire import cmd_retire  # noqa: E402

# Long enough to clear the len() < 80 promotion filter, and shaped to survive _is_noise.
_SECRET_BLOCK = (
    "Recovered the runner by exporting FORGEJO_TOKEN=gha_ZZZZexampleonlyZZZZ before "
    "re-running the deploy job against the staging cluster endpoint."
)
_CLEAN_BLOCK = (
    "The deploy job failed because the runner image lacked the git-lfs binary, so the "
    "checkout step silently produced pointer files instead of real assets."
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)
    return init_db(db_path)


def test_credential_block_is_not_promoted(conn):
    """The regression itself."""
    n = _promote_mined_to_facts(conn, "sess-ga02", {"key_concepts": f"- {_SECRET_BLOCK}"})
    rows = conn.execute("SELECT fact_text FROM facts").fetchall()
    assert n == 0 and not rows, (
        f"credential-shaped block reached facts.db: {rows}"
    )


def test_clean_block_alongside_secret_still_promoted(conn):
    """Blast-radius guard: the filter must drop the line, not the whole section."""
    _promote_mined_to_facts(
        conn, "sess-ga02", {"key_concepts": f"- {_SECRET_BLOCK}\n- {_CLEAN_BLOCK}"})
    texts = [r[0] for r in conn.execute("SELECT fact_text FROM facts").fetchall()]
    assert len(texts) == 1, f"expected exactly the clean block, got {texts}"
    assert "git-lfs" in texts[0]


def test_errors_fixes_section_also_filtered(conn):
    """Both mined sections share the collection loop; cover the second one."""
    _promote_mined_to_facts(conn, "sess-ga02", {"errors_fixes": f"- {_SECRET_BLOCK}"})
    assert not conn.execute("SELECT 1 FROM facts").fetchall()


@pytest.mark.parametrize("pattern", CREDENTIAL_PATTERNS)
def test_every_declared_pattern_is_enforced(conn, pattern):
    """The import existed but was never called; assert the whole tuple is live.

    Parametrized so adding a pattern to CREDENTIAL_PATTERNS automatically extends
    coverage rather than silently landing untested.
    """
    block = (f"While debugging the nightly sync we found {pattern}redactedexamplevalue "
             "in the exported environment of the long-running collector pod.")
    _promote_mined_to_facts(conn, "sess-ga02", {"key_concepts": f"- {block}"})
    rows = conn.execute("SELECT fact_text FROM facts").fetchall()
    assert not rows, f"pattern {pattern!r} not enforced at the promotion boundary"


def _claude_session(dir_: Path, stem: str, payload: str) -> Path:
    """A JSONL whose tool_result body is `payload`.

    parse_claude_events maps this to signal="Observed cluster state/output" (clean for
    every session) and outcome=payload[:2000] — the shape that isolates the leak to the
    outcome field.
    """
    path = dir_ / f"{stem}.jsonl"
    path.write_text(json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": f"toolu_{stem}",
            "is_error": False,
            "content": payload,
        }]},
    }) + "\n")
    return path


def _run_shim(tmp_path, monkeypatch, payload):
    """Drive `retire --claude-shim` over exactly ONE session, and return the rows.

    One session per invocation is load-bearing, not tidiness. Every tool_result event
    gets the hardcoded signal "Observed cluster state/output", so two sessions produce
    identical fact_text; upsert_fact's semantic dedup (cosine > 0.92) then collapses
    the second into the first, and _corroborate does not update `outcome`. A
    dirty-plus-clean fixture therefore passes or fails on mtime ordering rather than on
    the filter — an earlier draft of this test was green with the filter deleted.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_gaius_mod, "PROJECT_DIR", sessions)
    monkeypatch.setattr(_gaius_mod, "STAGING_DIR", tmp_path / "staged")
    _claude_session(sessions, "sess-only", payload)
    cmd_retire(["--claude-shim"])
    return init_db(db_path).execute("SELECT fact_text, outcome FROM facts").fetchall()


# >500 and <5000 chars, else parse_claude_events drops the tool_result outright.
_SHIM_DIRTY = ("kubectl get secret -o yaml showed FORGEJO_TOKEN=gha_ZZZZexampleonlyZZZZ "
               "in the runner environment. " * 8)
_SHIM_CLEAN = ("kubectl get nodes reported all twenty nodes Ready with the expected "
               "kubelet version across every site. " * 8)


def test_claude_shim_filters_credentials_in_outcome(tmp_path, monkeypatch):
    """The subtle half: `signal` is clean, the credential is only in `outcome`.

    parse_claude_events sets signal to a constant for every tool_result, so a guard
    on fact_text alone reads as a fix while the 2000-char raw payload still lands.
    """
    rows = _run_shim(tmp_path, monkeypatch, _SHIM_DIRTY)
    joined = " ".join(f"{r[0]} {r[1] or ''}" for r in rows)
    assert "FORGEJO_TOKEN=" not in joined, (
        "credential leaked through --claude-shim via the outcome field; filtering "
        f"fact_text alone is insufficient there (GA-02). rows={rows}"
    )


def test_claude_shim_keeps_clean_sessions(tmp_path, monkeypatch):
    """Blast-radius guard for the test above: the shim must still import."""
    rows = _run_shim(tmp_path, monkeypatch, _SHIM_CLEAN)
    assert any("twenty nodes Ready" in (r[1] or "") for r in rows), (
        f"clean session was dropped — the filter is over-blocking. rows={rows}"
    )
