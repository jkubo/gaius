"""Tests for gaius.baton — the `pass` verb (2026-08-11) and the invariants it must not
weaken. Written with the pass feature: baton previously shipped with no tests at all.

Covers the cross-file contract: hot marker in FRONTMATTER (never filename), consume-once
via landscape._hot_handoff_take, spawn-vs-hot delivery exclusivity, machine-caller argv
compatibility (gaius-baton-watch / marathon pass flags only, no verb), and the
deterministic destructive_pending backstop staying intact.
"""
import os

import gaius.baton as bt
import gaius.landscape as ls


# ── frontmatter patchers ─────────────────────────────────────────────────────────────

FM_DOC = "---\nskill: demo\ndate: 2026-08-11\nseverity: normal\n---\n\n## HANDOFF STATE\nbody\n"


def test_patch_hot_frontmatter_inserts_into_first_block(tmp_path):
    p = tmp_path / "h.md"
    p.write_text(FM_DOC)
    bt._patch_hot_frontmatter(str(p))
    txt = p.read_text()
    fm = txt.split("---")[1]
    assert "hot: true" in fm
    # body untouched
    assert txt.endswith("## HANDOFF STATE\nbody\n")


def test_patch_hot_frontmatter_idempotent(tmp_path):
    p = tmp_path / "h.md"
    p.write_text(FM_DOC)
    bt._patch_hot_frontmatter(str(p))
    once = p.read_text()
    bt._patch_hot_frontmatter(str(p))
    assert p.read_text() == once


def test_patch_hot_frontmatter_noop_without_frontmatter(tmp_path):
    p = tmp_path / "h.md"
    p.write_text("## HANDOFF STATE\nno frontmatter\n")
    bt._patch_hot_frontmatter(str(p))
    assert "hot:" not in p.read_text()


def test_destructive_backstop_unchanged():
    # regression guard: the deterministic token scan must survive the pass feature
    assert bt._is_destructive("Next action: git push origin main")
    assert not bt._is_destructive("Next action: read the docs")


# ── skill / transcript resolution ────────────────────────────────────────────────────

def test_resolve_skill_last_attribution_wins(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text('{"attributionSkill":"ops"}\n{"attributionSkill":"demo"}\n')
    assert bt._resolve_skill(str(t)) == "demo"


def test_resolve_skill_command_name_fallback(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text('x <command-name>/malint</command-name> y\n')
    assert bt._resolve_skill(str(t)) == "malint"


def test_resolve_skill_empty_when_unresolvable(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text("nothing here\n")
    assert bt._resolve_skill(str(t)) == ""


# ── argv contract ────────────────────────────────────────────────────────────────────

def test_cmd_baton_is_tombstone(capsys):
    # user-facing verb is dead; PreCompact / watchers must call gaius spin --transcript
    rc = bt.cmd_baton(["--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "retired" in err.lower()
    assert "gaius spin" in err
    assert "not an alias" in err


def test_cmd_baton_pass_is_the_same_tombstone(capsys):
    rc = bt.cmd_baton(["pass", "--skill", "demo", "--spawn"])
    assert rc == 2
    assert "gaius spin" in capsys.readouterr().err


# ── landscape consume-once pickup ────────────────────────────────────────────────────

def _hot_handoff(tmp_path, name="2026-08-11-070000-demo.md", hot=True, body="the baton"):
    p = tmp_path / name
    fm = "---\nskill: demo\nseverity: normal\n" + ("hot: true\n" if hot else "") + "---\n"
    p.write_text(fm + "\n## HANDOFF STATE\n" + body + "\n")
    return p


def test_hot_take_returns_and_consumes(tmp_path):
    import re as _re
    p = _hot_handoff(tmp_path)
    skill, body, commit = ls._hot_handoff_take(tmp_path, sid="abcd1234efgh")
    assert skill == "demo"
    assert "the baton" in body
    # B3: take does NOT consume; the returned commit closure does, after emit
    assert "hot: true" in p.read_text().split("---")[1]
    commit()
    txt = p.read_text()
    fm = txt.split("---")[1]
    assert "hot: true" not in fm
    assert "hot: consumed abcd1234" in fm
    # the stamp must keep its own line — a \s*$ pattern once ate the newline and glued
    # the closing --- fence onto the stamp (caught live 2026-08-11)
    assert _re.search(r"(?m)^hot: consumed abcd1234 [0-9T:\-]+\n", txt)
    assert "\n---\n" in txt[3:]  # closing fence intact on its own line
    # consume-once: second scan finds nothing
    assert ls._hot_handoff_take(tmp_path, sid="zzzz") == (None, None, None)


def test_hot_take_ignores_unstamped_and_stale(tmp_path):
    _hot_handoff(tmp_path, name="a-demo.md", hot=False)
    stale = _hot_handoff(tmp_path, name="b-demo.md", hot=True)
    old = stale.stat().st_mtime - (ls._HOT_TTL_H * 3600 + 60)
    os.utime(stale, (old, old))
    assert ls._hot_handoff_take(tmp_path) == (None, None, None)
    # stale marker NOT consumed (TTL already fences it)
    assert "hot: true" in stale.read_text().split("---")[1]


def test_hot_take_skips_oversized_without_consuming(tmp_path):
    p = _hot_handoff(tmp_path, body="x " * 20000)
    assert ls._hot_handoff_take(tmp_path, max_tokens=3000) == (None, None, None)
    assert "hot: true" in p.read_text().split("---")[1]


def test_hot_take_skips_empty_body_without_consuming(tmp_path):
    # a truncated/corrupt hot file (frontmatter only, empty body) must be skipped like oversized —
    # NOT returned with a live commit, else _emit_handoffs would consume it with nothing to emit
    # (the empty-body consume-without-emit hole the adversarial review caught, 2026-08-12)
    p = tmp_path / "e-demo.md"
    p.write_text("---\nskill: demo\nhot: true\n---\n")
    assert ls._hot_handoff_take(tmp_path, sid="x") == (None, None, None)
    assert "hot: true" in p.read_text().split("---")[1]  # left un-consumed, TTL-bounded


# The marathon reentrancy rail test (baton-pass in marathon's forbid-list) moved to
# gaius-praetorium tests/test_marathon_baton_rail.py with marathon in the Phase B split.


def test_cmd_baton_never_persists(monkeypatch, capsys):
    persisted = []
    monkeypatch.setattr(bt, "_persist", lambda *a, **k: persisted.append(a) or "")
    assert bt.cmd_baton(["pass", "--skill", "demo"]) == 2
    assert not persisted
    assert "retired" in capsys.readouterr().err.lower()


def test_hot_take_surfaces_destructive_pending(tmp_path):
    p = tmp_path / "d-demo.md"
    p.write_text("---\nskill: demo\ndestructive_pending: true\nhot: true\n---\n\nbody here\n")
    skill, body, _ = ls._hot_handoff_take(tmp_path)
    assert skill == "demo"
    assert body.startswith("⛔ destructive_pending: true")
    assert "body here" in body


def test_hot_take_defers_consume_until_commit(tmp_path):
    # B3 fail-safe: take does NOT flip the marker — only the returned commit closure
    # does, after the caller emits. A caller that early-returns before emitting leaves
    # the baton hot to re-deliver, instead of consume-then-drop (permanent loss).
    p = _hot_handoff(tmp_path)
    skill, body, commit = ls._hot_handoff_take(tmp_path, sid="deferme1")
    assert skill == "demo" and "the baton" in body
    assert "hot: true" in p.read_text().split("---")[1]              # NOT consumed by take
    commit()
    assert "hot: consumed deferme" in p.read_text().split("---")[1]  # consumed by commit
    assert ls._hot_handoff_take(tmp_path, sid="zzzz") == (None, None, None)  # gone after


def test_cmd_inject_delivers_and_consumes_hot_baton_on_empty_corpus(tmp_path):
    # INTEGRATION (B3): drive the whole cmd_inject path, not just the closure. A temp HOME gives
    # an isolated handoff dir AND an empty corpus, forcing the `No corpus entries available` early
    # return — the exact site the pre-fix code consumed-then-dropped the baton. Guards against a
    # future edit removing _emit_handoffs() from an early return (would silently stop delivering
    # on empty-corpus turns without reddening CI; re-delivery next turn masks it as "fail-safe").
    import subprocess
    import sys
    # Use the PUBLIC override (GAIUS_HANDOFF_DIR), not a deployment's directory layout.
    # This previously rebuilt the maintainer's own tree in path segments to match a
    # deployment pin that the publish pass strips, so it passed in dev and delivered
    # nothing in the mirror, where _HANDOFF_DIR is the shipped default. Segment-built paths
    # are also invisible to the leak scan, which needs a literal slash-joined string.
    hd = tmp_path / "handoffs"
    hd.mkdir(parents=True)
    marker = "INTEG_BATON_BODY_ZQ7"
    baton = hd / "2026-08-12-000000-demo.md"
    baton.write_text("---\nskill: demo\nseverity: normal\nhot: true\n---\n\n## HANDOFF STATE\n" + marker + "\n")
    # Drop PYTEST_CURRENT_TEST so init_db's live-DB guard doesn't trip: with HOME=tmp_path the
    # "home" DB IS the throwaway tmp_path/.gaius/facts.db (empty) — a real, isolated invocation.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["HOME"] = str(tmp_path)
    env["GAIUS_HANDOFF_DIR"] = str(hd)
    r = subprocess.run(
        [sys.executable, "-m", "gaius", "inject", "--budget", "3000", "--handoff-hot", "--format", "plain"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert marker in r.stdout, f"baton not delivered on empty corpus; stdout={r.stdout!r} stderr={r.stderr!r}"
    fm = baton.read_text().split("---")[1]
    assert "hot: consumed" in fm and "hot: true" not in fm  # commit fired on the early-return path


# ── scope + fail-loud (concurrent-collision fix, 2026-08-12) ──────────────────────────

def test_patch_scope_frontmatter_records_session_and_cwd(tmp_path):
    p = tmp_path / "h.md"
    p.write_text(FM_DOC)
    bt._patch_scope_frontmatter(p, "sid-xyz", "/work/repo-a")
    fm = p.read_text().split("---")[1]
    assert "session: sid-xyz" in fm
    assert "cwd: /work/repo-a" in fm
    # idempotent — a second call adds nothing
    before = p.read_text()
    bt._patch_scope_frontmatter(p, "sid-xyz", "/work/repo-a")
    assert p.read_text() == before


def _scoped_handoff(tmp_path, name, session="", cwd="", hot=True, body="the baton"):
    p = tmp_path / name
    fm = "---\nskill: demo\nseverity: normal\n"
    if session:
        fm += f"session: {session}\n"
    if cwd:
        fm += f"cwd: {cwd}\n"
    if hot:
        fm += "hot: true\n"
    fm += "---\n"
    p.write_text(fm + "\n## HANDOFF STATE\n" + body + "\n")
    return p


def test_hot_take_excludes_live_predecessor(tmp_path):
    # a baton whose predecessor session is still running is NOT ours to take — this is the
    # concurrent-collision the fix exists for (a live sibling's stub used to clobber the real one)
    p = _scoped_handoff(tmp_path, "a-demo.md", session="LIVESID")
    assert ls._hot_handoff_take(tmp_path, live_sids={"LIVESID"}) == (None, None, None)
    assert "hot: true" in p.read_text().split("---")[1]  # not consumed


def test_hot_take_consumes_dead_predecessor(tmp_path):
    p = _scoped_handoff(tmp_path, "a-demo.md", session="DEADSID")
    skill, body, commit = ls._hot_handoff_take(tmp_path, sid="new1", live_sids={"OTHER"})
    assert skill == "demo" and "the baton" in body
    assert "hot: true" in p.read_text().split("---")[1]  # B3: not consumed until commit
    commit()
    assert "hot: consumed" in p.read_text().split("---")[1]


def test_hot_take_cwd_mismatch_excluded(tmp_path):
    _scoped_handoff(tmp_path, "a-demo.md", cwd="/work/repo-b")
    assert ls._hot_handoff_take(tmp_path, cwd="/work/repo-a") == (None, None, None)


def test_hot_take_cwd_match_consumes(tmp_path):
    p = _scoped_handoff(tmp_path, "a-demo.md", cwd="/work/repo-a")
    skill, _, commit = ls._hot_handoff_take(tmp_path, cwd="/work/repo-a")
    assert skill == "demo"
    commit()
    assert "hot: consumed" in p.read_text().split("---")[1]


def test_hot_take_ambiguous_two_eligible_consumes_none(tmp_path):
    a = _scoped_handoff(tmp_path, "a-demo.md", session="DEAD1", body="baton A")
    b = _scoped_handoff(tmp_path, "b-demo.md", session="DEAD2", body="baton B")
    skill, menu, commit = ls._hot_handoff_take(tmp_path, live_sids={"OTHER"})
    assert skill == "__ambiguous__"
    assert commit is None  # ambiguous → nothing to consume
    assert "2 hot batons pending" in menu
    assert "a-demo.md" in menu and "b-demo.md" in menu
    # fail-loud: neither consumed, both still hot for a manual adopt
    assert "hot: true" in a.read_text().split("---")[1]
    assert "hot: true" in b.read_text().split("---")[1]


def test_hot_take_backward_compat_legacy_no_scope_fields(tmp_path):
    # a pre-scope handoff (no session/cwd) is lenient — still consumed when it is the only hot one
    _hot_handoff(tmp_path)
    skill, body, _ = ls._hot_handoff_take(tmp_path, sid="new", cwd="/anything", live_sids={"x"})
    assert skill == "demo" and "the baton" in body


def test_hot_take_two_legacy_batons_menu_not_newest(tmp_path):
    # THE regression this fix guards: two concurrent hot batons, neither carrying session/cwd
    # (pre-scope format). Lenient-on-missing keeps both eligible → the fix must MENU, never the
    # old silent newest-wins that let a sibling's stub clobber the real baton (live 2026-08-12).
    a = _hot_handoff(tmp_path, name="a-demo.md", body="baton A")
    b = _hot_handoff(tmp_path, name="b-demo.md", body="baton B")
    skill, menu, commit = ls._hot_handoff_take(tmp_path, sid="succ", cwd="/x", live_sids={"y"})
    assert skill == "__ambiguous__"
    assert commit is None  # fail-loud: consume nothing
    assert "a-demo.md" in menu and "b-demo.md" in menu
    assert "hot: true" in a.read_text().split("---")[1]  # neither consumed
    assert "hot: true" in b.read_text().split("---")[1]


def test_hot_take_preserves_body_horizontal_rule(tmp_path):
    # a `---` markdown rule in the body must survive consume-rewrite — the stamp lands in the
    # frontmatter, the closing fence stays on its own line, the body rule is not glued/duplicated
    p = tmp_path / "h-demo.md"
    p.write_text("---\nskill: demo\nhot: true\n---\n\nbefore\n\n---\n\nafter\n")
    skill, body, commit = ls._hot_handoff_take(tmp_path, sid="abcd")
    assert skill == "demo"
    assert "before" in body and "after" in body
    commit()  # B3: consume-rewrite happens on commit
    txt = p.read_text()
    assert "hot: consumed abcd" in txt.split("---")[1]  # stamp in frontmatter block
    assert "\n---\n\nafter" in txt                       # body rule intact
