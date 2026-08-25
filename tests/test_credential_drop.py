"""Credential material must be a DROP signal, never a PROMOTION signal.

Context (2026-08-14 audit). `extract.FINDING_PATTERNS` carried a raw
credential shape (`ghp_[A-Za-z0-9]`). Because `classify_finding()` returns
`max(score, FINDING_BASE_SCORE)` = 0.85 against a `MINE_SCORE_THRESHOLD` of
0.50, a credential-bearing block was not merely retained — it was *guaranteed*
to clear the staging bar, then stored verbatim at `[:300]` into facts.db
(mode 0644, rclone'd to SeaweedFS by both gaius-session-stop and
gaius-nightly-sync). It was the one site in the corpus pipeline where a
credential shape made text *more* likely to be kept.

The pre-existing guard, `CREDENTIAL_PATTERNS`, is five literal `key=`
substrings and is applied on only two of the parser paths — not on either
`classify_finding()` site. So vendor-issued shapes (`ghp_`, `sk-ant-`,
`AKIA`, `hvs.`, `tskey-`) passed through untouched.

These tests pin BOTH halves:
  MUST_DROP  — credential *material* never reaches a mined section.
  MUST_KEEP  — prose *about* a leak is still promoted, and shape-identical
               non-secrets (git SHAs, image digests) are not destroyed. That
               false-positive direction is not hypothetical: the prior
               iteration of this regex ate full 40-char git SHAs, which are
               indistinguishable from a Forgejo PAT by shape alone.
"""
import json

import pytest

from gaius.extract import (
    FINDING_PATTERNS,
    classify_finding,
    has_credential,
)
from gaius.retire import _mine_session

PAD = " Recording the surrounding diagnosis so the block clears MINE_MIN_TEXT_LEN and would otherwise be staged as high signal for future sessions of every agent on this cluster."

# Credential MATERIAL — vendor-issued shapes that cannot be anything else.
#
# Every value here is fabricated. They are written as prefix + body rather than as
# one literal so that hosted secret scanners do not read the fixture as a live
# credential and block the push — the concatenation happens at compile time, so
# has_credential() sees exactly the same string either way. Keep new entries in
# this form; a single literal will get a push rejected.
MUST_DROP = [
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "github_pat_" + "11ABCDEFG0123456789_" + "a" * 50,
    "sk-ant-api03-" + "x9Y8z7W6v5U4t3S2r1Q0p9O8n7M6l5K4j3I2h1G0f9E8d7C6b5A4",
    "sk-" + "proj0123456789abcdefghijklmnopqrstuvwxyz0123",
    "xoxb-" + "1234567890-0987654321-AbCdEfGhIjKlMnOpQrSt",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIA0123456789ABCDEF",
    "glpat-" + "xyz123ABC456def789",
    "hvs." + "CAESIJx9k2mQpR7tYvW1nB4sD6fG8hJ0kL2mN4pQ6rS8tU0v",
    "tskey-auth-" + "kFGiAS7CNTRL-2dpMswTvBqxfCA8xyz",
    "whsec_" + "0123456789abcdef0123456789abcdef",
    "sk_live_" + "0123456789abcdefghijklmn",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "$ANSIBLE_VAULT;1.1;AES256",
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IkFCQ0RFRkcifQ.eyJhdWQiOlsiazhzIl19abc",
    "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX",
    "postgres://svcuser:hunter2correcthorse@db.example.com:5432/appdb",
    "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7",
    "K10" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2",
]

# Prose ABOUT a leak (the valuable lesson) and shape-alikes that are NOT secrets.
MUST_KEEP = [
    "The vault password was exposed in kubectl describe on the finint pod, which is how we found it.",
    "Root cause: the forgejo token was visible in kubectl describe because it sat in plaintext in pod args.",
    "We rotated the credential and revoked the old one after the incident review.",
    # 40-char git SHA — shape-identical to a Forgejo PAT.
    "The regression landed in commit 7d8e096cb4dda1f2e3b5c6a7980d1e2f3a4b5c6d and was reverted.",
    # sha256 image digest.
    "Pinned the image to sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 to stop the roll.",
    # A UUID.
    "Session 04468458-95cc-41f2-ba1b-463256f24c11 showed the CronJob pod answering as a live endpoint.",
]


@pytest.mark.parametrize("secret", MUST_DROP)
def test_has_credential_catches_material(secret):
    assert has_credential(secret + PAD) is True, f"missed credential shape: {secret[:12]}…"


@pytest.mark.parametrize("prose", MUST_KEEP)
def test_has_credential_spares_prose_and_shape_alikes(prose):
    assert has_credential(prose) is False, f"false positive on: {prose[:60]}…"


def test_finding_patterns_carry_no_credential_shape():
    """No FINDING_PATTERNS entry may match raw credential material.

    The whole defect was a *shape* living in a promotion list. Prose
    descriptors ('exposed in', 'plaintext in pod args') are the valuable
    finding and stay; they carry no material.
    """
    probe = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    import re
    offenders = [p for p in FINDING_PATTERNS if re.search(p, probe, re.IGNORECASE)]
    assert offenders == [], f"credential shape still promotes: {offenders}"


def test_classify_finding_does_not_promote_credential_material():
    text = "upload rejected, used token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + PAD
    etype, score = classify_finding(text, "assistant_reasoning", 0.30)
    assert etype != "finding", "credential material was upgraded to a finding"
    assert score < 0.85, f"credential material boosted to {score}"


def test_classify_finding_still_promotes_leak_prose():
    """Guard the other direction: the lesson must survive the fix."""
    text = "The forgejo token was visible in kubectl describe" + PAD
    etype, score = classify_finding(text, "assistant_reasoning", 0.30)
    assert etype == "finding"
    assert score >= 0.85


def _session(tmp_path, blocks, kind="assistant", name="sess"):
    """Build a session JSONL exercising ONE of _mine_session's three intake paths.

    All three feed `sections`, but only the assistant path is scored — so a
    guard placed solely at the classify_finding() call site would leave the
    other two wide open. tool_result is the highest-risk of the three: error
    output is exactly where a rejected-auth traceback pastes a live token.
    """
    p = tmp_path / f"{name}.jsonl"
    with open(p, "w") as fh:
        fh.write(json.dumps({
            "type": "user",
            "message": {"content": [{"type": "text",
                                     "text": "Investigate the failing upload on the runner please."}]},
        }) + "\n")
        # Keep a clean high-signal block so the session always clears the
        # "insufficient signal -> return None" bar; otherwise a passing test
        # could pass merely because nothing staged at all.
        fh.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text":
                "Root cause: the deploy failed because the manifest pinned the wrong tag." + PAD}]},
        }) + "\n")
        for b in blocks:
            if kind == "assistant":
                e = {"type": "assistant",
                     "message": {"content": [{"type": "text", "text": b}]}}
            elif kind == "tool_result":
                e = {"type": "tool_result", "content": b}
            else:
                e = {"type": "user",
                     "message": {"content": [{"type": "text", "text": b}]}}
            fh.write(json.dumps(e) + "\n")
    return p


@pytest.mark.parametrize("secret", MUST_DROP)
def test_mine_session_never_stages_credential_material(tmp_path, secret):
    """End-to-end: the material must not survive into any mined section."""
    block = f"The runner failed with an error. The token in play was {secret} and that is what broke it.{PAD}"
    sections = _mine_session(_session(tmp_path, [block]))
    blob = "" if sections is None else "\n".join(str(v) for v in sections.values())
    assert secret not in blob, f"credential material staged verbatim: {secret[:12]}…"


@pytest.mark.parametrize("kind", ["assistant", "tool_result", "user"])
def test_all_three_intake_paths_reject_credentials(tmp_path, kind):
    """_mine_session has three routes into sections; all three must be guarded.

    Only the assistant route is scored, so a guard at the classify_finding()
    call site alone is a partial control. tool_result carries error output —
    the likeliest carrier of a real pasted token.
    """
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    block = (f"Traceback (most recent call last): RuntimeError: upload rejected, "
             f"the token used was {secret} and the push failed.{PAD}")
    sections = _mine_session(_session(tmp_path, [block], kind=kind, name=f"s_{kind}"))
    blob = "" if sections is None else "\n".join(str(v) for v in sections.values())
    assert secret not in blob, f"{kind} path staged credential material verbatim"


def _archive_capture(tmp_path, monkeypatch, content, **kwargs):
    """Run archive_session with rclone stubbed; return (result, uploaded_bytes)."""
    import subprocess as _sp
    from gaius import _core, retire

    monkeypatch.setattr(_core, "_gaius_cfg", {"s3": {"remote": "stub", "prefix": "sessions"}})
    monkeypatch.setattr(_core, "PROJECT_DIR", tmp_path)
    src = tmp_path / "session.jsonl"
    src.write_text(content)

    seen = {}

    def fake_run(cmd, **kw):
        # cmd = [rclone, copyto, <upload_path>, <target>, ...]
        seen["bytes"] = open(cmd[2]).read()
        return _sp.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(retire.subprocess, "run", fake_run)
    result = retire.archive_session(src, **kwargs)
    return result, seen.get("bytes", "")


@pytest.mark.parametrize("secret", MUST_DROP[:8])
def test_archive_session_scrubs_before_upload(tmp_path, monkeypatch, secret):
    """The third S3 egress path must not ship credential material either."""
    body = json.dumps({"type": "assistant", "text": f"token {secret} failed"}) + "\n"
    result, uploaded = _archive_capture(tmp_path, monkeypatch, body)
    assert result is not None, "archive unexpectedly aborted"
    assert secret not in uploaded, "archive_session uploaded credential material"


def test_archive_session_scrubs_even_without_bloat_strip(tmp_path, monkeypatch):
    """Scrub is unconditional: callers disable strip for SIZE, not for safety."""
    secret = "sk-ant-api03-" + "x9Y8z7W6v5U4t3S2r1Q0p9O8n7M6l5K4j3I2h1G0f9E8d7C6b5A4"
    body = json.dumps({"type": "assistant", "text": f"key {secret} here"}) + "\n"
    _, uploaded = _archive_capture(tmp_path, monkeypatch, body, strip_before_archive=False)
    assert secret not in uploaded


def test_archive_session_fails_closed_not_raw(tmp_path, monkeypatch):
    """On a scrub/strip exception it must ABORT, never fall back to raw.

    The pre-2026-08-14 code printed 'Bloat strip failed, archiving raw' and
    uploaded the untouched original — a documented path for shipping
    credentials to S3. Regression guard for exactly that fallback.
    """
    from gaius import retire

    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    body = json.dumps({"type": "assistant", "text": f"token {secret}"}) + "\n"
    monkeypatch.setattr(retire, "strip_bloat",
                        lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    result, uploaded = _archive_capture(tmp_path, monkeypatch, body)
    assert result is None, "archive_session returned a target despite a failed scrub"
    assert uploaded == "", "archive_session uploaded something after a failed scrub"


def test_mine_session_still_stages_the_lesson(tmp_path):
    """The fix must not gut mining: a real finding with no material still stages."""
    block = ("Root cause: the deploy failed because the credential was visible in "
             "kubectl describe, which is a plaintext in pod args problem." + PAD)
    sections = _mine_session(_session(tmp_path, [block]))
    assert sections is not None, "mining returned nothing for a legitimate finding"
    blob = "\n".join(str(v) for v in sections.values())
    assert "kubectl describe" in blob
