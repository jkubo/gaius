"""gaius.leitner — scheduler invariants.

qid must stay byte-identical to the prep-console JS djb2. Grade/draw are the
policy the quiz CLI and any card deck share.
"""
import random

from gaius.leitner import (
    BOX_WEIGHT,
    MASTERED_AT,
    MAX_BOX,
    NEW_WEIGHT,
    box_histogram,
    draw,
    grade,
    item_weight,
    qid,
)


def test_qid_matches_js_djb2_base36():
    # Independent implementation of the JS loop for a known string.
    text = "Gaius embeds with `all-MiniLM-L6-v2` at 384 dimensions."
    h = 5381
    for ch in text:
        h = (((h << 5) + h) ^ ord(ch)) & 0xFFFFFFFF
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    expected = ""
    n = h
    if n == 0:
        expected = "0"
    else:
        chars = []
        while n:
            n, rem = divmod(n, 36)
            chars.append(alphabet[rem])
        expected = "".join(reversed(chars))
    assert qid(text) == expected
    assert qid(text) == qid(text)
    assert qid(text + " ") != qid(text)  # editing q mints a new id


def test_unseen_ranks_below_a_miss():
    miss = {"b": 0, "n": 1, "c": 0, "w": 1}
    assert item_weight(None) == NEW_WEIGHT
    assert item_weight({"b": 0, "n": 0, "c": 0, "w": 0}) == NEW_WEIGHT
    assert item_weight(miss) == BOX_WEIGHT[0]
    assert item_weight(miss) > item_weight(None)


def test_miss_drops_to_box_zero():
    rec = {"b": 3, "n": 4, "c": 4, "w": 0}
    missed = grade(rec, False)
    assert missed["b"] == 0
    assert missed["w"] == 1
    assert missed["n"] == 5
    assert rec["b"] == 3  # input not mutated


def test_correct_promotes_and_caps():
    rec = {"b": 0, "n": 0, "c": 0, "w": 0}
    rec = grade(rec, True)
    assert rec["b"] == 1
    parked = {"b": MAX_BOX, "n": 9, "c": 9, "w": 0}
    assert grade(parked, True)["b"] == MAX_BOX


def test_draw_without_replacement_respects_budget():
    ids = [f"q{i}" for i in range(10)]
    progress = {i: {"b": 0, "n": 1, "c": 0, "w": 1} for i in ids}
    got = draw(ids, progress, rng=random.Random(0), k=4)
    assert len(got) == 4
    assert len(set(got)) == 4
    assert draw([], {}, k=3) == []


def test_draw_prefers_misses_over_mastered():
    ids = ["miss", "mast"]
    progress = {
        "miss": {"b": 0, "n": 2, "c": 0, "w": 2},
        "mast": {"b": MASTERED_AT, "n": 8, "c": 8, "w": 0},
    }
    rng = random.Random(1)
    picks = [draw(ids, progress, rng=rng, k=1)[0] for _ in range(80)]
    assert picks.count("miss") > picks.count("mast")


def test_histogram_skips_unseen():
    progress = {
        "a": {"b": 0, "n": 1, "c": 0, "w": 1},
        "b": {"b": 4, "n": 5, "c": 5, "w": 0},
        "c": {"b": 0, "n": 0, "c": 0, "w": 0},
    }
    hist = box_histogram(progress, ["a", "b", "c", "missing"])
    assert hist[0] == 1
    assert hist[4] == 1
    assert sum(hist) == 2
