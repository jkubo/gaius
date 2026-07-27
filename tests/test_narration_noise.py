"""_is_noise() — anchored mid-reasoning narration + reviewer-verdict prose.

Gap 37: a session's own planning prose gets mined as a pending "fact", so one
session's reasoning becomes the next session's review queue (~7/day).

The load-bearing property under test is the ANCHOR, not the vocabulary: the
clause must begin inside the first _NARRATION_ANCHOR_CHARS characters. The loose
anywhere-in-text form matches ~24% of the live pending queue and swallows real
operational facts, so every "clause appears late" case below must stay False.

Pure-function only; no facts.db, no session files."""
import pytest

from gaius._core import _is_noise, _NARRATION_ANCHOR_CHARS


# ─────────────────────────────────────────────────────────────────────────────
# Anchored narration → noise. Samples are the real shapes measured in the queue.
# ─────────────────────────────────────────────────────────────────────────────

ANCHORED_NARRATION = [
    "Let me find out why the overlay interface is not coming up on that node.",
    "Let me dig into why the replicated volume lost quorum after the blip.",
    "Now let me verify the auth middleware is actually attached.",
    "Good — let me check the Role's resourceNames before merging the PR.",
    "OK, let's trace the request through the proxy and see where it 503s.",
    "I'll re-run the smoke test against staging before repointing the probes.",
    "I will check the etcd member list on all three control planes.",
    "I need to confirm the PVC storageClass before tolerating the taint.",
    "I'm going to grep the whole surface for that column before changing it.",
    "I am going to compare the Helm revision against the live DaemonSet.",
]


@pytest.mark.parametrize("text", ANCHORED_NARRATION)
def test_anchored_narration_is_noise(text):
    assert _is_noise(text) is True


def test_lead_in_still_anchored():
    """A short lead-in keeps the clause inside the bound — still narration."""
    text = "Right, that explains it. Let me check the other two nodes."
    assert text.lower().index("let me") < _NARRATION_ANCHOR_CHARS
    assert _is_noise(text) is True


# ─────────────────────────────────────────────────────────────────────────────
# The anchor. A planning clause that starts PAST the bound is prose wrapped
# around a real fact — these are the cases the loose form destroyed.
# ─────────────────────────────────────────────────────────────────────────────

def test_late_clause_carrying_a_real_fact_is_kept():
    text = (
        "The backup CronJob runs at 02:00 UTC but the cluster's timezone is "
        "America/Chicago, so the window it reports is six hours off. "
        "Let me confirm that against the last run."
    )
    assert text.index("Let me") > _NARRATION_ANCHOR_CHARS
    assert _is_noise(text) is False


def test_late_clause_second_sample_is_kept():
    text = (
        "The divergence job is failing at the clone step, not at the "
        "diff — the token has no read access to that remote. I'll open an issue."
    )
    assert _is_noise(text) is False


def test_clause_exactly_at_the_bound_is_kept():
    """Strictly `< _NARRATION_ANCHOR_CHARS`; the boundary itself is not noise."""
    pad = "x" * (_NARRATION_ANCHOR_CHARS - 1) + " "   # clause starts at exactly N
    assert len(pad) == _NARRATION_ANCHOR_CHARS
    assert _is_noise(pad + "let me check the node") is False


def test_clause_one_char_inside_the_bound_is_noise():
    pad = "x" * (_NARRATION_ANCHOR_CHARS - 2) + " "   # clause starts at N-1
    assert _is_noise(pad + "let me check the node") is True


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary false positives
# ─────────────────────────────────────────────────────────────────────────────

def test_lets_encrypt_is_not_narration():
    """"Let's Encrypt" is a cert issuer and leads real TLS facts."""
    text = "Let's Encrypt staging issuer is rate-limited to 5 duplicate certs/week."
    assert _is_noise(text) is False


def test_lets_encrypt_mid_sentence_is_not_narration():
    text = "cert-manager uses Let's Encrypt DNS-01 via the Cloudflare solver."
    assert _is_noise(text) is False


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer-of-record verdict prose — meta, never ops
# ─────────────────────────────────────────────────────────────────────────────

REVIEWER_VERDICTS = [
    "Fact 20174 = duplicate of an already-durable entry. Reject.",
    "Fact 20181 = narration from a prior session, Reject via gaius reject.",
    "Fact 19022 = still useful but unverified — Keep via agent-review.",
    "Fact 18740 = cannot confirm from live state, Defer for 7 days.",
]


@pytest.mark.parametrize("text", REVIEWER_VERDICTS)
def test_reviewer_verdict_is_noise(text):
    assert _is_noise(text) is True


def test_fact_reference_without_a_verdict_is_kept():
    """A fact that merely cites a fact id is not a review verdict."""
    text = "Fact 20174 = the single-replica pin, which the multi-zone HA change superseded."
    assert _is_noise(text) is False


def test_verdict_word_far_from_the_fact_ref_is_kept():
    """A mid-sentence "reject" is ordinary English, not a review verdict.

    This is the precision control for the authored-verdict rule: the fact-ref
    opens the text, so only the TERMINAL-token requirement keeps this out.
    """
    text = (
        "Fact 20174 = the queue-depth alert rule, whose expression compares "
        "equality where the evaluator expects a threshold. The operator may "
        "reject the compensating-control claim."
    )
    assert _is_noise(text) is False


# ── Authored verdicts: assessment clause runs long, verdict lands at the end ──
# These reproduce the SHAPE measured in a real review queue: a long assessment
# clause with the verdict as the final token. The original 120-char window caught
# NONE of them; they are the recall case. Content is synthetic on purpose — these
# fixtures ship publicly, so they must not carry operational detail.

LONG_AUTHORED_VERDICTS = [
    "Fact 953 = valuable cascade-failure causal chain (scheduler OOM → node "
    "reboot → link saturation → consensus read stall → API timeouts). "
    "Reusable diagnostic pattern. Keep via agent-review.",
    "Fact 3826 = resolved instance-incident (database pod affinity label "
    "fix, long since applied). Stale resolved config fix, not a "
    "reusable procedure. Reject.",
    "Fact 20083 = mid-investigation prose fragment from another active session. "
    "Owned by that active session; the durable version homes there. Reject prose.",
    "**Fact 891 = reusable log-index corruption fix (object-store side: "
    "batch-delete the corrupted table prefix). Actionable, distinct "
    "from the local-cache variant. Keep via agent-review.**",
]


@pytest.mark.parametrize("text", LONG_AUTHORED_VERDICTS)
def test_long_authored_verdict_is_noise(text):
    assert _is_noise(text) is True


def test_verdict_term_without_the_anchored_opener_is_kept():
    """The review term alone is not enough — the opener is the precision anchor.

    A real fact may legitimately discuss the agent-review verb.
    """
    text = (
        "gaius review verbs: agent-review marks a pending fact machine-reviewed "
        "and leaves confidence untouched; reject is the only removal verb."
    )
    assert _is_noise(text) is False


def test_long_assessment_without_a_verdict_term_is_kept():
    """Under-catch is the intended failure mode when no verdict term appears."""
    text = (
        "Fact 20031 = truncated mid-reasoning prose fragment from a feature "
        "branch, and its intermediate conclusion was later corrected by that "
        "same session — the default branch already carries the fix."
    )
    assert _is_noise(text) is False


# ─────────────────────────────────────────────────────────────────────────────
# Ordinary operational facts must survive untouched
# ─────────────────────────────────────────────────────────────────────────────

ORDINARY_FACTS = [
    "Overlay MTU is 1400 — the underlay path MTU is 1450 and the encapsulation "
    "adds 50 bytes.",
    "containerStatuses[0] is the ALPHABETICALLY-first container, not spec order, "
    "so the readiness gate is a constant true.",
    "NEVER kubectl rollout restart a CNI DaemonSet — it kills pod networking on "
    "every node simultaneously.",
    "The deploy job only diffs the range since the last applied commit, so a "
    "revert commit is a silent no-op.",
    "The cache warmer holds a global lock for the whole rebuild, so one slow "
    "rebuild stalls every reader.",
]


@pytest.mark.parametrize("text", ORDINARY_FACTS)
def test_ordinary_facts_are_not_noise(text):
    assert _is_noise(text) is False


def test_empty_and_short_input():
    assert _is_noise("") is False
    assert _is_noise("ok") is False
