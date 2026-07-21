"""Loop-2 automated gates — item 3: cap the repetition-derived confirmation boost in the ranker.

Env GAIUS_CONFIRMATION_BOOST_CAP (default unset = disabled). Under test on the pure helper
apply_confirmation_boost_cap (the exact code path cmd_inject calls):
  - default-off (cap=None) → score returned UNCHANGED (byte-identical)
  - flag-on → net repetition boost bounded to the cap
  - never penalizes an unboosted fact; a sub-1.0 cap is floored to 1.0
"""
from gaius.landscape import apply_confirmation_boost_cap as cap


# ── default-off: byte-identical ─────────────────────────────────────────────────

def test_default_off_is_identity():
    for score in (0.1, 0.5, 1.0, 3.7, 42.0):
        for rep_boost in (1.0, 1.2, 1.8, 5.0):
            assert cap(score, rep_boost, None) == score  # cap=None → untouched


def test_unboosted_never_touched_even_when_flag_on():
    # rep_boost == 1.0 means no repetition boost was applied → must never be reduced
    assert cap(0.5, 1.0, 1.2) == 0.5
    assert cap(0.5, 1.0, 0.3) == 0.5  # even a nonsensical sub-1.0 cap can't penalize it


# ── flag-on: bound the boost ────────────────────────────────────────────────────

def test_caps_stacked_boost():
    # score already carries rep_boost=1.8 (stored_q 1.2 × cross-agent 1.5); cap to 1.2
    # → scale back by 1.2/1.8. The pre-boost score was 1.0, so result == 1.2.
    assert cap(1.8, 1.8, 1.2) == 1.2


def test_boost_below_cap_untouched():
    # rep_boost 1.3 is under the 2.0 cap → no change
    assert cap(1.3, 1.3, 2.0) == 1.3


def test_sub_one_cap_floored_to_one():
    # a misconfigured cap < 1.0 is floored to 1.0: a boosted fact is brought back to its
    # unboosted value, never below it.
    # score=1.8 with rep_boost=1.8 → floored cap 1.0 → 1.8 * (1.0/1.8) == 1.0
    assert cap(1.8, 1.8, 0.5) == 1.0


def test_cap_equal_to_boost_is_noop():
    assert cap(1.5, 1.5, 1.5) == 1.5


def test_monotonic_reduction():
    # a higher rep_boost past the cap yields a stronger reduction of the raw pre-boost score
    base = 1.0
    r1, r2 = 1.5, 3.0
    out1 = cap(base * r1, r1, 1.2)
    out2 = cap(base * r2, r2, 1.2)
    assert out1 == 1.2 and out2 == 1.2  # both clamped to cap × base
