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
    assert cc._significant_tokens("node:r1-web-gpu-02") == set()
    assert cc._significant_tokens("node:r2-web-gpu-01") == set()


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
    active = [{"resource": "node:r1-web-gpu-02", "session_id": "A"}]
    assert cc._overlapping_claims(active, "subsystem:storage", "B") == []


def test_overlap_excludes_own_session_and_exact_resource():
    active = [{"resource": "subsystem:linstor-drbd", "session_id": "A"},
              {"resource": "subsystem:linstor-x", "session_id": "A"}]
    # requesting the exact resource, held by our own session A → nothing to warn about
    assert cc._overlapping_claims(active, "subsystem:linstor-drbd", "A") == []


def test_overlap_empty_when_requester_has_no_signal():
    active = [{"resource": "subsystem:linstor-drbd", "session_id": "A"}]
    assert cc._overlapping_claims(active, "node:r2-web-gpu-01", "B") == []


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


# ── baton pass: handoff transfers this session's claims → pool, releases as handed-off ─
#    (--no-handoff on every test → no real handoff file is written / pruned as a side effect)

def _run_handoff(db, monkeypatch, sid="baton-sess", next_steps="", body="", as_json=True,
                 spawn=False):
    import argparse
    import io
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))  # deterministic; never blocks/raises
    cc._concord_handoff(argparse.Namespace(
        db=db, skill="", next=next_steps, severity="normal",
        no_handoff=True, no_title=True, spawn=spawn, json=as_json))


def test_handoff_transfers_claims_to_pool_and_releases(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:baton-demo", "baton-sess", 0, "", "mid-repair", 3600)
    cc._try_claim(conn, "node:demo-01", "baton-sess", 0, "", "", 3600)
    conn.close()

    _run_handoff(db, monkeypatch, next_steps="do X")
    capsys.readouterr()

    conn = cc.init_concord(db)
    assert cc._active_claims(conn) == []  # all mine released
    reasons = {r[0]: r[1] for r in conn.execute(
        "SELECT resource, released_reason FROM claims WHERE released_at IS NOT NULL")}
    assert reasons == {"subsystem:baton-demo": "handed-off", "node:demo-01": "handed-off"}
    tasks = conn.execute("SELECT title, resource, status FROM pool_tasks ORDER BY id").fetchall()
    assert tasks == [("baton: subsystem:baton-demo", "subsystem:baton-demo", "open"),
                     ("baton: node:demo-01", "node:demo-01", "open")]
    conn.close()


def test_handoff_released_resource_is_reclaimable(tmp_path, monkeypatch):
    """INVARIANT: handing off a claim frees the resource — a successor can re-claim it
    (the partial-unique index only guards rows with released_at IS NULL)."""
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:db", "baton-sess", 0, "", "", 3600)
    conn.close()
    _run_handoff(db, monkeypatch)
    conn = cc.init_concord(db)
    won, _ = cc._try_claim(conn, "subsystem:db", "successor", 0, "succ", "", 3600)
    assert won
    conn.close()


def test_handoff_only_releases_own_session_claims(tmp_path, monkeypatch):
    """A baton pass must NEVER release a sibling session's claim."""
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:mine", "baton-sess", 0, "", "", 3600)
    cc._try_claim(conn, "subsystem:sibling", "other-sess", 0, "", "", 3600)
    conn.close()
    _run_handoff(db, monkeypatch)
    conn = cc.init_concord(db)
    active = {c["resource"]: c["session_id"] for c in cc._active_claims(conn)}
    assert active == {"subsystem:sibling": "other-sess"}  # sibling untouched, mine handed off
    conn.close()


def test_handoff_claimless_seeds_one_task(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "c.db")
    cc.init_concord(db).close()
    _run_handoff(db, monkeypatch, next_steps="pick up altdata scoring")
    capsys.readouterr()
    conn = cc.init_concord(db)
    tasks = conn.execute("SELECT resource, status FROM pool_tasks").fetchall()
    assert tasks == [("", "open")]
    conn.close()


def test_handoff_noop_when_nothing_to_hand_off(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "c.db")
    cc.init_concord(db).close()
    _run_handoff(db, monkeypatch, as_json=False)  # no claims, no --next, empty body
    assert "nothing to hand off" in capsys.readouterr().out
    conn = cc.init_concord(db)
    assert conn.execute("SELECT COUNT(*) FROM pool_tasks").fetchone()[0] == 0
    conn.close()


def _run_handoff_writer_spy(db, monkeypatch, sid="baton-sess", next_steps="", body="",
                            no_handoff=False, ret="/tmp/fake-handoff.md"):
    """Run handoff with the file writer stubbed to a spy — records calls, never touches disk."""
    import argparse
    import io
    calls = []
    monkeypatch.setattr(cc, "_write_handoff", lambda *a, **k: (calls.append(a), ret)[1])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    cc._concord_handoff(argparse.Namespace(
        db=db, skill="", next=next_steps, severity="normal",
        no_handoff=no_handoff, no_title=True, spawn=False, json=True))
    return calls


def test_handoff_noop_does_not_invoke_the_writer(tmp_path, monkeypatch, capsys):
    """Regression (verifier #2): a bare handoff with nothing to hand off must NOT call the
    writer — the writer ALWAYS writes a file + prunes, so an accidental bare run would litter
    the handoffs dir and evict real handoffs. no_handoff=False so the writer WOULD run if reached."""
    db = str(tmp_path / "c.db")
    cc.init_concord(db).close()
    calls = _run_handoff_writer_spy(db, monkeypatch)  # no claims, no next, no body
    capsys.readouterr()
    assert calls == []


def test_handoff_appends_path_after_commit(tmp_path, monkeypatch, capsys):
    """Regression (verifier #3): with real work the writer IS invoked, and its path is appended
    to the seeded baton detail AFTER the claim→pool transfer has committed."""
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:x", "baton-sess", 0, "", "", 3600)
    conn.close()
    calls = _run_handoff_writer_spy(db, monkeypatch, ret="/tmp/fake-handoff.md")
    capsys.readouterr()
    assert len(calls) == 1  # writer invoked exactly once
    conn = cc.init_concord(db)
    assert cc._active_claims(conn) == []  # claim released (committed before the file write)
    detail = conn.execute("SELECT detail FROM pool_tasks WHERE id=1").fetchone()[0]
    assert "handoff: /tmp/fake-handoff.md" in detail
    conn.close()


# ── baton --spawn: launch an inert `claude --bg --permission-mode plan` successor ──────
#    (spawn exec + /dev/tty confirm are always monkeypatched — a unit test never spawns a
#    real session or reads a terminal; the mirror of gaius-baton-watch's step-7 primitive.)

def test_spawn_builds_bg_plan_command():
    """The launch is a plan-mode --bg session hydrated via --append-system-prompt-file — never
    skip-perms, and the opening prompt is the compact baton loader (fresh-floor successor)."""
    name, cmd = cc._build_spawn_cmd("fable", "abcd1234", "/tmp/h.md")
    assert name == "baton:fable:abcd1234"
    assert cmd[cmd.index("--name") + 1] == "baton:fable:abcd1234"
    assert "--bg" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == "/tmp/h.md"
    assert cmd[-1] == cc._LOAD_BATON_PROMPT
    assert "--dangerously-skip-permissions" not in cmd  # plan mode, NOT skip-perms
    assert "--model" not in cmd and "--effort" not in cmd  # omitted when unset


def test_bg_id_regex_captures_backgrounded_line():
    assert cc._BG_ID_RE.search("backgrounded · abcd1234 · baton:fable:x").group(1) == "abcd1234"
    assert cc._BG_ID_RE.search("no id in this line") is None


def test_spawn_successor_skipped_without_handoff():
    """No handoff file → nothing to hydrate a plan successor → spawn skipped (never exec'd)."""
    r = cc._spawn_successor("fable", "baton-sess", "", "/tmp")
    assert r["spawned"] is False and "no handoff file" in r["reason"]


def test_spawn_successor_declined_does_not_exec(monkeypatch):
    """prompt-before-spawn: a 'no' at the /dev/tty prompt must NOT exec claude --bg."""
    ran = []
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: False)
    monkeypatch.setattr(cc, "_run_spawn", lambda *a, **k: ran.append(a) or "id")
    monkeypatch.setattr(cc, "_stage_spawn_launch", lambda *a, **k: "/tmp/x.launch")
    r = cc._spawn_successor("fable", "baton-sess", "/tmp/h.md", "/tmp")
    assert ran == []  # spawn never exec'd
    assert r["spawned"] is False and "declined" in r["reason"]
    assert r["launch"] == "/tmp/x.launch"


def test_spawn_successor_headless_fails_closed(monkeypatch):
    """No controlling terminal (_confirm_tty → None) → fail closed: no spawn, stage the launch."""
    ran = []
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: None)
    monkeypatch.setattr(cc, "_run_spawn", lambda *a, **k: ran.append(a) or "id")
    monkeypatch.setattr(cc, "_stage_spawn_launch", lambda *a, **k: "/tmp/x.launch")
    r = cc._spawn_successor("fable", "baton-sess", "/tmp/h.md", "/tmp")
    assert ran == []
    assert r["spawned"] is False and "headless" in r["reason"]


def test_spawn_successor_confirmed_returns_id(monkeypatch):
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: True)
    monkeypatch.setattr(cc, "_run_spawn", lambda name, cmd, cwd: "abcd1234")
    r = cc._spawn_successor("fable", "baton-sess", "/tmp/h.md", "/tmp")
    assert r == {"spawned": True, "name": "baton:fable:baton-se", "id": "abcd1234"}


def test_spawn_successor_spawn_failure_stages_launch(monkeypatch):
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: True)
    monkeypatch.setattr(cc, "_run_spawn", lambda *a, **k: None)  # --bg failed
    monkeypatch.setattr(cc, "_stage_spawn_launch", lambda *a, **k: "/tmp/x.launch")
    r = cc._spawn_successor("fable", "baton-sess", "/tmp/h.md", "/tmp")
    assert r["spawned"] is False and "spawn failed" in r["reason"]
    assert r["launch"] == "/tmp/x.launch"


def test_stage_spawn_launch_writes_paste_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() → tmp on POSIX
    p = cc._stage_spawn_launch("sid123", "baton:fable:sid123", "/tmp/h.md")
    assert p and p.endswith("sid123.launch")
    txt = open(p).read()
    assert "--permission-mode plan" in txt
    assert "--append-system-prompt-file" in txt and "/tmp/h.md" in txt
    assert "--dangerously-skip-permissions" not in txt


def test_handoff_spawn_launches_when_confirmed(tmp_path, monkeypatch, capsys):
    """End-to-end: --spawn wires the prepared handoff path into a --bg plan spawn on confirm."""
    import argparse
    import io
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:x", "baton-sess", 0, "", "", 3600)
    conn.close()
    monkeypatch.setattr(cc, "_write_handoff", lambda *a, **k: "/tmp/fake-handoff.md")
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: True)
    seen = {}
    monkeypatch.setattr(cc, "_run_spawn",
                        lambda name, cmd, cwd: seen.update(name=name, cmd=cmd) or "abcd1234")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "baton-sess")
    monkeypatch.setattr("sys.stdin", io.StringIO("real body"))
    cc._concord_handoff(argparse.Namespace(
        db=db, skill="fable", next="", severity="normal",
        no_handoff=False, no_title=True, spawn=True, json=False))
    out = capsys.readouterr().out
    assert seen["name"] == "baton:fable:baton-se"
    assert "--permission-mode" in seen["cmd"] and "plan" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--append-system-prompt-file") + 1] == "/tmp/fake-handoff.md"
    assert "successor spawned" in out and "abcd1234" in out


def test_handoff_without_spawn_flag_never_launches(tmp_path, monkeypatch):
    """Regression: default handoff (no --spawn) must not touch the spawn path at all."""
    import argparse
    import io
    ran = []
    monkeypatch.setattr(cc, "_run_spawn", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cc, "_confirm_tty", lambda prompt: ran.append("tty"))
    db = str(tmp_path / "c.db")
    conn = cc.init_concord(db)
    cc._try_claim(conn, "subsystem:x", "baton-sess", 0, "", "", 3600)
    conn.close()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "baton-sess")
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    cc._concord_handoff(argparse.Namespace(
        db=db, skill="", next="", severity="normal",
        no_handoff=True, no_title=True, spawn=False, json=True))
    assert ran == []
