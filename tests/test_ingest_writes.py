"""GA-01 — `gaius ansible` and `gaius aliases` must actually WRITE facts.

All five ``upsert_fact`` call sites in ``gaius/ingest.py`` omitted the required
positional ``session_uuid`` (slot 6 of the signature), so every live write raised
TypeError. Three sites propagated the error; two — the playbook-summary leg
(``ingest.py`` §cmd_ansible step 3) and the ``gen_corpus.py`` leg (§cmd_aliases) —
sit inside ``except Exception: pass``, so the commands printed "Extracted N facts",
exited 0, and wrote nothing. ``--dry-run`` returns before the call, which is why a
manual smoke test looked healthy.

``tests/test_peer_promote.py::test_all_upsert_fact_callers_bind`` guards the call
SHAPE. These guard the OUTCOME — a row in facts.db. Both are needed: the static
guard cannot see an except-pass swallowing the error, and testing one leg
end-to-end would leave the other four uncovered.

Run:
    pytest tests/test_ingest_writes.py -v
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
from gaius._core import init_db  # noqa: E402
from gaius.ingest import cmd_aliases, cmd_ansible  # noqa: E402

yaml = pytest.importorskip("yaml", reason="cmd_ansible hard-requires PyYAML")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Isolated facts.db. cmd_* call init_db() with no args, which resolves
    _core.DB_PATH at call time — so patching the hub is what redirects them."""
    path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", path)
    init_db(path).close()
    return path


def _facts(db, agent=None):
    conn = init_db(db)
    sql = "SELECT fact_text, agents, sessions FROM facts"
    rows = conn.execute(sql).fetchall()
    conn.close()
    if agent is not None:
        rows = [r for r in rows if agent in (r[1] or "")]
    return rows


def _ansible_tree(root: Path, with_playbook=True) -> Path:
    (root / "inventory").mkdir(parents=True, exist_ok=True)
    (root / "inventory" / "hosts.yml").write_text(
        "all:\n"
        "  hosts:\n"
        "    node01:\n"
        "      role: primary control plane for the lab rack\n"
    )
    if with_playbook:
        (root / "playbooks").mkdir(parents=True, exist_ok=True)
        (root / "playbooks" / "03-storage-prep.yml").write_text(
            "- name: Provision block storage on every worker\n"
            "  hosts: all\n"
        )
    return root


def test_cmd_ansible_writes_inventory_facts(db, tmp_path):
    """The loud leg: a TypeError here used to abort the whole command."""
    root = _ansible_tree(tmp_path / "ansible", with_playbook=False)
    cmd_ansible(["--path", str(root)])
    rows = _facts(db, agent="gaius-ansible")
    assert rows, "cmd_ansible wrote zero facts — GA-01 has regressed"


def test_cmd_ansible_writes_playbook_facts(db, tmp_path):
    """The SILENT leg: this call sits inside `except Exception: pass`.

    Without an assertion on stored rows this failure mode is invisible — the
    command still prints a count and exits 0.
    """
    root = _ansible_tree(tmp_path / "ansible")
    cmd_ansible(["--path", str(root)])
    rows = _facts(db, agent="gaius-ansible")
    assert any("Playbook 03-storage-prep.yml" in r[0] for r in rows), (
        "playbook-summary leg wrote nothing; its exception was swallowed by "
        "`except Exception: pass` (GA-01)"
    )


def test_cmd_aliases_writes_alias_and_function_facts(db, tmp_path):
    """Covers both loud legs of cmd_aliases: alias lines and function defs."""
    aliases = tmp_path / ".aliases"
    aliases.write_text(
        "alias kx='kubectl --context lab get pods -A'\n"
        "drain_node() {\n"
        "  echo draining\n"
        "}\n"
    )
    cmd_aliases(["--path", str(aliases)])
    texts = [r[0] for r in _facts(db, agent="gaius-aliases")]
    assert any("Alias 'kx'" in t for t in texts), "alias leg wrote nothing (GA-01)"
    assert any("Function 'drain_node'" in t for t in texts), (
        "function leg wrote nothing (GA-01)"
    )


def test_cmd_aliases_writes_gen_corpus_facts(db, tmp_path):
    """The second SILENT leg: gen_corpus.py mining, inside `except Exception: pass`."""
    aliases = tmp_path / ".aliases"
    aliases.write_text("alias kx='kubectl get pods'\n")
    (tmp_path / "gen_corpus.py").write_text(
        '# ALIASES\n'
        'qa("What does kx do?", "It lists pods across all namespaces.")\n'
        '# PLAYBOOK\n'
    )
    cmd_aliases(["--path", str(aliases)])
    texts = [r[0] for r in _facts(db, agent="gaius-aliases")]
    assert any("What does kx do?" in t for t in texts), (
        "gen_corpus leg wrote nothing; its exception was swallowed (GA-01)"
    )


def test_dry_run_writes_nothing(db, tmp_path):
    """Blast-radius guard: --dry-run must stay a no-op, not start writing."""
    root = _ansible_tree(tmp_path / "ansible")
    cmd_ansible(["--path", str(root), "--dry-run"])
    assert not _facts(db), "--dry-run wrote to facts.db"


def test_session_uuid_is_constant_across_runs(db, tmp_path):
    """Pins the session_uuid design decision, not just its presence.

    upsert_fact's _corroborate appends any unseen session_uuid to the row's
    `sessions` array. A per-run value (timestamp/uuid4) would therefore grow that
    array by one entry on every nightly run, on every corroborated fact, forever.
    The ingest paths mine a filesystem tree rather than an agent session, so they
    use a constant — matching reconcile.py's `session_uuid="reconcile"`.
    """
    root = _ansible_tree(tmp_path / "ansible")
    cmd_ansible(["--path", str(root)])
    cmd_ansible(["--path", str(root)])
    cmd_ansible(["--path", str(root)])

    sessions = {r[2] for r in _facts(db, agent="gaius-ansible")}
    assert sessions == {json.dumps(["ansible"])}, (
        f"sessions array drifted across identical runs: {sessions}"
    )
