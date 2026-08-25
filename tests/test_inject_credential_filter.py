"""Credential-bearing facts must never reach a model provider.

The corpus is shared and is known to hold live, deliberately un-revoked
credentials. `gaius inject` runs on every session start and every prompt, and
the hooks hand its stdout to Claude/Grok as context; `gaius_search` returns
`fact_text[:300]` to the MCP client. Neither consulted `has_credential`, which
already existed.

These tests pin the exclusion at CANDIDATE SELECTION. Filtering at format time
would be too late by construction: at that point the fact has already been
chosen, and the only thing left to decide is how much of the secret to print.

The credential values below are synthetic and match only the vendor SHAPE.
"""
import os
import sys
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import init_db
from gaius.extract import has_credential
from gaius.landscape import cmd_inject

SENTINEL = "CREDLEAK-SENTINEL-4b2e"
# Synthetic, shape-only. Never a real key.
FAKE_XAI = "xai-" + "A" * 24
FAKE_ANTHROPIC = "sk-ant-" + "B" * 24
CLEAN_MARKER = "CLEANFACT-SENTINEL-9d17"


def _seed(conn, key, text, domain="ops"):
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, fact_type, score, provenance, "
        "first_seen, last_seen) "
        "VALUES (?, ?, ?, 'auto', 0.90, 'inferred', 3, 'operational', 5.0, 'test', "
        "datetime('now'), datetime('now'))",
        (key, domain, text),
    )
    conn.commit()


# ── the prefix list itself ───────────────────────────────────────────────────

def test_xai_prefix_is_recognised_as_a_credential():
    """The estate's OWN provider was missing from the shape list."""
    assert has_credential(f"the key is {FAKE_XAI} do not share")


def test_anthropic_prefix_still_recognised():
    """sk- already covered sk-ant-; pin it so a future edit cannot narrow it."""
    assert has_credential(f"the key is {FAKE_ANTHROPIC}")


def test_prose_about_a_leak_is_not_itself_a_credential():
    """The finding is the valuable part and carries no secret — must NOT match."""
    assert not has_credential(
        "the xai token was visible in kubectl describe output, rotate it"
    )


# ── gaius inject — the every-prompt egress path ──────────────────────────────

def test_inject_excludes_credential_facts_and_keeps_clean_ones(tmp_path, monkeypatch, capsys):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _seed(conn, "leaky", f"{SENTINEL} deploy token {FAKE_XAI} for the cluster")
    _seed(conn, "clean", f"{CLEAN_MARKER} flannel MTU is 1050 on this fleet")
    conn.close()

    # --task so the clean fact clears the relevance threshold; without it the
    # control test proves nothing (an empty injection would "pass" the exclusion
    # assertions for the wrong reason).
    cmd_inject(["--budget", "8000", "--no-semantic", "--no-always-skills",
                "--task", "flannel MTU cluster deploy token"])
    out = capsys.readouterr()

    assert SENTINEL not in out.out, "credential-bearing fact reached inject stdout"
    assert FAKE_XAI not in out.out, "the credential itself reached inject stdout"
    assert CLEAN_MARKER in out.out, "the filter also dropped an ordinary fact"


def test_inject_reports_the_exclusion_count_on_stderr(tmp_path, monkeypatch, capsys):
    """Silent filtering reads as a shorter injection; the count must be visible.

    stderr specifically — stdout is what the hooks feed the model.
    """
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _seed(conn, "leaky1", f"token {FAKE_XAI} one")
    _seed(conn, "leaky2", f"token {FAKE_ANTHROPIC} two")
    _seed(conn, "clean", f"{CLEAN_MARKER} ordinary operational fact")
    conn.close()

    cmd_inject(["--budget", "8000", "--no-semantic", "--no-always-skills"])
    err = capsys.readouterr().err

    assert "excluded 2 fact(s) carrying credential material" in err
