"""`gaius commands` wired-copy semantics — symlink safety + `paths:`-stripped frontmatter.

Two coupled invariants (both learned the hard way, 2026-07-25):
  1. A wired SKILL.md carrying `paths:` is demoted to file-glob-conditional by Claude
     Code and vanishes from the always-available Skill list — so the stub must drop it.
  2. The wired copy is sometimes a SYMLINK back to the source skill file. Writing
     through it would rewrite the source (frontmatter stripped) — silent data loss.
"""
from gaius import _core
from gaius._core import _stub_content, _strip_frontmatter_key, cmd_commands

SKILL_SRC = """---
name: demo
description: Demo skill — picker text that must survive.
trigger: "demo work"
paths:
  - "manifests/demo/*.yaml"
  - "playbooks/*.yml"
gate: reference
---

# Session Mode: Demo

Body line.
"""


def _write_skill(skills_dir, name, text):
    skills_dir.mkdir(parents=True, exist_ok=True)
    p = skills_dir / f"{name}.md"
    p.write_text(text)
    return p


def test_strip_frontmatter_key_drops_key_and_its_list():
    out = _strip_frontmatter_key(SKILL_SRC, "paths")
    assert "paths:" not in out
    assert "manifests/demo/*.yaml" not in out
    # sibling keys and body survive, in order
    assert "description: Demo skill" in out
    assert "gate: reference" in out
    assert out.count("---") == 2
    assert out.endswith("Body line.\n")


def test_strip_frontmatter_key_noop_without_frontmatter():
    text = "# No frontmatter\n\npaths: not-in-frontmatter\n"
    assert _strip_frontmatter_key(text, "paths") == text


def test_stub_content_keeps_description_drops_paths():
    stub = _stub_content({"full_text": SKILL_SRC})
    assert "description: Demo skill" in stub   # picker text preserved
    assert "paths:" not in stub                # else CC demotes to conditional
    assert stub.endswith("\n")


def test_cmd_commands_replaces_symlink_without_clobbering_source(tmp_path, monkeypatch, capsys):
    skills_dir = tmp_path / "skills"
    src = _write_skill(skills_dir, "demo", SKILL_SRC)
    wired_dir = tmp_path / "claude" / "skills"
    stub = wired_dir / "demo" / "SKILL.md"
    stub.parent.mkdir(parents=True)
    stub.symlink_to(src)                       # the drifted wiring

    monkeypatch.setattr(_core, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(_core, "CLAUDE_SKILLS_DIR", wired_dir)
    monkeypatch.setattr(_core, "CLAUDE_COMMANDS_DIR", tmp_path / "claude" / "commands")

    cmd_commands(["--all"])

    assert src.read_text() == SKILL_SRC        # source untouched — the whole point
    assert not stub.is_symlink()               # link replaced by a regular file
    assert "paths:" not in stub.read_text()
    assert "description: Demo skill" in stub.read_text()
    assert "relinked" in capsys.readouterr().out


def test_cmd_commands_dry_run_leaves_symlink_intact(tmp_path, monkeypatch, capsys):
    skills_dir = tmp_path / "skills"
    src = _write_skill(skills_dir, "demo", SKILL_SRC)
    wired_dir = tmp_path / "claude" / "skills"
    stub = wired_dir / "demo" / "SKILL.md"
    stub.parent.mkdir(parents=True)
    stub.symlink_to(src)

    monkeypatch.setattr(_core, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(_core, "CLAUDE_SKILLS_DIR", wired_dir)
    monkeypatch.setattr(_core, "CLAUDE_COMMANDS_DIR", tmp_path / "claude" / "commands")

    cmd_commands(["--all", "--dry-run"])

    assert stub.is_symlink()
    assert src.read_text() == SKILL_SRC
    assert "relink" in capsys.readouterr().out


def test_cmd_commands_is_idempotent_on_regular_stub(tmp_path, monkeypatch, capsys):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "demo", SKILL_SRC)
    wired_dir = tmp_path / "claude" / "skills"
    monkeypatch.setattr(_core, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(_core, "CLAUDE_SKILLS_DIR", wired_dir)
    monkeypatch.setattr(_core, "CLAUDE_COMMANDS_DIR", tmp_path / "claude" / "commands")

    cmd_commands(["--all"])                    # create
    capsys.readouterr()
    cmd_commands(["--all"])                    # second run must not rewrite
    assert "1 unchanged" in capsys.readouterr().out


def test_cmd_commands_skips_hidden_skills(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "shown", SKILL_SRC)
    _write_skill(skills_dir, "quiet", SKILL_SRC.replace("name: demo", "name: quiet\nhidden: true"))
    wired_dir = tmp_path / "claude" / "skills"
    monkeypatch.setattr(_core, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(_core, "CLAUDE_SKILLS_DIR", wired_dir)
    monkeypatch.setattr(_core, "CLAUDE_COMMANDS_DIR", tmp_path / "claude" / "commands")

    cmd_commands(["--all"])

    assert (wired_dir / "shown" / "SKILL.md").exists()
    assert not (wired_dir / "quiet").exists()
