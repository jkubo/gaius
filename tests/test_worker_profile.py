"""Tests for `gaius inject --profile worker` — the identity-stripped bundle fed
to commanded non-agent models (headless grok, local vLLM).

The contract under test: a worker never receives the deployment's agent identity,
governance rules, authority context, or a predecessor's handoff. Identity is not a
string in this codebase — it arrives as DATA through the skills, memory, handoff
and corpus channels — so these tests pin the channel allowlist AND the marker
backstop, and assert the default profile is untouched.
"""
import pytest

import gaius._core as _gaius_mod
from gaius import landscape


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Never touch the live DB."""
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


class TestIdentityMarker:
    """Markers are deployment config, so these tests supply their own synthetic
    set rather than asserting against whatever this install happens to name its
    agents — the behaviour under test is the matching, not the vocabulary."""

    @pytest.fixture(autouse=True)
    def _markers(self, monkeypatch):
        monkeypatch.setattr(landscape, "_WORKER_IDENTITY_MARKERS",
                            ("overseer", "tribunal", "standing vote"))

    def test_clean_text_has_no_marker(self):
        assert not landscape._has_identity_marker(
            "DRBD quorum lost on node-01; check linstor resource list")

    def test_marker_is_detected(self):
        assert landscape._has_identity_marker("escalate to the tribunal for a ruling")

    def test_multiword_marker_is_detected(self):
        assert landscape._has_identity_marker("holds a standing vote on proposals")

    def test_marker_match_is_case_insensitive(self):
        assert landscape._has_identity_marker("OVERSEER said so")

    def test_marker_matches_inside_a_word_boundary_free_substring(self):
        """Substring matching is intentional: agent names appear in compounds
        (e.g. '<name>-trader' in a skill body) that a word-boundary match misses."""
        assert landscape._has_identity_marker("routed to overseer-trader for sizing")

    def test_empty_text_is_not_a_marker(self):
        assert not landscape._has_identity_marker("")
        assert not landscape._has_identity_marker(None)

    def test_no_markers_configured_means_no_filtering(self, monkeypatch):
        """An install with no agent names needs no filter — but it must then be
        reported as unconfigured rather than passing as a clean run (the header
        prints NO MARKERS CONFIGURED)."""
        monkeypatch.setattr(landscape, "_WORKER_IDENTITY_MARKERS", ())
        assert not landscape._has_identity_marker("overseer, tribunal")


class TestWorkerPreamble:
    def test_override_file_is_used(self, tmp_path, monkeypatch):
        p = tmp_path / "pre.md"
        p.write_text("CUSTOM VOICE", encoding="utf-8")
        monkeypatch.setenv("GAIUS_WORKER_PREAMBLE", str(p))
        text, src = landscape._worker_preamble()
        assert text == "CUSTOM VOICE"
        assert src == "custom"

    def test_missing_override_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GAIUS_WORKER_PREAMBLE", str(tmp_path / "nope.md"))
        text, src = landscape._worker_preamble()
        assert src == "default"
        assert "report of CLAIMS" in text

    def test_empty_override_falls_back_to_default(self, tmp_path, monkeypatch):
        p = tmp_path / "empty.md"
        p.write_text("   \n", encoding="utf-8")
        monkeypatch.setenv("GAIUS_WORKER_PREAMBLE", str(p))
        _, src = landscape._worker_preamble()
        assert src == "default"

    def test_default_preamble_carries_no_identity(self):
        """The shipped default must be safe for any deployment — it is the text
        used when an operator has not written their own."""
        assert not landscape._has_identity_marker(landscape._WORKER_PREAMBLE_DEFAULT)

    def test_default_preamble_states_the_three_worker_rules(self):
        d = landscape._WORKER_PREAMBLE_DEFAULT
        assert "not that" in d.lower() or "hold no standing" in d.lower()  # not the agent
        assert "CLAIMS" in d                                              # claims, not fact
        assert "read-only" in d.lower()                                   # scope discipline
        assert "never" in d.lower() and "instructions" in d.lower()       # injection resistance


class TestWorkerChannelAllowlist:
    def test_worker_memory_dirs_exclude_governance_channels(self):
        """feedback = how to work with the operator, project = authority/decisions,
        user = who the operator is. None belong in a worker bundle."""
        assert "domain" in landscape._WORKER_MEMORY_DIRS
        assert "reference" in landscape._WORKER_MEMORY_DIRS
        for excluded in ("feedback", "project", "user"):
            assert excluded not in landscape._WORKER_MEMORY_DIRS


class TestWorkerInjectEndToEnd:
    """Drive cmd_inject with an empty corpus so the run is hermetic: the preamble
    must still ship (it is the one part a worker always needs), and no identity
    may appear."""

    def _run(self, capsys, argv, tmp_path, monkeypatch, preamble=None):
        monkeypatch.setattr(landscape, "MEMORY_DIR", tmp_path / "no-memory")
        monkeypatch.setattr(landscape, "load_skills", lambda: [])
        monkeypatch.setenv("GAIUS_HANDOFF_DIR", str(tmp_path / "no-handoffs"))
        # Hermetic means hermetic: without this the run reads the OPERATOR's
        # ~/.gaius/worker_preamble.md, so asserting on preamble wording below
        # passed or failed on whatever that machine's deployment voice happened
        # to say (caught 2026-08-16, when the kub0 override replaced the v0
        # placeholder). Point it at nothing so the SHIPPED default is what the
        # end-to-end path exercises.
        monkeypatch.setenv("GAIUS_WORKER_PREAMBLE",
                           str(preamble or tmp_path / "no-preamble.md"))
        try:
            landscape.cmd_inject(argv)
        except SystemExit:
            pass
        return capsys.readouterr().out

    def test_worker_profile_emits_preamble_even_when_bundle_is_empty(
            self, capsys, tmp_path, monkeypatch):
        out = self._run(capsys, ["--budget", "500", "--task", "zzz-no-match",
                                 "--profile", "worker"], tmp_path, monkeypatch)
        assert "# Worker Context Bundle" in out
        assert "preamble: default" in out          # the override is isolated above
        assert "report of CLAIMS" in out           # the shipped default's wording

    def test_operator_override_reaches_the_bundle(self, capsys, tmp_path,
                                                  monkeypatch):
        """A deployment's own preamble must actually ship — and be labeled as
        custom, since that tag is the only tell that the operator's voice (not
        the generic default) is what the worker read."""
        p = tmp_path / "voice.md"
        p.write_text("Role: legionary. Deployment voice.", encoding="utf-8")
        out = self._run(capsys, ["--budget", "500", "--task", "zzz-no-match",
                                 "--profile", "worker"], tmp_path, monkeypatch,
                        preamble=p)
        assert "preamble: custom" in out and "Deployment voice." in out

    def test_default_profile_emits_no_preamble(self, capsys, tmp_path, monkeypatch):
        """Regression guard: the default bundle must be byte-for-byte unaffected."""
        out = self._run(capsys, ["--budget", "500", "--task", "zzz-no-match"],
                        tmp_path, monkeypatch)
        assert "# Worker Context Bundle" not in out

    def test_worker_profile_reports_filter_state(self, capsys, tmp_path, monkeypatch):
        """The filter must be observable. A run that dropped nothing and a run on an
        install with no markers configured must not look identical.

        BOTH states are set explicitly. `_WORKER_IDENTITY_MARKERS` ships EMPTY and is
        populated per deployment, so reading the ambient value made this pass in a
        configured tree and fail in the published one — a divergence only a run in
        the mirror could surface.
        """
        monkeypatch.setattr(landscape, "_WORKER_IDENTITY_MARKERS",
                            ("alpha-agent", "beta-agent"))
        out = self._run(capsys, ["--budget", "500", "--task", "zzz-no-match",
                                 "--profile", "worker"], tmp_path, monkeypatch)
        assert "identity-filter:" in out and "markers" in out

        monkeypatch.setattr(landscape, "_WORKER_IDENTITY_MARKERS", ())
        out2 = self._run(capsys, ["--budget", "500", "--task", "zzz-no-match",
                                  "--profile", "worker"], tmp_path, monkeypatch)
        assert "NO MARKERS CONFIGURED" in out2
        assert out != out2

    def test_invalid_profile_is_rejected(self, capsys, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            landscape.cmd_inject(["--budget", "500", "--profile", "nonsense"])
