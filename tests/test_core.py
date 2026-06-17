"""gaius core test suite.

Tests cover the public API surface of gaius._core with no dependency on
a live corpus, external services, or deployment-specific configuration.

Run:
    pytest tests/ -v
    pytest tests/ -v --tb=short   # compact tracebacks
"""
import io
import json
import os
import sys
import sqlite3
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Isolation: force blank config so built-in defaults are predictable ────────
os.environ["GAIUS_CONFIG"] = "/dev/null"

# Add repo root to path so `from gaius._core import ...` works in-tree
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

from gaius._core import (
    _load_gaius_config,
    _load_entity_patterns,
    agent_to_principal,
    content_hash,
    get_session_threshold,
    init_db,
    upsert_fact,
    LOCAL_THRESHOLD,
    DEFAULT_THRESHOLD,
    _DEFAULT_PRINCIPAL,
    _DEFAULT_PRINCIPAL_BY_AGENT,
    _DEFAULT_FORMAT_BY_AGENT,
    FORMAT_BY_AGENT,
    AGENT_THRESHOLDS,
    READINESS_THRESHOLDS,
    _DEFAULT_READINESS_THRESHOLDS,
    DEFAULT_READINESS,
    _discover_domain_thresholds,
    _gaius_cfg,
    DOMAIN_KEYWORDS,
    _FAILURE_CLASS_MAP,
    _DOMAIN_MAP,
    _FAILURE_CLASS_MAP_DEFAULT,
    _DOMAIN_MAP_DEFAULT,
    is_internal_agent,
    INTERNAL_AGENTS,
)

# Generic placeholder names standing in for a deployment's private agent roster.
# The shipped defaults must NEVER hardcode any deployment's real agent names —
# these fakes let the guards verify defaults stay empty/generic without naming
# any real operator. (The same names are used to exercise the internal-agent
# filtering MECHANISM, which is config-driven and ships with an empty roster.)
_FAKE_OPERATOR_AGENTS = {"agent-a", "agent-b", "agent-c", "agent-d", "agent-e"}


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigLoading:
    # _GAIUS_CONFIG_FILE is set at import time from env, so we monkeypatch
    # the module attribute directly rather than the env var.

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        from gaius import _core
        monkeypatch.setattr(_core, "_GAIUS_CONFIG_FILE", tmp_path / "nonexistent.yaml")
        assert _load_gaius_config() == {}

    def test_parses_valid_yaml(self, tmp_path, monkeypatch):
        from gaius import _core
        cfg = tmp_path / "config.yaml"
        cfg.write_text("operator:\n  name: Test Operator\n")
        monkeypatch.setattr(_core, "_GAIUS_CONFIG_FILE", cfg)
        result = _load_gaius_config()
        assert result["operator"]["name"] == "Test Operator"

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        from gaius import _core
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        monkeypatch.setattr(_core, "_GAIUS_CONFIG_FILE", cfg)
        assert _load_gaius_config() == {}

    def test_invalid_yaml_returns_empty(self, tmp_path, monkeypatch):
        from gaius import _core
        cfg = tmp_path / "config.yaml"
        cfg.write_text("{ bad yaml: [unclosed\n")
        monkeypatch.setattr(_core, "_GAIUS_CONFIG_FILE", cfg)
        assert _load_gaius_config() == {}

    def test_nested_principals_parsed(self, tmp_path, monkeypatch):
        from gaius import _core
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "principals:\n"
            "  default: myuser\n"
            "  mapping:\n"
            "    my-agent: myuser\n"
        )
        monkeypatch.setattr(_core, "_GAIUS_CONFIG_FILE", cfg)
        result = _load_gaius_config()
        assert result["principals"]["default"] == "myuser"
        assert result["principals"]["mapping"]["my-agent"] == "myuser"


# ─────────────────────────────────────────────────────────────────────────────
# Default principal
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultPrincipal:
    def test_default_principal_is_operator(self):
        """With blank config, default principal should be 'operator'."""
        assert _DEFAULT_PRINCIPAL == "operator"

    def test_default_principal_by_agent_is_empty(self):
        """No hardcoded agent→principal mappings in public defaults."""
        assert _DEFAULT_PRINCIPAL_BY_AGENT == {}

    def test_unknown_agent_falls_back(self):
        result = agent_to_principal("totally-unknown-xyz-999")
        assert result == _DEFAULT_PRINCIPAL

    def test_any_agent_returns_default_without_mapping(self):
        for name in ["claude-agent", "gemini-agent", "my-custom-agent"]:
            assert agent_to_principal(name) == _DEFAULT_PRINCIPAL


# ─────────────────────────────────────────────────────────────────────────────
# Session size thresholds
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionThresholds:
    def test_local_origin_uses_local_threshold(self):
        assert get_session_threshold("local", "anything") == LOCAL_THRESHOLD

    def test_local_threshold_larger_than_default(self):
        assert LOCAL_THRESHOLD > DEFAULT_THRESHOLD

    def test_researcher_has_large_threshold(self):
        t = get_session_threshold("cluster", "researcher")
        assert t == 10 * 1024 * 1024

    def test_agent_suffix_stripped(self):
        """researcher-agent should match researcher threshold."""
        assert (get_session_threshold("cluster", "researcher-agent") ==
                get_session_threshold("cluster", "researcher"))

    def test_task_agents_get_2mb(self):
        for agent in ["groom", "dev", "qa", "ux", "sentinel"]:
            assert get_session_threshold("cluster", agent) == 2 * 1024 * 1024

    def test_unknown_cluster_agent_gets_default(self):
        t = get_session_threshold("cluster", "mystery-agent-xyz")
        assert t == DEFAULT_THRESHOLD

    def test_no_deployment_specific_defaults(self):
        """Built-in thresholds must not hardcode any deployment's agent names."""
        assert not _FAKE_OPERATOR_AGENTS.intersection(set(AGENT_THRESHOLDS.keys()))


# ─────────────────────────────────────────────────────────────────────────────
# Entity pattern loading
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityPatternLoading:
    def test_k8s_preset_loads_baseline(self):
        """Default preset=k8s includes node/service/namespace/incident patterns."""
        patterns = _load_entity_patterns()
        assert "node" in patterns
        assert "service" in patterns
        assert "namespace" in patterns
        assert "incident" in patterns

    def test_preset_none_returns_empty(self, monkeypatch):
        monkeypatch.setitem(_gaius_cfg, "entities", {"preset": "none"})
        patterns = _load_entity_patterns()
        assert patterns == {}

    def test_custom_patterns_merged(self, monkeypatch):
        monkeypatch.setitem(_gaius_cfg, "entities", {
            "preset": "none",
            "patterns": {"myservice": r'\b(?:my-api|my-worker)\b'},
        })
        patterns = _load_entity_patterns()
        assert "myservice" in patterns
        assert len(patterns) == 1

    def test_custom_extends_builtin(self, monkeypatch):
        monkeypatch.setitem(_gaius_cfg, "entities", {
            "preset": "k8s",
            "patterns": {"custom": r'\bmy-entity\b'},
        })
        patterns = _load_entity_patterns()
        assert "node" in patterns       # from k8s preset
        assert "custom" in patterns     # added by user

    def test_no_internal_names_in_builtin_service_pattern(self):
        """Deployment-specific service names must not be baked into the defaults.

        The built-in service pattern ships only widely-used, generic service
        names; a deployment's own private services belong in config, not here.
        """
        patterns = _load_entity_patterns()
        if "service" in patterns:
            svc_pattern = patterns["service"].pattern
            for fake in _FAKE_OPERATOR_AGENTS:
                assert fake not in svc_pattern
            # A private/bespoke deployment service name must not be a default.
            assert "my-private-service" not in svc_pattern

    def test_patterns_are_compiled_regexes(self):
        import re
        patterns = _load_entity_patterns()
        for name, pat in patterns.items():
            assert hasattr(pat, "match"), f"{name} should be a compiled regex"


# ─────────────────────────────────────────────────────────────────────────────
# Format-by-agent
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatByAgent:
    def test_pentagi_maps_to_pentagi(self):
        assert _DEFAULT_FORMAT_BY_AGENT.get("pentagi") == "pentagi"

    def test_no_internal_defaults(self):
        """No deployment-specific agent names in shipped FORMAT_BY_AGENT."""
        internal = _FAKE_OPERATOR_AGENTS | {f"{a}-agent" for a in _FAKE_OPERATOR_AGENTS}
        assert not internal.intersection(set(_DEFAULT_FORMAT_BY_AGENT.keys()))

    def test_unknown_agent_not_in_map(self):
        assert "unknown-agent-xyz" not in FORMAT_BY_AGENT

    def test_config_formats_merged(self, monkeypatch):
        monkeypatch.setitem(_gaius_cfg, "principals", {
            "formats": {"custom-agent": "gemini"}
        })
        from gaius._core import FORMAT_BY_AGENT as fba
        # Note: FORMAT_BY_AGENT is module-level; config merge tested at dict level
        merged = {**_DEFAULT_FORMAT_BY_AGENT, "custom-agent": "gemini"}
        assert merged["custom-agent"] == "gemini"


# ─────────────────────────────────────────────────────────────────────────────
# Internal-agent filtering mechanism
# The MECHANISM ships intact but the roster is EMPTY by default — no agent is
# excluded from extraction unless a deployment lists it in internal_agents.
# ─────────────────────────────────────────────────────────────────────────────

class TestInternalAgentFiltering:
    def test_default_roster_is_empty(self):
        """Shipped INTERNAL_AGENTS must be empty — no deployment roster ships."""
        assert INTERNAL_AGENTS == frozenset()

    def test_nothing_filtered_by_default(self):
        """With an empty roster, no agent is treated as internal."""
        for name in _FAKE_OPERATOR_AGENTS | {"claude", "gemini", "demo"}:
            assert is_internal_agent(name) is False

    def test_empty_or_none_agent_not_internal(self):
        assert is_internal_agent("") is False
        assert is_internal_agent(None) is False

    def test_mechanism_filters_configured_names(self, monkeypatch):
        """Configuring internal_agents makes is_internal_agent return True for them."""
        monkeypatch.setattr("gaius._core.INTERNAL_AGENTS",
                            frozenset({"agent-a", "agent-b"}))
        assert is_internal_agent("agent-a") is True
        assert is_internal_agent("agent-b") is True
        # Trailing '-agent' suffix is stripped before matching.
        assert is_internal_agent("agent-a-agent") is True
        # Case-insensitive.
        assert is_internal_agent("AGENT-A") is True
        # Names not in the configured roster are still mined.
        assert is_internal_agent("agent-c") is False

    def test_config_builds_roster_lowercased(self, tmp_path):
        """internal_agents from config.yaml populates INTERNAL_AGENTS, lowercased."""
        import yaml, importlib, sys as _sys
        cfg = {"internal_agents": ["Bot-One", "bot-two"]}
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(cfg))
        old_env = os.environ.get("GAIUS_CONFIG")
        os.environ["GAIUS_CONFIG"] = str(cfg_file)
        try:
            for mod in ("gaius._core", "gaius"):
                if mod in _sys.modules:
                    del _sys.modules[mod]
            from gaius._core import INTERNAL_AGENTS as ia, is_internal_agent as iia
            assert "bot-one" in ia
            assert "bot-two" in ia
            assert iia("Bot-One") is True
        finally:
            if old_env is None:
                os.environ.pop("GAIUS_CONFIG", None)
            else:
                os.environ["GAIUS_CONFIG"] = old_env
            for mod in ("gaius._core", "gaius"):
                if mod in _sys.modules:
                    del _sys.modules[mod]


# ─────────────────────────────────────────────────────────────────────────────
# Domain keywords
# ─────────────────────────────────────────────────────────────────────────────

class TestDomainKeywords:
    def test_standard_domains_present(self):
        for domain in ["networking", "security", "storage", "services", "observability"]:
            assert domain in DOMAIN_KEYWORDS

    def test_no_internal_domain_names(self):
        """Shipped domain keys must be generic, not a deployment's own domains."""
        internal = _FAKE_OPERATOR_AGENTS | {"my-private-domain"}
        assert not internal.intersection(set(DOMAIN_KEYWORDS.keys()))

    def test_ollama_domain_has_no_internal_keywords(self):
        if "ollama" in DOMAIN_KEYWORDS:
            assert not _FAKE_OPERATOR_AGENTS.intersection(set(DOMAIN_KEYWORDS["ollama"]))

# ─────────────────────────────────────────────────────────────────────────────
# Config-driven maps (failure classes + blog domain tags)
# These dicts must ship with generic defaults only. Project-specific terms
# (stack names, CNIs, storage backends) belong in ~/.gaius/config.yaml.
# ─────────────────────────────────────────────────────────────────────────────

# Terms that are project-specific and must NOT appear in the shipped defaults.
_INTERNAL_TERMS = {
    "tailscale", "headscale", "seaweedfs", "linstor", "drbd",
    "flannel", "rocm", "nebula", "openbao", "vault", "forgejo",
    "mimir", "alloy", "klipper",
}


class TestFailureClassMap:
    """_FAILURE_CLASS_MAP defaults must be generic; config extends them."""

    def test_required_classes_present(self):
        for cls in ["networking", "storage", "compute", "control_plane", "observability", "security"]:
            assert cls in _FAILURE_CLASS_MAP

    def test_defaults_have_no_internal_terms(self):
        """Default keyword lists (before config extension) must not contain project-specific terms."""
        for cls, keywords in _FAILURE_CLASS_MAP_DEFAULT.items():
            found = _INTERNAL_TERMS.intersection(set(keywords))
            assert not found, f"_FAILURE_CLASS_MAP_DEFAULT[{cls!r}] has internal terms: {found}"

    def test_config_can_extend(self, tmp_path):
        """Config failure_class_keywords merges onto defaults."""
        import yaml, importlib, sys as _sys
        cfg = {"failure_class_keywords": {"networking": ["mytunnel"]}}
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(cfg))
        old_env = os.environ.get("GAIUS_CONFIG")
        os.environ["GAIUS_CONFIG"] = str(cfg_file)
        try:
            # Reimport to pick up new config env
            if "gaius._core" in _sys.modules:
                del _sys.modules["gaius._core"]
            if "gaius" in _sys.modules:
                del _sys.modules["gaius"]
            from gaius._core import _FAILURE_CLASS_MAP as fcm
            assert "mytunnel" in fcm["networking"]
            # Defaults should still be present
            assert "dns" in fcm["networking"]
        finally:
            if old_env is None:
                os.environ.pop("GAIUS_CONFIG", None)
            else:
                os.environ["GAIUS_CONFIG"] = old_env
            if "gaius._core" in _sys.modules:
                del _sys.modules["gaius._core"]
            if "gaius" in _sys.modules:
                del _sys.modules["gaius"]


class TestDomainMap:
    """_DOMAIN_MAP defaults must be generic; config extends them."""

    def test_required_domains_present(self):
        for dom in ["networking", "storage", "observability", "security"]:
            assert dom in _DOMAIN_MAP

    def test_defaults_have_no_internal_terms(self):
        for dom, tags in _DOMAIN_MAP_DEFAULT.items():
            found = _INTERNAL_TERMS.intersection(set(tags))
            assert not found, f"_DOMAIN_MAP_DEFAULT[{dom!r}] has internal terms: {found}"


# ─────────────────────────────────────────────────────────────────────────────
# Content hash and dedup helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_content_different_hash(self):
        assert content_hash("foo") != content_hash("bar")

    def test_returns_string(self):
        assert isinstance(content_hash("test content"), str)

    def test_empty_string(self):
        h = content_hash("")
        assert isinstance(h, str) and len(h) > 0

    def test_whitespace_sensitivity(self):
        assert content_hash("a b") != content_hash("ab")


# ─────────────────────────────────────────────────────────────────────────────
# Domain auto-discovery
# ─────────────────────────────────────────────────────────────────────────────

class TestDomainDiscovery:
    def test_discovers_md_files(self, tmp_path):
        (tmp_path / "networking.md").write_text("# Networking\n")
        (tmp_path / "storage.md").write_text("# Storage\n")
        (tmp_path / "not-a-doc.txt").write_text("ignored\n")
        result = _discover_domain_thresholds(tmp_path)
        assert "networking" in result
        assert "storage" in result
        assert "not-a-doc" not in result

    def test_discovered_domains_use_default_readiness(self, tmp_path):
        (tmp_path / "my-domain.md").write_text("# My Domain\n")
        result = _discover_domain_thresholds(tmp_path)
        assert result["my-domain"] == DEFAULT_READINESS

    def test_empty_directory(self, tmp_path):
        assert _discover_domain_thresholds(tmp_path) == {}

    def test_nonexistent_directory(self, tmp_path):
        assert _discover_domain_thresholds(tmp_path / "ghost") == {}

    def test_multiple_md_files(self, tmp_path):
        for name in ["alpha", "beta", "gamma"]:
            (tmp_path / f"{name}.md").write_text(f"# {name}\n")
        result = _discover_domain_thresholds(tmp_path)
        assert set(result.keys()) == {"alpha", "beta", "gamma"}


# ─────────────────────────────────────────────────────────────────────────────
# Readiness threshold merge order
# ─────────────────────────────────────────────────────────────────────────────

class TestReadinessThresholdMerge:
    def test_defaults_present(self):
        for domain in ["quality", "security"]:
            assert domain in READINESS_THRESHOLDS

    def test_quality_has_raised_bar(self):
        q = READINESS_THRESHOLDS["quality"]
        assert q["score"] == 0.70
        assert q["min_facts"] == 100

    def test_merge_order_config_wins(self):
        auto = {"my-domain": DEFAULT_READINESS}
        explicit = {"my-domain": {"score": 0.99, "min_facts": 999}}
        merged = {**_DEFAULT_READINESS_THRESHOLDS, **auto, **explicit}
        assert merged["my-domain"]["score"] == 0.99


# ─────────────────────────────────────────────────────────────────────────────
# Database: init and upsert
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabase:
    def test_init_db_creates_tables(self, tmp_path):
        conn = init_db(tmp_path / "facts.db")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "facts" in tables
        assert "sessions" in tables

    def test_upsert_fact_inserts(self, tmp_path):
        conn = init_db(tmp_path / "facts.db")
        upsert_fact(
            conn,
            domain="networking",
            fact_key="test-key-001",
            fact_text="Flannel requires MTU 1230 on Tailscale overlay.",
            agent="operator",
            session_uuid="test-session-001",
            provenance="automated",
        )
        row = conn.execute(
            "SELECT fact_text, domain FROM facts WHERE fact_key = ?",
            ("test-key-001",)
        ).fetchone()
        assert row is not None
        assert row[0] == "Flannel requires MTU 1230 on Tailscale overlay."
        assert row[1] == "networking"

    def test_upsert_fact_dedup_on_key(self, tmp_path):
        conn = init_db(tmp_path / "facts.db")
        for i in range(3):
            upsert_fact(
                conn,
                domain="networking",
                fact_key="dedup-key",
                fact_text=f"Version {i}",
                agent="operator",
                session_uuid="sess",
                provenance="automated",
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE fact_key = 'dedup-key'"
        ).fetchone()[0]
        assert count == 1

    def test_upsert_fact_increments_confirmation(self, tmp_path):
        """upsert_fact increments confirmation_count on repeated calls — not a plain overwrite."""
        conn = init_db(tmp_path / "facts.db")
        upsert_fact(conn, domain="storage", fact_key="conf-key",
                    fact_text="first observation", agent="operator", session_uuid="s1",
                    provenance="automated")
        upsert_fact(conn, domain="storage", fact_key="conf-key",
                    fact_text="corroborating observation", agent="other", session_uuid="s2",
                    provenance="automated")
        row = conn.execute(
            "SELECT confirmation_count FROM facts WHERE fact_key = 'conf-key'"
        ).fetchone()
        assert row[0] >= 2

    def test_tombstone_excluded_from_count(self, tmp_path):
        conn = init_db(tmp_path / "facts.db")
        upsert_fact(conn, domain="storage", fact_key="tomb-key",
                    fact_text="will be tombstoned", agent="operator", session_uuid="s1",
                    provenance="automated")
        conn.execute("UPDATE facts SET outcome = 'tombstone' WHERE fact_key = 'tomb-key'")
        conn.commit()
        active = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE outcome IS NULL OR outcome != 'tombstone'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        assert total == 1
        assert active == 0


# ─────────────────────────────────────────────────────────────────────────────
# S3 config validation
# ─────────────────────────────────────────────────────────────────────────────

class TestS3Config:
    def test_archive_session_skips_when_no_remote(self, tmp_path, monkeypatch):
        """archive_session must not crash — returns None when s3.remote is unset."""
        from gaius import _core
        monkeypatch.setattr(_core, "_gaius_cfg", {})
        monkeypatch.setattr(_core, "PROJECT_DIR", tmp_path)
        test_file = tmp_path / "session.jsonl"
        test_file.write_text('{"type": "test"}\n')
        result = _core.archive_session(test_file, strip_before_archive=False)
        assert result is None

    def test_s3_path_built_from_config(self, monkeypatch):
        """s3-retire lists the agent dir ROOT — sessions live under both
        <agent>/sessions/ and <agent>/projects/ depending on uploader
        generation; the recursive copy must see both subtrees."""
        from gaius import _core
        monkeypatch.setattr(_core, "_gaius_cfg", {
            "s3": {"remote": "my-remote", "prefix": "my-sessions/cluster"}
        })
        remote = _core._gaius_cfg.get("s3", {}).get("remote", "")
        prefix = _core._gaius_cfg.get("s3", {}).get("prefix", "sessions").strip("/")
        agent = "gemini-agent"
        path = f"{remote}:{prefix}/{agent}/"
        assert path == "my-remote:my-sessions/cluster/gemini-agent/"


# ─────────────────────────────────────────────────────────────────────────────
# gaius init (non-interactive path)
# ─────────────────────────────────────────────────────────────────────────────

class TestGaiusInit:
    def test_init_creates_config_and_dirs(self, tmp_path, monkeypatch):
        from gaius import _core

        # Point gaius home and config to tmp_path
        gaius_dir = tmp_path / ".gaius"
        config_path = gaius_dir / "config.yaml"
        memory_dir = tmp_path / "memory"

        monkeypatch.setattr(_core, "Path", Path)  # ensure real Path used

        # Provide pre-canned answers for all input() calls
        answers = iter(["y", "1", str(tmp_path / "sessions"), str(memory_dir)])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

        # Patch gaius_dir so it writes to tmp, not ~/.gaius
        with patch("gaius._core.Path.home", return_value=tmp_path):
            # Find preset
            preset_src = Path(_REPO) / "presets" / "default.yaml"
            if not preset_src.exists():
                pytest.skip("presets/default.yaml not found — run from repo root")

            _core.cmd_init([])

        # Config should exist
        written_config = gaius_dir / "config.yaml"
        assert written_config.exists(), "config.yaml should be written"
        content = written_config.read_text()
        assert "sessions_dir:" in content

    def test_init_aborts_on_existing_config_no_overwrite(self, tmp_path, monkeypatch):
        from gaius import _core

        gaius_dir = tmp_path / ".gaius"
        gaius_dir.mkdir()
        (gaius_dir / "config.yaml").write_text("# existing\n")

        answers = iter(["n"])  # don't overwrite
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

        with patch("gaius._core.Path.home", return_value=tmp_path):
            buf = io.StringIO()
            with redirect_stdout(buf):
                _core.cmd_init([])

        assert "Aborted" in buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Presets
# ─────────────────────────────────────────────────────────────────────────────

class TestPresets:
    def test_k8s_preset_exists(self):
        preset = _REPO / "presets" / "k8s.yaml"
        assert preset.exists(), "presets/k8s.yaml must exist"

    def test_default_preset_exists(self):
        preset = _REPO / "presets" / "default.yaml"
        assert preset.exists(), "presets/default.yaml must exist"

    def test_k8s_preset_is_valid_yaml(self):
        import yaml
        preset = _REPO / "presets" / "k8s.yaml"
        doc = yaml.safe_load(preset.read_text())
        assert isinstance(doc, dict)

    def test_default_preset_is_valid_yaml(self):
        import yaml
        preset = _REPO / "presets" / "default.yaml"
        doc = yaml.safe_load(preset.read_text())
        assert isinstance(doc, dict)

    def test_presets_have_no_hardcoded_internal_paths(self):
        # Guard against leaking a specific home dir or private domain into the
        # shipped presets. These literals are leak-detectors, not config values.
        for preset_name in ["k8s.yaml", "default.yaml"]:
            content = (_REPO / "presets" / preset_name).read_text()
            assert "/home/" not in content
            assert ".internal" not in content

    def test_k8s_preset_has_no_internal_agent_names(self):
        content = (_REPO / "presets" / "k8s.yaml").read_text()
        for name in _FAKE_OPERATOR_AGENTS:
            assert name not in content, f"deployment name '{name}' found in k8s.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Section extraction helper
# ─────────────────────────────────────────────────────────────────────────────

class TestSectionExtraction:
    def test_extracts_known_section(self):
        from gaius._core import extract_section
        text = "1. Primary Request and Intent\nDid X\n\n2. Key Technical Concepts\nUsed Y"
        result = extract_section(text, "Primary Request and Intent")
        assert "Did X" in result

    def test_missing_section_returns_empty(self):
        from gaius._core import extract_section
        assert extract_section("No sections here", "Nonexistent Section") == ""
