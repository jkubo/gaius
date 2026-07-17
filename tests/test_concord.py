"""Tests for gaius.concord — claim-overlap advisory + the core claim invariant it must
not weaken. Added 2026-07-17 with the overlap-warning feature (the P0 shipped with no
direct concord unit tests). Born from a real session collision: subsystem:linstor-drbd
vs subsystem:ansible-linstor-registration never collided on the exact-match UNIQUE index."""
import gaius.concord as cc


# ── _significant_tokens ──────────────────────────────────────────────────────────────

def test_significant_tokens_drops_prefix_keeps_meaningful():
    assert cc._significant_tokens("subsystem:ansible-linstor-registration") == {
        "ansible", "linstor", "registration"}


def test_significant_tokens_excludes_short_site_hw_tokens():
    # node keys are site/hardware tokens (<4 chars) → no fuzzy signal (exact-match only)
    assert cc._significant_tokens("node:aus-fwd-gpu-02") == set()
    assert cc._significant_tokens("node:lax-fwd-gpu-01") == set()


def test_significant_tokens_excludes_generic_stopwords():
    assert cc._significant_tokens("subsystem:drbd-recovery-decision") == {"drbd"}


# ── _overlapping_claims ──────────────────────────────────────────────────────────────

def test_overlap_detects_shared_token():
    active = [{"resource": "subsystem:linstor-drbd", "session_id": "A", "holder": "sessA"}]
    ov = cc._overlapping_claims(active, "subsystem:ansible-linstor-registration", "B")
    assert len(ov) == 1
    assert ov[0]["resource"] == "subsystem:linstor-drbd"
    assert ov[0]["shared"] == ["linstor"]


def test_overlap_no_false_positive_on_unrelated():
    active = [{"resource": "node:aus-fwd-gpu-02", "session_id": "A"}]
    assert cc._overlapping_claims(active, "subsystem:storage", "B") == []


def test_overlap_excludes_own_session_and_exact_resource():
    active = [{"resource": "subsystem:linstor-drbd", "session_id": "A"},
              {"resource": "subsystem:linstor-x", "session_id": "A"}]
    # requesting the exact resource, held by our own session A → nothing to warn about
    assert cc._overlapping_claims(active, "subsystem:linstor-drbd", "A") == []


def test_overlap_empty_when_requester_has_no_signal():
    active = [{"resource": "subsystem:linstor-drbd", "session_id": "A"}]
    assert cc._overlapping_claims(active, "node:lax-fwd-gpu-01", "B") == []


# ── integration against a real (temp) DB ─────────────────────────────────────────────

def test_claim_overlap_integration(tmp_path):
    conn = cc.init_concord(str(tmp_path / "c.db"))
    won, _ = cc._try_claim(conn, "subsystem:linstor-drbd", "A", 0, "sessA", "", 3600)
    assert won
    won, _ = cc._try_claim(conn, "subsystem:ansible-linstor-registration", "B", 0, "sessB", "", 3600)
    assert won  # different name → wins the atomic claim
    ov = cc._overlapping_claims(
        cc._active_claims(conn), "subsystem:ansible-linstor-registration", "B")
    assert any(o["resource"] == "subsystem:linstor-drbd" and o["shared"] == ["linstor"]
               for o in ov)


def test_exact_match_still_blocks(tmp_path):
    """INVARIANT: the advisory overlap feature must NOT weaken the atomic single-winner
    UNIQUE index. Two sessions on the SAME resource → exactly one winner."""
    conn = cc.init_concord(str(tmp_path / "c.db"))
    won1, _ = cc._try_claim(conn, "subsystem:drbd", "A", 0, "sessA", "", 3600)
    won2, holder = cc._try_claim(conn, "subsystem:drbd", "B", 0, "sessB", "", 3600)
    assert won1 is True
    assert won2 is False
    assert holder["session_id"] == "A"


# ── claim lifecycle: renewal, TTL, dead-holder, real concurrency (added with OSS
#    inclusion, 2026-07-17 — same-day complement to the overlap tests above) ──────────

def test_reclaim_renews_for_same_session(tmp_path):
    conn = cc.init_concord(str(tmp_path / "c.db"))
    cc._try_claim(conn, "subsystem:db", "A", 0, "a", "first", 3600)
    won, _ = cc._try_claim(conn, "subsystem:db", "A", 0, "a", "renewed", 3600)
    assert won
    active = cc._active_claims(conn)
    assert len(active) == 1 and active[0]["note"] == "renewed"


def test_expired_ttl_is_reclaimable(tmp_path):
    # NB: ttl_sec=0 means "no expiry" (falsy skips the TTL check) — so expire a real
    # ttl by backdating the claim instead of sleeping.
    conn = cc.init_concord(str(tmp_path / "c.db"))
    cc._try_claim(conn, "subsystem:db", "A", 0, "a", "", 1)
    conn.execute("UPDATE claims SET created_at='2026-01-01T00:00:00.000000Z' "
                 "WHERE resource='subsystem:db' AND released_at IS NULL")
    conn.commit()
    won, _ = cc._try_claim(conn, "subsystem:db", "B", 0, "b", "", 3600)
    assert won


def test_dead_holder_is_reclaimable(tmp_path):
    conn = cc.init_concord(str(tmp_path / "c.db"))
    cc._try_claim(conn, "subsystem:db", "ghost", 999999, "ghost", "", 999999)
    won, _ = cc._try_claim(conn, "subsystem:db", "B", 0, "b", "", 3600)
    assert won


def test_concurrent_claims_exactly_one_winner(tmp_path):
    import threading
    db = str(tmp_path / "race.db")
    cc.init_concord(db).close()
    wins = []

    def worker(i):
        conn = cc.init_concord(db)  # one connection per thread
        won, _ = cc._try_claim(conn, "race:lock", f"sess-{i}", 0, str(i), "", 3600)
        if won:
            wins.append(i)
        conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


# ── pool: atomic take + dead-taker reap ──────────────────────────────────────────────

def test_pool_take_single_winner_and_dead_reap(tmp_path):
    conn = cc.init_concord(str(tmp_path / "c.db"))
    conn.execute("INSERT INTO pool_tasks (title, status, created_at) VALUES ('t1','open',?)",
                 (cc._utcnow(),))
    conn.commit()
    cur = conn.execute(
        "UPDATE pool_tasks SET status='taken', taken_by='a', taken_pid=999999, taken_at=? "
        "WHERE id=1 AND status='open'", (cc._utcnow(),))
    assert cur.rowcount == 1
    cur2 = conn.execute(
        "UPDATE pool_tasks SET status='taken', taken_by='b', taken_at=? "
        "WHERE id=1 AND status='open'", (cc._utcnow(),))
    assert cur2.rowcount == 0  # second take loses
    conn.commit()
    assert cc._reap_pool(conn) == 1  # dead taker → task returns to the pool
    assert conn.execute("SELECT status FROM pool_tasks WHERE id=1").fetchone()[0] == "open"


# ── prompt-delta cursor: deliver-once, own-exclusion, steal surfacing ────────────────

def _brief(db, scope, session, capsys):
    import argparse
    cc._concord_brief(argparse.Namespace(db=db, scope=scope, session=session))
    return capsys.readouterr().out


def test_prompt_delta_delivers_once(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    cc.init_concord(db).close()
    assert _brief(db, "prompt", "viewer", capsys) == ""  # first call: init cursor silently
    conn = cc.init_concord(db)
    conn.execute(
        "INSERT INTO findings (id, session_id, summary, severity, status, created_at, updated_at)"
        " VALUES ('f-1','sib','replica lag is the root cause','major','open',?,?)",
        (cc._utcnow(), cc._utcnow()))
    conn.commit()
    conn.close()
    # same-second publish must still surface (microsecond cursor), and exactly once
    assert "replica lag" in _brief(db, "prompt", "viewer", capsys)
    assert _brief(db, "prompt", "viewer", capsys) == ""


def test_own_findings_not_echoed_back(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    cc.init_concord(db).close()
    _brief(db, "prompt", "me", capsys)
    conn = cc.init_concord(db)
    conn.execute(
        "INSERT INTO findings (id, session_id, summary, severity, status, created_at, updated_at)"
        " VALUES ('f-2','me','my own discovery','info','open',?,?)",
        (cc._utcnow(), cc._utcnow()))
    conn.commit()
    conn.close()
    assert _brief(db, "prompt", "me", capsys) == ""


def test_steal_surfaces_in_victims_delta(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:db", "victim", 0, "victim", "", 3600)
    conn.close()
    _brief(db, "prompt", "victim", capsys)  # init cursor
    conn = cc.init_concord(db)
    conn.execute(
        "UPDATE claims SET released_at=?, released_reason='stolen by thief' "
        "WHERE resource='subsystem:db' AND released_at IS NULL", (cc._utcnow(),))
    conn.commit()
    conn.close()
    assert "taken over" in _brief(db, "prompt", "victim", capsys)
