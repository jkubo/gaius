"""Gap-13 KG rebuild — entity extraction expansion, weighted co-occurrence,
fact_entities membership, incremental indexing, and export-links."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import init_db, upsert_fact
import gaius.kg as kg
from gaius.kg import (
    extract_entities,
    kg_index_fact,
    refresh_entity_domains,
    add_cooccurrence,
    cmd_kg_export_links,
    _MAX_COOCCUR_ENTITIES,
)


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Never touch the live DB."""
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "facts.db")
    yield c
    c.close()


def _insert_fact(conn, fact_id, text, domain="general"):
    conn.execute(
        "INSERT INTO facts (id, domain, fact_key, fact_text, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (fact_id, domain, f"key-{fact_id}", text))


# ── Extraction ───────────────────────────────────────────────────────────────

class TestExtraction:
    def test_cve_extraction(self):
        ents = extract_entities("DirtyClone CVE-2026-43503 affects the page cache")
        assert ("cve:cve-2026-43503", "cve-2026-43503", "cve") in ents

    def test_model_extraction(self):
        ents = extract_entities("vigiles runs gemma4-31b on the gx10")
        assert any(e[2] == "model" and e[1] == "gemma4-31b" for e in ents)
        # bare-number version segments must not break the chain (llama-3-70b)
        ents = extract_entities("benchmarked llama-3-70b and qwen2-72b today")
        names = {e[1] for e in ents if e[2] == "model"}
        assert {"llama-3-70b", "qwen2-72b"} <= names

    def test_expanded_services(self):
        ents = extract_entities("seaweedfs filer OOMed; clickhouse ingest and drbd held")
        names = {e[1] for e in ents if e[2] == "service"}
        assert {"seaweedfs", "clickhouse", "drbd"} <= names

    def test_namespace_requires_context_anchor(self):
        # anchored forms extract
        ents = extract_entities("pods in namespace seaweedfs crashed")
        assert ("namespace:seaweedfs", "seaweedfs", "namespace") in ents
        ents = extract_entities("kubectl get pods -n networking | grep flannel")
        assert any(e[0] == "namespace:networking" for e in ents)
        ents = extract_entities("apply with --namespace piraeus-datastore today")
        assert any(e[0] == "namespace:piraeus-datastore" for e in ents)
        # the colon form is deliberately NOT an anchor — it caught Tetragon
        # Linux-ns selectors ("namespace: Pid") and YAML listings on the corpus
        ents = extract_entities("selector has namespace: pid, operator: NotIn")
        assert not [e for e in ents if e[2] == "namespace"]
        # bare common words do NOT extract (the old pattern's false-positive flood)
        ents = extract_entities("the default configuration provides security by default")
        assert not [e for e in ents if e[2] == "namespace"]

    def test_namespace_prose_forms_do_not_extract(self):
        # post-"namespace" verbs and pre-"namespace" adjectives were the
        # measured junk classes on the live corpus — must stay unmatched
        for prose in ["delete this namespace now",
                      "the namespace holds the pods",
                      "moved into the shared namespace yesterday",
                      "namespace verified after the rollout"]:
            ents = extract_entities(prose)
            assert not [e for e in ents if e[2] == "namespace"], prose

    def test_namespace_short_flag_requires_k8s_cli_context(self):
        # every unix tool has a -n flag — without a k8s CLI word on the line
        # these were extracting shell noise (grep -n, sort -n, curl -n)
        for prose in ["grep -n pattern file.txt",
                      "sort -n sizes.txt | head",
                      "curl -n netrc-endpoint"]:
            ents = extract_entities(prose)
            assert not [e for e in ents if e[2] == "namespace"], prose
        ents = extract_entities("helm upgrade seaweedfs -n seaweedfs-ns ./chart")
        assert any(e[0] == "namespace:seaweedfs-ns" for e in ents)

    def test_node_pattern_unchanged(self):
        ents = extract_entities("k8s-aus-fwd-gpu-01 rebooted")
        assert any(e[2] == "node" for e in ents)

    def test_high_frequency_prose_incidents_removed(self):
        ents = extract_entities("request timeout and degraded performance")
        assert not [e for e in ents if e[2] == "incident"]

    def test_aliases_canonicalize(self):
        # builtin morphology aliases: postgres→postgresql, oomkilled→oomkill
        ents = extract_entities("postgres pod was oomkilled again")
        ids = {e[0] for e in ents}
        assert "service:postgresql" in ids and "service:postgres" not in ids
        assert "incident:oomkill" in ids and "incident:oomkilled" not in ids
        # alias target dedups with a direct mention of the canonical form
        ents = extract_entities("postgres and postgresql are the same thing")
        assert len([e for e in ents if e[2] == "service"]) == 1

    def test_compound_product_names_excluded(self):
        ents = extract_entities("Grafana Alloy DaemonSet ships logs to Docker Hub")
        names = {e[1] for e in ents if e[2] == "service"}
        assert "grafana" not in names   # "Grafana Alloy" is Alloy, not the dashboard
        assert "docker" not in names    # "Docker Hub" is a registry, not the runtime
        assert "alloy" in names
        ents = extract_entities("grafana dashboard broke and docker restarted")
        names = {e[1] for e in ents if e[2] == "service"}
        assert {"grafana", "docker"} <= names

    def test_model_pattern_excludes_pvc_names(self):
        ents = extract_entities("pvc-273b went StandAlone on drbd")
        assert not [e for e in ents if e[2] == "model"]


# ── Indexing: fact_entities, weights, typed pairs, watermark ─────────────────

class TestKgIndexFact:
    def test_fact_entities_and_watermark(self, conn):
        _insert_fact(conn, 1, "seaweedfs uses drbd volumes", "storage")
        kg_index_fact(conn, 1, "seaweedfs uses drbd volumes", "storage")
        conn.commit()
        fe = conn.execute("SELECT entity_id FROM fact_entities WHERE fact_id = 1").fetchall()
        assert {r[0] for r in fe} >= {"service:seaweedfs", "service:drbd"}
        stamped = conn.execute("SELECT kg_indexed_at FROM facts WHERE id = 1").fetchone()[0]
        assert stamped is not None

    def test_cooccurrence_weight_aggregates(self, conn):
        for fid, text in [(1, "drbd and linstor quorum"), (2, "linstor drives drbd replication")]:
            _insert_fact(conn, fid, text, "storage")
            kg_index_fact(conn, fid, text, "storage")
        conn.commit()
        rows = conn.execute(
            "SELECT subject, object, weight FROM triples WHERE predicate = 'co_occurs_with'").fetchall()
        assert len(rows) == 1  # one aggregated edge, not one per fact
        subj, obj, weight = rows[0]
        assert weight == 2
        assert subj < obj  # canonical symmetric order

    def test_cooccurrence_predicates_stay_weak(self, conn):
        # Co-occurrence must never assert strong semantics (affected_by etc.) —
        # a Splunk-CVE fact would otherwise brand postgresql as affected. Cross-
        # type pairs get symmetric mentioned_with with sorted subject.
        text = "k8s-aus-fwd-gpu-01 vulnerable to CVE-2026-43503"
        _insert_fact(conn, 1, text, "security")
        kg_index_fact(conn, 1, text, "security")
        row = conn.execute("SELECT subject, predicate, object FROM triples").fetchone()
        assert row[1] == "mentioned_with"
        assert row[0] == "cve:cve-2026-43503"       # sorted: c < n
        assert row[2] == "node:k8s-aus-fwd-gpu-01"
        strong = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE predicate IN "
            "('affected_by','has_storage','runs_model')").fetchone()[0]
        assert strong == 0

    def test_cooccurrence_respects_proximity_window(self, conn):
        text = "drbd quorum flapped this morning." + (" filler" * 60) + " unrelated linstor footnote"
        _insert_fact(conn, 1, text, "storage")
        kg_index_fact(conn, 1, text, "storage")
        assert conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0] == 0
        # both entities still get fact membership rows
        fe = {r[0] for r in conn.execute("SELECT entity_id FROM fact_entities").fetchall()}
        assert {"service:drbd", "service:linstor"} <= fe

    def test_relation_derived_entities_get_fact_links(self, conn):
        text = "cctv-api runs on k8s-aus-fwd-gpu-02 now"
        _insert_fact(conn, 1, text, "services")
        kg_index_fact(conn, 1, text, "services")
        row = conn.execute(
            "SELECT subject, predicate, object FROM triples WHERE predicate = 'runs_on'").fetchone()
        assert row is not None
        fe = {r[0] for r in conn.execute("SELECT entity_id FROM fact_entities").fetchall()}
        assert row[0] in fe and row[2] in fe  # invariant: every entity reachable from a fact

    def test_pair_cap_reports_skipped(self, conn, monkeypatch):
        many = "traefik nginx grafana prometheus loki mimir redis etcd flannel kafka"
        _insert_fact(conn, 1, many, "services")
        skipped = kg_index_fact(conn, 1, many, "services")
        assert skipped == 10 - _MAX_COOCCUR_ENTITIES
        n_pairs = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE predicate = 'co_occurs_with'").fetchone()[0]
        assert n_pairs == _MAX_COOCCUR_ENTITIES * (_MAX_COOCCUR_ENTITIES - 1) // 2

    def test_excluded_types_get_no_edges_but_keep_membership(self, conn, monkeypatch):
        import re as _re
        monkeypatch.setattr(kg, "_ENTITY_PATTERNS", {
            "agent": _re.compile(r"\bclaudeus\b", _re.I),
            "service": _re.compile(r"\bdrbd\b", _re.I),
        })
        _insert_fact(conn, 1, "claudeus fixed drbd", "storage")
        kg_index_fact(conn, 1, "claudeus fixed drbd", "storage")
        assert conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0] == 0
        fe = {r[0] for r in conn.execute("SELECT entity_id FROM fact_entities").fetchall()}
        assert "agent:claudeus" in fe and "service:drbd" in fe

    def test_refresh_entity_domains_majority_vote(self, conn):
        for fid, domain in [(1, "storage"), (2, "storage"), (3, "general")]:
            text = "drbd resource stuck"
            _insert_fact(conn, fid, text, domain)
            kg_index_fact(conn, fid, text, domain)
        refresh_entity_domains(conn)
        conn.commit()
        dom = conn.execute("SELECT domain FROM entities WHERE id = 'service:drbd'").fetchone()[0]
        assert dom == "storage"

    def test_upsert_fact_auto_indexes_kg(self, conn):
        upsert_fact(conn, "storage", "auto-kg-1", "seaweedfs volume on drbd failed",
                    agent="test", session_uuid="s1", provenance="test")
        fe = conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
        assert fe >= 2  # KG stays current at insert time, no manual index needed


# ── Incremental index (watermark makes weight aggregation idempotent) ────────

class TestIncrementalIndex:
    def test_second_index_run_adds_nothing(self, conn, monkeypatch, tmp_path):
        monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "facts.db")
        _insert_fact(conn, 1, "drbd and linstor quorum", "storage")
        conn.commit()
        kg.cmd_kg(["index"])
        w1 = sqlite3.connect(str(tmp_path / "facts.db")).execute(
            "SELECT COALESCE(SUM(weight),0) FROM triples").fetchone()[0]
        kg.cmd_kg(["index"])  # same facts again — watermark must skip them
        w2 = sqlite3.connect(str(tmp_path / "facts.db")).execute(
            "SELECT COALESCE(SUM(weight),0) FROM triples").fetchone()[0]
        assert w1 == w2 > 0


# ── export-links ─────────────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ["feedback", "troubleshooting", "domain", "handoffs"]:
        (root / d).mkdir(parents=True)
    (root / "feedback" / "f1.md").write_text(
        "---\nname: drbd lesson\n---\ndrbd and linstor quorum rules; etcd flannel interplay\n")
    (root / "troubleshooting" / "t1.md").write_text("# DRBD\ndrbd linstor split-brain recovery\n")
    (root / "domain" / "storage.md").write_text("# Storage\netcd flannel notes\n")
    # filler files raise N so shared entities stay under the document-frequency cap
    for i, svc in enumerate(["nginx", "redis", "kafka", "mimir"]):
        (root / "feedback" / f"filler{i}.md").write_text(f"note about {svc}\n")
    (root / "handoffs" / "h1.md").write_text("drbd linstor handoff\n")
    return root


class TestExportLinks:
    def test_writes_derived_footers(self, vault):
        cmd_kg_export_links(["--root", str(vault)])
        f1 = (vault / "feedback" / "f1.md").read_text()
        assert "<!-- gaius:related begin -->" in f1
        assert "[[t1]]" in f1
        assert "[[storage]]" in f1          # target-only dir receives inbound links
        assert (vault / "domain" / "storage.md").read_text().count("gaius:related") == 0
        assert "gaius:related" not in (vault / "handoffs" / "h1.md").read_text()

    def test_idempotent(self, vault):
        cmd_kg_export_links(["--root", str(vault)])
        first = (vault / "feedback" / "f1.md").read_text()
        cmd_kg_export_links(["--root", str(vault)])
        assert (vault / "feedback" / "f1.md").read_text() == first

    def test_stale_footer_removed_when_no_links_qualify(self, vault):
        lone = vault / "feedback" / "lonely.md"
        lone.write_text("nothing shared here\n\n<!-- gaius:related begin -->\n"
                        "**Related:** [[ghost]]\n<!-- gaius:related end -->\n")
        cmd_kg_export_links(["--root", str(vault)])
        assert "gaius:related" not in lone.read_text()

    def test_oversized_file_skipped(self, vault):
        big = vault / "feedback" / "big.md"
        big.write_text("drbd linstor etcd flannel\n" + "x" * 16000)
        before = big.read_text()
        cmd_kg_export_links(["--root", str(vault)])
        assert big.read_text() == before

    def test_weak_incident_entities_cannot_stand_alone(self, vault):
        # Two notes sharing ONLY an incident word (split-brain) must not link —
        # same failure class ≠ related subsystem. But incident + concrete
        # entity is legitimate evidence and must link.
        (vault / "feedback" / "only_incident_a.md").write_text("etcd hit split-brain today\n")
        (vault / "feedback" / "only_incident_b.md").write_text("mysql split-brain recovered\n")
        cmd_kg_export_links(["--root", str(vault)])
        a = (vault / "feedback" / "only_incident_a.md").read_text()
        assert "only_incident_b" not in a  # split-brain alone doesn't join etcd to mysql
        # t1 (drbd linstor split-brain) ↔ f1 (drbd linstor ...) share 2 strong
        # entities — still linked
        assert "[[t1]]" in (vault / "feedback" / "f1.md").read_text()
