"""Gap 39 — auto-mined facts must NOT be tagged 'structural' (the no-decay class).

`volatility_recency()` returns exactly 1.0 for fact_type='structural', so anything
mined under that tag never decays. Mining ran with a hardcoded fact_type="structural",
which made the overwhelming majority of a mature corpus permanently decay-proof —
including pure point-in-time state snapshots, which then outlived their own truth.

An auto-mined block is arbitrary session prose. Nothing at the mining boundary knows
whether it is design-level, so it must fall to the schema default 'operational' and
decay at the normal rate. These tests lock that; without them the one-word regression
is invisible (the rest of the suite passes either way).
"""
import os
import sys
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import init_db, _promote_mined_to_facts, volatility_recency


# Blocks must exceed the len() < 80 promotion filter and survive _is_noise().
_STATE_SNAPSHOT = (
    "The signature pipeline has recovered: signatures went from 1 on Monday to 572 "
    "on Tuesday after the api pods restarted and picked up the new image."
)
_DESIGNISH = (
    "The control plane is a thin deterministic service that keeps every model call "
    "off the request path, so routing stays reproducible under partial failure."
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)
    return init_db(db_path)


def _mine(conn, **sections):
    n = _promote_mined_to_facts(conn, "sess-gap39", sections)
    rows = conn.execute("SELECT fact_text, fact_type FROM facts").fetchall()
    return n, rows


def test_mined_facts_are_not_structural(conn):
    """The regression itself: mining must never emit the no-decay class."""
    n, rows = _mine(conn, key_concepts=f"- {_STATE_SNAPSHOT}\n- {_DESIGNISH}")
    assert n == 2, f"expected both blocks promoted, got {n}"
    assert rows, "no facts were written"
    assert [r[1] for r in rows] == ["operational", "operational"]
    assert "structural" not in {r[1] for r in rows}


def test_mined_facts_actually_decay(conn):
    """End-to-end intent, not just the column value: a mined fact must lose recency."""
    _mine(conn, errors_fixes=f"- {_STATE_SNAPSHOT}")
    ft = conn.execute("SELECT fact_type FROM facts").fetchone()[0]
    recency = volatility_recency("auto-mined", ft, age_days=90.0, rate=0.02)
    assert recency < 1.0, (
        f"mined fact_type={ft!r} still exempt from decay — Gap 39 has regressed"
    )


def test_errors_fixes_section_also_operational(conn):
    """Both mined sections go through the same call site; cover the second one."""
    _, rows = _mine(conn, errors_fixes=f"- {_DESIGNISH}")
    assert [r[1] for r in rows] == ["operational"]


def test_structural_remains_exempt_for_deliberate_ratings(conn):
    """Guard the fix's blast radius: the 'structural' axis itself must still work.

    Gap 39 is about mining over-assigning the tag, NOT about removing it. A caller
    that deliberately rates a fact structural (gaius_fact_add) must stay decay-proof.
    """
    assert volatility_recency("automated", "structural", 365.0, 0.02) == 1.0
