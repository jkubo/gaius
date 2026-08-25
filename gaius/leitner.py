"""gaius.leitner — spaced-repetition scheduler (v0.2 headline).

Pure functions over an item-id → progress map. No I/O, no facts.db, no
question text. The quiz CLI, the private prep console, and any other deck
share this module; content stays with the caller.

The box policy is the one already shipped in the private prep console
(weighted draw, miss → box 0, unseen ranks below a recent miss). Calendar
`next_review` intervals are a later config overlay, not the default.

Progress record: {"b": box, "n": seen, "c": correct, "w": wrong}.
"""
from __future__ import annotations

import random
from typing import Iterable, Mapping, MutableMapping, Sequence

MAX_BOX = 5
MASTERED_AT = 4
# Draw weight per box. Index 0 = just missed (resurface hard); index 5 = parked.
BOX_WEIGHT = (12, 6, 3, 2, 1, 1)
NEW_WEIGHT = 8  # unseen: below a miss (12), above a near-mastered item (1–2)

Progress = Mapping[str, Mapping[str, int]]
MutProgress = MutableMapping[str, dict]


def qid(text: str) -> str:
    """djb2 of the question text, unsigned 32-bit, base36.

    Byte-identical to the prep-console `qid()` so a deck keyed here and a
    deck keyed in the HTML app share progress. Editing `text` mints a new
    id and deliberately resets that item's box.
    """
    h = 5381
    for ch in text:
        h = (((h << 5) + h) ^ ord(ch)) & 0xFFFFFFFF
    return _to_base36(h)


def _to_base36(n: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 36)
        chars.append(alphabet[rem])
    return "".join(reversed(chars))


def item_weight(rec: Mapping[str, int] | None) -> int:
    """Draw weight for one item. Unseen uses NEW_WEIGHT even if box defaults to 0."""
    if rec is None or int(rec.get("n") or 0) == 0:
        return NEW_WEIGHT
    b = int(rec.get("b") or 0)
    if b < 0:
        b = 0
    if b > MAX_BOX:
        b = MAX_BOX
    return BOX_WEIGHT[b]


def grade(rec: Mapping[str, int] | None, correct: bool) -> dict:
    """Return a new progress record after one attempt. Miss drops to box 0."""
    r = {
        "b": int((rec or {}).get("b") or 0),
        "n": int((rec or {}).get("n") or 0),
        "c": int((rec or {}).get("c") or 0),
        "w": int((rec or {}).get("w") or 0),
    }
    r["n"] += 1
    if correct:
        r["c"] += 1
        r["b"] = min(MAX_BOX, r["b"] + 1)
    else:
        r["w"] += 1
        r["b"] = 0
    return r


def draw(
    item_ids: Sequence[str],
    progress: Progress,
    rng: random.Random | None = None,
    k: int = 1,
) -> list[str]:
    """Weighted sample without replacement. Empty pool → empty list.

    `k` is a session budget. Mastered items keep a weight of 1 so they can
    still appear; they do not vanish from the deck.
    """
    ids = [i for i in item_ids if i]
    if not ids:
        return []
    rng = rng or random.Random()
    k = min(k, len(ids))
    chosen: list[str] = []
    pool = list(ids)
    for _ in range(k):
        weights = [item_weight(progress.get(i)) for i in pool]
        total = sum(weights)
        if total <= 0:
            break
        pick = rng.randrange(total)
        acc = 0
        idx = 0
        for idx, w in enumerate(weights):
            acc += w
            if pick < acc:
                break
        chosen.append(pool.pop(idx))
    return chosen


def box_histogram(progress: Progress, item_ids: Iterable[str] | None = None) -> list[int]:
    """Counts per box 0..MAX_BOX, plus unseen as box 0-unseen is NOT mixed in.

    Returns a list of length MAX_BOX+1 over items that have been seen.
    Unseen ids (n==0 or missing) are omitted — callers that want an unseen
    count should compute it themselves.
    """
    hist = [0] * (MAX_BOX + 1)
    ids = list(item_ids) if item_ids is not None else list(progress.keys())
    for i in ids:
        rec = progress.get(i)
        if rec is None or int(rec.get("n") or 0) == 0:
            continue
        b = int(rec.get("b") or 0)
        b = 0 if b < 0 else (MAX_BOX if b > MAX_BOX else b)
        hist[b] += 1
    return hist
