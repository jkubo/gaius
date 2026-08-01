"""mnemosyne test suite — memory file health monitor."""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec

import pytest

_REPO = Path(__file__).parent.parent


def _load_script(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(name, loader)
    mod = module_from_spec(spec)
    loader.exec_module(mod)
    return mod


mn = _load_script("mnemosyne", _REPO / "mnemosyne")


# ─────────────────────────────────────────────────────────────────────────────
# color_status
# ─────────────────────────────────────────────────────────────────────────────

class TestColorStatus:
    def test_green_below_warn(self):
        assert "GREEN" in mn.color_status(50, 180, 200)

    def test_yellow_at_warn_boundary(self):
        assert "YELLOW" in mn.color_status(180, 180, 200)

    def test_yellow_between_warn_and_error(self):
        assert "YELLOW" in mn.color_status(195, 180, 200)

    def test_red_at_error_boundary(self):
        assert "RED" in mn.color_status(200, 180, 200)

    def test_red_above_error(self):
        assert "RED" in mn.color_status(999, 180, 200)

    def test_green_zero_lines(self):
        assert "GREEN" in mn.color_status(0, 50, 100)


# ─────────────────────────────────────────────────────────────────────────────
# count_lines
# ─────────────────────────────────────────────────────────────────────────────

class TestCountLines:
    def test_correct_line_count(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("line1\nline2\nline3\n")
        assert mn.count_lines(f) == 3

    def test_single_line_no_newline(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("one line")
        assert mn.count_lines(f) == 1

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("")
        assert mn.count_lines(f) == 0

    def test_nonexistent_returns_minus_one(self, tmp_path):
        assert mn.count_lines(tmp_path / "ghost.md") == -1


# ─────────────────────────────────────────────────────────────────────────────
# audit keyword detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAudit:
    @pytest.fixture(autouse=True)
    def _default_keywords(self, monkeypatch):
        # Isolate from the operator's ~/.gaius/config.yaml audit_keywords (which
        # drift over time). _load_domain_keywords() honors GAIUS_CONFIG=/dev/null
        # → built-in defaults; DOMAIN_KEYWORDS is bound at import, so reload it.
        monkeypatch.setenv("GAIUS_CONFIG", "/dev/null")
        monkeypatch.setattr(mn, "DOMAIN_KEYWORDS", mn._load_domain_keywords())

    def _run_audit(self, memory_dir):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_audit(memory_dir, [])
        return buf.getvalue()

    def test_flags_storage_keyword(self, tmp_path):
        (tmp_path / "common.md").write_text("- drbd replication needs LINSTOR config\n")
        out = self._run_audit(tmp_path)
        assert "storage" in out

    def test_flags_networking_keyword(self, tmp_path):
        (tmp_path / "common.md").write_text("- flannel VXLAN requires MTU tuning\n")
        out = self._run_audit(tmp_path)
        assert "networking" in out

    def test_ignores_universal_hard_rules(self, tmp_path):
        """Lines containing 'never'/'always' are treated as global rules — not flagged."""
        (tmp_path / "common.md").write_text("- Never run drbd without backup\n")
        out = self._run_audit(tmp_path)
        assert "✓" in out

    def test_clean_file_passes(self, tmp_path):
        (tmp_path / "common.md").write_text(
            "- Always check logs before escalating\n"
            "- Request reviews for all PRs\n"
        )
        out = self._run_audit(tmp_path)
        assert "✓" in out

    def test_multiple_domains_flagged(self, tmp_path):
        (tmp_path / "common.md").write_text(
            "- drbd volume needs format\n"
            "- grafana dashboard shows metrics\n"
        )
        out = self._run_audit(tmp_path)
        assert "storage" in out
        assert "observability" in out

    def test_missing_common_md_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            mn.cmd_audit(tmp_path, [])


class TestContentDefects:
    """scan_content_defects catches structural corruption line/byte checks miss."""

    def test_detects_joined_bullet(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **A**: text ending (2026-06-08).- **B**: merged on one line\n")
        kinds = [k for _, k, _ in mn.scan_content_defects(p)]
        assert "joined-line" in kinds

    def test_clean_file_no_defects(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **A**: a fact.\n- **B**: another fact.\n")
        assert mn.scan_content_defects(p) == []

    def test_legit_inline_dash_not_flagged(self, tmp_path):
        # space before the dash => legitimate inline emphasis, not a merged bullet
        p = tmp_path / "x.md"
        p.write_text("- **A**: uses X - **bold** mid sentence.\n")
        assert all(k != "joined-line" for _, k, _ in mn.scan_content_defects(p))

    def test_internal_hyphen_date_not_flagged(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **Window**: 2026-06-29/30 genesis-config window pending.\n")
        assert mn.scan_content_defects(p) == []

    def test_detects_runaway_line(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **Header**: " + ("accretion " * 230) + "\n")  # >2000 chars, no merge
        kinds = [k for _, k, _ in mn.scan_content_defects(p)]
        assert "long-line" in kinds and "joined-line" not in kinds


class TestMemoryByteBudget:
    """MEMORY.md injection-budget check (16KB warn / 20KB error). Regressed once
    when an installed-only copy was overwritten by source — now tested so it can't
    silently vanish again. Bodies use <180 short lines to isolate bytes from the
    line-count and runaway-line checks."""

    def _write(self, d, total_bytes, line_len=100):
        line = "z" * (line_len - 1) + "\n"
        n = total_bytes // line_len
        (d / "MEMORY.md").write_text(line * n)

    def test_over_16kb_is_yellow_advisory(self, tmp_path, capsys):
        self._write(tmp_path, 17000)                 # 170 lines, GREEN on lines
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "YELLOW" in out and "injection-budget" in out
        assert "within threshold" not in out

    def test_over_20kb_emits_blocking_red_marker(self, tmp_path, capsys):
        self._write(tmp_path, 22100, line_len=130)   # 170 lines, RED on bytes only
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "\033[31m\033[1mRED\033[0m" in out     # pre-commit hook greps this -> blocks

    def test_under_16kb_clean(self, tmp_path, capsys):
        self._write(tmp_path, 5100, line_len=51)     # 100 lines, all GREEN
        mn.cmd_health(tmp_path, [])
        assert "within threshold" in capsys.readouterr().out


class TestIndexGlossAccretion:
    """scan_index_gloss — Gap-32 structural cure. Flags MEMORY.md '## Project
    Files' index lines carrying accreted prose, measured link-count-agnostically
    by stripping [label](path) tokens. A vertical with MANY terse links must pass;
    one whose links carry paragraph glosses must flag. (This is the failure the
    2000-char runaway check + total-byte budget both miss.)"""

    TERSE = "- **Widgets**: " + " | ".join(f"[f{i}](project/p{i}.md)" for i in range(22))
    ACCRETED = ("- **Widgets**: [master](project/p.md) — "
                + "verbose resolved status detail from a session note " * 12)

    def _doc(self, *index_lines, recent=None):
        body = "# MEMORY\n\n## Project Files — grouped by vertical\n\n"
        body += "\n".join(index_lines) + "\n"
        if recent:
            body += "\n## Recent State\n\n" + recent + "\n"
        return body

    def test_terse_link_dense_line_passes(self, tmp_path):
        # 22 links, ~492 chars total, but tiny gloss — must NOT flag.
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE))
        assert mn.scan_index_gloss(p) == []

    def test_accreted_line_flagged(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.ACCRETED))
        hits = mn.scan_index_gloss(p)
        assert len(hits) == 1
        assert hits[0][1] > mn.INDEX_GLOSS_WARN     # gloss bytes over threshold

    def test_only_scans_project_files_section(self, tmp_path):
        # an accreted-looking line in Recent State must NOT be flagged
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE, recent=self.ACCRETED))
        assert mn.scan_index_gloss(p) == []

    def test_heaviest_first(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        small = "- **A**: [x](p.md) — " + "gloss " * 70
        big   = "- **B**: [y](p.md) — " + "gloss " * 140
        p.write_text(self._doc(small, big))
        hits = mn.scan_index_gloss(p)
        assert len(hits) == 2
        assert hits[0][1] > hits[1][1]              # heaviest-first

    def test_cmd_health_surfaces_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.ACCRETED))
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "Gap-32" in out
        assert "within threshold" not in out

    def test_cmd_health_clean_index_no_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.TERSE))
        mn.cmd_health(tmp_path, [])
        assert "Gap-32" not in capsys.readouterr().out

    def test_accretion_advisory_is_not_a_blocking_red_token(self, tmp_path, capsys):
        # the YELLOW accretion advisory must never emit the exact ANSI token the
        # pre-commit hook greps to block commits.
        (tmp_path / "MEMORY.md").write_text(self._doc(self.ACCRETED))
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestRecentStateAdvisory:
    """scan_recent_state_bullets — flags a fact that outgrew the ## Recent State
    changelog. Separate from scan_index_gloss (which is '## Project Files'-scoped
    and must stay so). Advisory only: YELLOW, never the blocking RED token."""

    FAT = "- **X**: " + "verbose resolved status detail from a session note " * 16
    TERSE = "- **Y**: [home](project/p.md) — shipped 07-19."

    def _doc(self, *recent_lines, project=None):
        body = "# MEMORY\n\n"
        if project:
            body += "## Project Files\n\n" + project + "\n\n"
        body += "## Recent State (2026-07-20)\n\n" + "\n".join(recent_lines) + "\n"
        return body

    def test_fat_recent_bullet_flagged(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.FAT))
        hits = mn.scan_recent_state_bullets(p)
        assert len(hits) == 1
        assert hits[0][1] > mn.RECENT_STATE_BULLET_WARN

    def test_terse_recent_bullet_passes(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE))
        assert mn.scan_recent_state_bullets(p) == []

    def test_index_gloss_does_not_scan_recent_state(self, tmp_path):
        # the FAT bullet lives ONLY in Recent State — scan_index_gloss must ignore it
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.FAT, project="- **P**: [x](project/p.md) terse"))
        assert mn.scan_index_gloss(p) == []

    def test_cmd_health_surfaces_recent_state_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.FAT))
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "Recent State bullet" in out
        assert "within threshold" not in out

    def test_recent_state_advisory_is_not_a_blocking_red_token(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.FAT))
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestPointerResolution:
    """scan_pointer_resolution — Gap-42. A pointer is a CLAIM about another file
    and rots independently of the bullet it sits on. Catches (b) dangling,
    (c) ambiguous bare basename, and the mechanical subset of (a): a §anchor
    naming a heading the target does not have. Advisory only: YELLOW, never the
    blocking RED token — stranding the nightly memory auto-commit over a broken
    link is a worse failure than the broken link."""

    @pytest.fixture(autouse=True)
    def _no_env_override(self, monkeypatch):
        monkeypatch.delenv("MNEMOSYNE_POINTER_FILES", raising=False)

    def _tree(self, tmp_path):
        """A memory root mirroring the real shape: networking.md collides across
        domain/ and troubleshooting/; etcd.md is unique to troubleshooting/."""
        for d in ("domain", "troubleshooting", "project", "skills", "sop"):
            (tmp_path / d).mkdir()
        (tmp_path / "domain" / "networking.md").write_text("# Networking\n")
        (tmp_path / "troubleshooting" / "networking.md").write_text("# Networking\n")
        (tmp_path / "troubleshooting" / "etcd.md").write_text("# etcd\n")
        (tmp_path / "gotchas.md").write_text("# Gotchas\n")
        # no `Registry` HEADING — the section is a bold label, not a heading
        (tmp_path / "domain" / "services.md").write_text(
            "# Services\n\n**Registry**:\n- pull-through cache serves stale tags\n")
        (tmp_path / "skills" / "audit.md").write_text("## RBAC Review Pattern\n")
        (tmp_path / "project" / "p.md").write_text("## Deploy Model & Infra Gotchas\n")
        return tmp_path

    def _doc(self, tmp_path, *body):
        (tmp_path / "MEMORY.md").write_text("# MEMORY\n\n" + "\n".join(body) + "\n")
        return tmp_path

    def _kinds(self, hits):
        return sorted((h[2], h[3]) for h in hits)

    # ── resolves clean ───────────────────────────────────────────────────────
    def test_qualified_paths_are_silent(self, tmp_path):
        root = self._doc(self._tree(tmp_path),
                         "- fact → `domain/networking.md`",
                         "- fact → [p](project/p.md)",
                         "- fact → `gotchas.md`",
                         "- fact → `domain/`")
        assert mn.scan_pointer_resolution(root) == []

    # ── class (b): dangling ──────────────────────────────────────────────────
    def test_dangling_slashed_path(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `project/project_nope.md`")
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("dangling", "project/project_nope.md")]

    def test_slashed_path_gets_no_fallback_rescue(self, tmp_path):
        # domain/etcd.md does not exist; troubleshooting/etcd.md does. A literal
        # path must NOT be silently rescued by searching the other roots.
        root = self._doc(self._tree(tmp_path), "- fact → `domain/etcd.md`")
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("dangling", "domain/etcd.md")]

    def test_bare_basename_at_the_root_resolves(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `gotchas.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_unique_bare_basename_in_a_subdir_is_unqualified(self, tmp_path):
        # Gap 42 class (c) is "unresolvable path": it does not exist AT the memory
        # root, so a follower has to guess the directory. A unique hit is not a
        # pass — the documented repair is to path-qualify it.
        root = self._doc(self._tree(tmp_path), "- fact → `etcd.md`")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("unqualified", "etcd.md")]
        assert "troubleshooting/etcd.md" in hits[0][4]

    def test_unqualified_still_checks_its_anchor(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `services.md` §Nope")
        # services.md collides, so it is ambiguous, not unqualified — use a unique one
        (tmp_path / "domain" / "solo.md").write_text("# Solo\n\n**Real**:\n")
        self._doc(root, "- fact → `solo.md` §Ghost")
        kinds = [h[2] for h in mn.scan_pointer_resolution(root)]
        assert "unqualified" in kinds and "anchor" in kinds

    # ── class (c): ambiguous ─────────────────────────────────────────────────
    def test_ambiguous_bare_basename(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("ambiguous", "networking.md")]
        assert "domain/networking.md" in hits[0][4]
        assert "troubleshooting/networking.md" in hits[0][4]

    def test_ambiguity_is_never_resolved_by_root_priority(self, tmp_path):
        # picking a winner would turn a visible failure into a confidently-wrong read
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        assert mn.scan_pointer_resolution(root)[0][2] == "ambiguous"

    def test_qualifying_the_path_is_the_repair(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `troubleshooting/networking.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_multiple_targets_on_one_line(self, tmp_path):
        # finditer, not search: one line carrying 3 chained targets
        root = self._doc(self._tree(tmp_path),
                         "- fact → `networking.md`/`etcd.md`/`gotchas.md`")
        # all three adjudicated independently: collides / subdir-only / at the root
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("ambiguous", "networking.md"), ("unqualified", "etcd.md")]

    # ── mechanical subset of class (a): anchors ──────────────────────────────
    def test_anchor_matches_bold_label_not_just_heading(self, tmp_path):
        # domain/services.md has no `Registry` heading — only `**Registry**:`.
        # A heading-only matcher false-positives here.
        root = self._doc(self._tree(tmp_path), "- fact → `domain/services.md` §Registry")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_absent_is_flagged(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `domain/services.md` §Scanner")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("anchor", "domain/services.md")]
        assert "§Scanner" in hits[0][4]

    def test_anchor_trailing_qualifier_is_stripped(self, tmp_path):
        # `§RBAC step 5` points at `## RBAC Review Pattern`, item 5. The
        # step/digit tail is a locator, not part of the section name.
        root = self._doc(self._tree(tmp_path), "- fact → `skills/audit.md` §RBAC step 5")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_binds_to_nearest_preceding_pointer(self, tmp_path):
        root = self._doc(self._tree(tmp_path),
                         "- fact → `gotchas.md` and → `domain/services.md` §Registry")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_on_directory_target_is_skipped(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `domain/` §Whatever")
        assert mn.scan_pointer_resolution(root) == []

    # ── false-positive defenses ──────────────────────────────────────────────
    @pytest.mark.parametrize("token", [
        "https://git.example.com/acme/infra.git",   # F1 URL
        "/admin/reports/summary",                   # F2 HTTP route
        "git ls-tree origin/main --name-only",      # F3 shell command
        "pods/exec:create",                         # F4 k8s verb
        "modules/widget*",                          # F4 glob
        "ci.yml",                                   # F5 not .md
        "./deploy.sh",                              # F5 not .md
        "tool.py",                                  # F5 not .md
        "networking",                               # F5 bare identifier
        "order_entries",                            # F5 code identifier
        "services/",                                # F6 another repo's dir
        ".cache/",                                  # F6 another repo's dir
        "origin/main",                              # F7 git ref
        "acme/widgets",                             # F7 repo slug
        "postmortem/2026-04-06-network-outage.md",  # another repo's .md
    ])
    def test_non_pointers_are_silent(self, tmp_path, token):
        root = self._doc(self._tree(tmp_path), f"- fact `{token}` in prose")
        assert mn.scan_pointer_resolution(root) == []

    def test_absolute_path_outside_the_memory_root_is_silent(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- vault at `~/infra/vault.yml`")
        assert mn.scan_pointer_resolution(root) == []

    def test_absolute_path_inside_the_memory_root_resolves(self, tmp_path):
        # the same tree written two ways must resolve to one identity
        root = self._tree(tmp_path)
        self._doc(root, f"- fact → `{root}/domain/networking.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_trailing_slash_survives_path_resolution(self, tmp_path):
        # Path.resolve() strips a trailing slash; reading the dir intent after
        # normalizing would silently demote this to a non-.md skip
        root = self._tree(tmp_path)
        self._doc(root, f"- SOPs live in `{root}/sop/`")
        assert mn.scan_pointer_resolution(root) == []

    # ── plumbing ─────────────────────────────────────────────────────────────
    def test_missing_memory_file_returns_empty(self, tmp_path):
        assert mn.scan_pointer_resolution(tmp_path) == []

    def test_kill_switch(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".gaius").mkdir(parents=True)
        (home / ".gaius" / "pointer-check-disabled").touch()
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        assert mn.scan_pointer_resolution(root)          # fires without the switch
        monkeypatch.setenv("HOME", str(home))
        assert mn.scan_pointer_resolution(root) == []

    def test_cmd_health_surfaces_pointer_rot(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "pointer(s) do not resolve" in out
        assert "within threshold" not in out

    def test_pointer_rot_is_not_a_blocking_red_token(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path),
                  "- a → `networking.md`",
                  "- b → `project/project_nope.md`",
                  "- c → `domain/services.md` §Scanner")
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestPointerContent:
    """Gap-42 class (a): pointer RESOLVES but the target lacks the fact.
    Bar is zero false POSITIVES — false negatives (fact homed under other
    wording) are accepted by design."""

    def _tree(self, tmp_path, body):
        (tmp_path / "project").mkdir(parents=True, exist_ok=True)
        (tmp_path / "project" / "t.md").write_text(body)
        return tmp_path

    def _doc(self, root, *lines):
        (root / "MEMORY.md").write_text("\n".join(lines) + "\n")
        return root

    def test_flags_when_no_ident_landed(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nunrelated prose\n"),
                         "- `headlamp-admin-token` and `credential-sync` → [x](project/t.md)")
        found = mn.scan_pointer_content(root)
        assert len(found) == 1
        assert found[0][1] == 1

    def test_silent_when_one_ident_landed(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nthe `drift-scan.sh` script\n"),
                         "- `drift-scan.sh:23` and `nowhere.yaml` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_whitespace_insensitive_match(self, tmp_path):
        """MEMORY.md compresses `pods:create`; the target writes `pods: create`.
        Same fact — this was the check's only real-world false positive."""
        root = self._doc(self._tree(tmp_path, "# T\nunscoped `pods: create` escalates\n"),
                         "- `pods:create` and `absent-thing.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_single_ident_is_too_fragile_to_judge(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nnothing\n"),
                         "- `lonely-token.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_dangling_pointer_left_to_sibling_scan(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nnothing\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/nope.md)")
        assert mn.scan_pointer_content(root) == []

    def test_kill_switch(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".gaius").mkdir(parents=True)
        (home / ".gaius" / "pointer-check-disabled").touch()
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root)             # fires without the switch
        monkeypatch.setenv("HOME", str(home))
        assert mn.scan_pointer_content(root) == []

    def test_not_a_blocking_red_token(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                  "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md)")
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "does not contain the fact" in out
        assert "\033[31m\033[1mRED\033[0m" not in out


class TestHeaviestLines:
    def test_returns_longest_first(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("short\n" + "m" * 200 + "\n" + "l" * 500 + "\n")
        rows = mn.heaviest_lines(p, n=2)
        assert len(rows) == 2
        assert rows[0][1] == 500 and rows[1][1] == 200


class TestScanRecentStateDuplicates:
    """scan_recent_state_duplicates — Gap-43. Two sessions writing the same event
    as two bullets was the dominant MEMORY.md byte-growth vector, and nothing
    detected it. Detection is by SHARED RARE IDENTIFIERS (issue refs, SHAs,
    backticked symbols), never prose similarity: two bullets on one SUBSYSTEM
    share none of those, two bullets on one EVENT share several.

    Zero false positives is the bar — the scanner is only worth reading if a hit
    means something. Every negative case below defends that bar."""

    def _mem(self, tmp_path, body):
        p = tmp_path / "MEMORY.md"
        p.write_text(body)
        return p

    def test_same_two_issue_refs_flags(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- shipped the alerting sweep (#285, #288) — pager never fired\n"
            "- the alert work landed in #285 and #288, evaluator was dead\n"
        ))
        hits = mn.scan_recent_state_duplicates(p)
        assert len(hits) == 1
        assert hits[0][2] == "same-ids"

    def test_same_subsystem_different_ids_is_clean(self, tmp_path):
        """The precision case: same words, no shared identifiers, no hit."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- alerting sweep for the site pager (#285, #288)\n"
            "- alerting sweep for the storage pager (#411, #412)\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_single_shared_id_is_not_enough(self, tmp_path):
        """DUP_MIN_STRONG is 2 — one co-cited issue is ordinary cross-reference."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the readiness gate fix landed in #290\n"
            "- unrelated work that also happens to mention #290 in passing\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_shared_sha_counts_as_strong(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- reverted in a1b2c3d and re-landed as e4f5a6b\n"
            "- the a1b2c3d revert plus e4f5a6b, same incident\n"
        ))
        hits = mn.scan_recent_state_duplicates(p)
        assert len(hits) == 1

    def test_common_vocabulary_is_dropped(self, tmp_path):
        """A token in more than DUP_DF_CAP bullets is this file's vocabulary,
        not an event fingerprint — otherwise every bullet pairs with every other."""
        rows = "".join(
            f"- bullet {i} about `flannel-mtu` and `tailscale0-pmtud` "
            f"and `drbd-quorum` and `lost-quorum-taint`\n"
            for i in range(6)
        )
        assert mn.scan_recent_state_duplicates(self._mem(tmp_path, "## Recent State\n" + rows)) == []

    def test_earlier_section_is_scanned_too(self, tmp_path):
        """A bullet re-added after being rolled to '## Earlier' is the same defect."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the pager sweep (#285, #288)\n"
            "\n## Standing Gates\n"
            "- something else entirely\n"
            "\n## Earlier\n"
            "- rolled bullet covering #285 and #288\n"
        ))
        assert len(mn.scan_recent_state_duplicates(p)) == 1

    def test_bullets_outside_the_scanned_sections_are_ignored(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Domain Files\n"
            "- the pager sweep (#285, #288)\n"
            "- duplicate of the pager sweep (#285, #288)\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_issue_range_interiors_are_NOT_expanded(self, tmp_path):
        """Endpoints-only is deliberate — see the _DUP_ISSUE comment. Expanding
        `#285-#288` to its interior was built and reverted 2026-07-26: across 40
        committed MEMORY.md versions it gained zero true duplicates and produced a
        false positive, because hand-written ranges are approximations. A bullet
        citing only an INTERIOR id must not pair with the range."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the sweep covering #285-#288 and also #301-#304\n"
            "- separate work on #286 and #302, different incident\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert mn.scan_recent_state_duplicates(tmp_path / "ghost.md") == []
