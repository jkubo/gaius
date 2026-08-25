"""Tests for gaius.spin — HITL context-spin must never evict a peer handoff.

The founding incident: a saturated session whose same-skill handoff was already
on disk refused `gaius concord handoff` / `gaius baton pass` because a fourth
write would prune existing[3:]. There was no reuse verb. These tests pin that
reuse/replace contract.
"""
import io
import json
import os

import pytest

import gaius.spin as sp


def _seed(dir_path, name, body="## Next Steps\n- [ ] do the thing\n", **fm):
    p = dir_path / name
    skill = fm.get("skill", "mnemos")
    lines = ["---", f"skill: {skill}", "date: 2026-08-15", "time: 00:00 UTC",
             f"severity: {fm.get('severity', 'normal')}"]
    if "hot" in fm:
        lines.append(f"hot: {fm['hot']}")
    if "spin" in fm:
        lines.append(f"spin: {fm['spin']}")
    lines += ["---", "", f"# Session Handoff: {skill} (2026-08-15)", "", body]
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture
def ho(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIUS_HANDOFF_DIR", str(tmp_path))
    return tmp_path


def test_handoff_dir_honors_env(ho):
    assert sp.handoff_dir() == ho


def test_newest_is_reverse_lex_filename(ho):
    _seed(ho, "2026-08-15-002737-mnemos.md")
    _seed(ho, "2026-08-15-051012-mnemos.md")
    _seed(ho, "2026-08-15-023808-mnemos.md")
    assert sp.newest_handoff("mnemos").name == "2026-08-15-051012-mnemos.md"


def test_reuse_empty_stdin_writes_nothing(ho, monkeypatch, capsys):
    a = _seed(ho, "2026-08-15-002737-mnemos.md", body="oldest")
    b = _seed(ho, "2026-08-15-023808-mnemos.md", body="mid")
    c = _seed(ho, "2026-08-15-051012-mnemos.md", body="newest-original")
    before = {p.name: p.read_text() for p in (a, b, c)}
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    rc = sp.cmd_spin(["--skill", "mnemos"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reused 2026-08-15-051012-mnemos.md" in out
    assert "no write, no prune" in out
    assert "would evict 2026-08-15-002737-mnemos.md" in out
    assert {p.name: p.read_text() for p in (a, b, c)} == before
    assert len(list(ho.glob("*-mnemos.md"))) == 3


def test_replace_updates_newest_only(ho, monkeypatch, capsys):
    a = _seed(ho, "2026-08-15-002737-mnemos.md", body="oldest")
    b = _seed(ho, "2026-08-15-023808-mnemos.md", body="mid")
    c = _seed(ho, "2026-08-15-051012-mnemos.md", body="stale-newest", spin="1")
    oldest = a.read_text()
    mid = b.read_text()
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO("## Next Steps\n- [ ] spun state\n"))
    rc = sp.cmd_spin(["--skill", "mnemos"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "replaced 2026-08-15-051012-mnemos.md" in out
    assert len(list(ho.glob("*-mnemos.md"))) == 3
    assert a.read_text() == oldest
    assert b.read_text() == mid
    new = c.read_text()
    assert "spun state" in new
    assert "stale-newest" not in new
    assert "spin: 2" in new
    assert "spun_at:" in new


def test_replace_preserves_hot_true(ho, monkeypatch):
    p = _seed(ho, "2026-08-15-051012-mnemos.md", hot="true")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO("## Next Steps\n- [ ] keep hot\n"))
    assert sp.cmd_spin(["--skill", "mnemos"]) == 0
    fm, _ = sp.parse_frontmatter(p.read_text())
    assert fm["hot"] == "true"


def test_create_when_none_exist(ho, monkeypatch, capsys):
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO("## Completed\n- [x] first\n"))
    rc = sp.cmd_spin(["--skill", "fable"])
    assert rc == 0
    files = list(ho.glob("*-fable.md"))
    assert len(files) == 1
    assert "first" in files[0].read_text()
    assert "created" in capsys.readouterr().out


def test_empty_and_none_refuses(ho, monkeypatch, capsys):
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    rc = sp.cmd_spin(["--skill", "mnemos"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "nothing to reuse" in err
    assert list(ho.glob("*.md")) == []


def test_dry_run_replace_writes_nothing(ho, monkeypatch):
    p = _seed(ho, "2026-08-15-051012-mnemos.md", body="untouched")
    before = p.read_text()
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO("## Next Steps\n- [ ] would write\n"))
    rc = sp.cmd_spin(["--skill", "mnemos", "--dry-run"])
    assert rc == 0
    assert p.read_text() == before
    assert len(list(ho.glob("*.md"))) == 1


def test_missing_skill_errors(ho):
    with pytest.raises(SystemExit):
        sp.cmd_spin([])


def test_skill_from_env(ho, monkeypatch, capsys):
    _seed(ho, "2026-08-15-051012-ops.md", skill="ops")
    monkeypatch.setenv("GAIUS_ACTIVE_SKILL", "ops")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    rc = sp.cmd_spin([])
    assert rc == 0
    assert "skill ops" in capsys.readouterr().out


def test_json_reuse(ho, monkeypatch, capsys):
    _seed(ho, "2026-08-15-002737-mnemos.md")
    _seed(ho, "2026-08-15-023808-mnemos.md")
    _seed(ho, "2026-08-15-051012-mnemos.md")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    rc = sp.cmd_spin(["--skill", "mnemos", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["action"] == "reused"
    assert data["wrote"] is False
    assert data["slots"] == 3
    assert data["would_evict"] == "2026-08-15-002737-mnemos.md"
    assert data["spawned"] is False
    assert "spin ready" in captured.err


def test_card_forbids_second_write_and_child_spawn(ho, monkeypatch, capsys):
    _seed(ho, "2026-08-15-051012-mnemos.md")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    sp.cmd_spin(["--skill", "mnemos"])
    out = capsys.readouterr().out
    assert "gaius concord handoff" in out
    assert "gaius baton pass" in out
    assert "spawn_subagent" in out
    assert "grok     then  /mnemos" in out
    assert "claude   then  /mnemos" in out


def test_other_skill_untouched(ho, monkeypatch):
    peer = _seed(ho, "2026-08-15-010000-jetint.md", skill="jetint", body="jetint-body")
    _seed(ho, "2026-08-15-051012-mnemos.md", body="mnemos-old")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO("## Next Steps\n- [ ] mnemos-new\n"))
    assert sp.cmd_spin(["--skill", "mnemos"]) == 0
    assert "jetint-body" in peer.read_text()
    assert list(ho.glob("*-jetint.md")) == [peer]


def test_strip_duplicate_heading():
    body = "# Session Handoff: mnemos (2026-08-15)\n\n## Next Steps\n- [ ] x\n"
    rendered = sp.render_handoff("mnemos", body, {"severity": "normal"})
    assert rendered.count("# Session Handoff:") == 1


def test_date_only_filename_sorts_ahead_of_timestamp(ho):
    """Reverse-lex: {date}-{skill}.md sorts newer than {date}-{HHMMSS}-{skill}.md.

    Doctrine must not instruct the date-only name (fid-4). This test pins the
    sort so a future newest_handoff change cannot silently invert it.
    """
    _seed(ho, "2026-08-15-053318-mnemos.md")
    _seed(ho, "2026-08-15-mnemos.md")
    assert sp.newest_handoff("mnemos").name == "2026-08-15-mnemos.md"


def test_list_slots(ho, capsys):
    _seed(ho, "2026-08-15-002737-mnemos.md")
    _seed(ho, "2026-08-15-051012-mnemos.md")
    rc = sp.cmd_spin(["--skill", "mnemos", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2/3 slots" in out
    assert "2026-08-15-051012-mnemos.md newest" in out


def test_transcript_authors_then_replaces_without_prune(ho, monkeypatch, capsys):
    a = _seed(ho, "2026-08-15-002737-mnemos.md", body="oldest")
    b = _seed(ho, "2026-08-15-023808-mnemos.md", body="mid")
    c = _seed(ho, "2026-08-15-051012-mnemos.md", body="stale")
    tp = ho / "t.jsonl"
    tp.write_text("{}\n")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)

    def fake_author(skill, transcript, model, timeout, tail_bytes):
        assert skill == "mnemos"
        assert transcript == str(tp)
        return "## HANDOFF STATE\nfrom-transcript\n"

    monkeypatch.setattr("gaius.baton._author_body", fake_author)
    rc = sp.cmd_spin(["--skill", "mnemos", "--transcript", str(tp), "--session", "sid-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "replaced 2026-08-15-051012-mnemos.md" in out
    assert len(list(ho.glob("*-mnemos.md"))) == 3
    assert "oldest" in a.read_text()
    assert "mid" in b.read_text()
    new = c.read_text()
    assert "from-transcript" in new
    assert "stale" not in new
    assert "session: sid-1" in new
    assert "hot:" not in new.split("---")[1]


def test_transcript_create_when_none(ho, monkeypatch, capsys):
    tp = ho / "t.jsonl"
    tp.write_text("{}\n")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("gaius.baton._author_body",
                        lambda *a, **k: "## HANDOFF STATE\nfirst-from-transcript\n")
    rc = sp.cmd_spin(["--skill", "fable", "--transcript", str(tp)])
    assert rc == 0
    files = list(ho.glob("*-fable.md"))
    assert len(files) == 1
    assert "first-from-transcript" in files[0].read_text()
    assert "created" in capsys.readouterr().out


def test_transcript_author_fail_reuses_existing(ho, monkeypatch):
    p = _seed(ho, "2026-08-15-051012-mnemos.md", body="keep-me")
    before = p.read_text()
    tp = ho / "t.jsonl"
    tp.write_text("{}\n")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("gaius.baton._author_body", lambda *a, **k: "")
    assert sp.cmd_spin(["--skill", "mnemos", "--transcript", str(tp)]) == 0
    assert p.read_text() == before
    assert len(list(ho.glob("*-mnemos.md"))) == 1


def test_transcript_author_fail_creates_stub_when_none(ho, monkeypatch):
    tp = ho / "t.jsonl"
    tp.write_text("{}\n")
    monkeypatch.setattr(sp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sp.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("gaius.baton._author_body", lambda *a, **k: "")
    assert sp.cmd_spin(["--skill", "ops", "--transcript", str(tp)]) == 0
    files = list(ho.glob("*-ops.md"))
    assert len(files) == 1
    assert "summarizer unavailable" in files[0].read_text()
    assert "hot:" not in files[0].read_text().split("---")[1]

