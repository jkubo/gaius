"""fact_type volatility axis (2026-07-03) — volatility_recency precedence + rates."""
import math

from gaius._core import volatility_recency


RATE = 0.02  # cmd_decay default


def test_structural_never_decays():
    assert volatility_recency("automated", "structural", 365.0, RATE) == 1.0


def test_no_decay_provenance_still_exempt():
    assert volatility_recency("finding", "operational", 200.0, RATE) == 1.0


def test_operational_default_rate_unchanged():
    # NULL/None fact_type behaves like operational (pre-migration rows)
    for ft in ("operational", None, ""):
        assert volatility_recency("automated", ft, 30.0, RATE) == math.exp(-RATE * 30.0)


def test_live_decays_three_times_faster():
    live = volatility_recency("automated", "live", 30.0, RATE)
    assert live == math.exp(-RATE * 3.0 * 30.0)
    assert live < volatility_recency("automated", "operational", 30.0, RATE)


def test_explicit_live_wins_over_no_decay_provenance():
    # A deliberately-rated state snapshot must decay even under a durable provenance.
    assert volatility_recency("finding", "live", 100.0, RATE) < 1.0
