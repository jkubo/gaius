"""gaius.cards — MCQ card schema + validator (content-agnostic).

Ships in OSS. Card *content* does not — a caller supplies the array.
Every rule here has cost a real study bank time before:

  * 4 choices / exactly one correct — renderers assume both and silently
    mis-score otherwise.
  * Every choice carries `why` — being right about one option is not
    knowing why the other three fail.
  * No length tell — a bank you can beat by picking the longest option
    reports mastery you do not have.
  * qid is a djb2 of question TEXT, so a duplicate question text is two
    cards sharing one Leitner box.

Optional `prov` binds a card to corpus rows so a fact lifecycle can
retire or regenerate `why` without touching `q` (touching `q` resets the
box). This module does not fetch facts; it only checks shape.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable

from gaius.leitner import qid

LENGTH_SPREAD = 1.40


def validate_cards(cards: Iterable[dict]) -> list[str]:
    """Return a list of problems. Empty list = gate green."""
    problems: list[str] = []
    cards = list(cards)
    for i, c in enumerate(cards):
        where = f"card {i} ({c.get('subcategory') or c.get('category') or '?'}): "
        ch = c.get("choices") or []
        if len(ch) != 4:
            problems.append(where + f"{len(ch)} choices, expected 4")
            continue
        oks = [x for x in ch if x.get("ok")]
        if len(oks) != 1:
            problems.append(where + f"{len(oks)} correct answers, expected exactly 1")
        if any(not str(x.get("why") or "").strip() for x in ch):
            problems.append(where + "a choice has an empty `why`")
        q = c.get("q") or ""
        if not str(q).strip():
            problems.append(where + "empty question text")
        lens = [len(x.get("t") or "") for x in ch]
        if min(lens) == 0:
            problems.append(where + "a choice has empty text")
            continue
        if oks and len(oks[0].get("t") or "") == max(lens) and lens.count(max(lens)) == 1:
            problems.append(
                where + f"LENGTH TELL — correct answer is the longest ({sorted(lens)})"
            )
        if max(lens) / min(lens) > LENGTH_SPREAD:
            problems.append(where + f"length spread >40% ({sorted(lens)})")
        if re.search(r"\*\*|^\s*[-*]\s", str(q), re.M):
            problems.append(where + "markdown in question text (only `backticks` render)")
        prov = c.get("prov")
        if prov is not None:
            if not isinstance(prov, dict):
                problems.append(where + "prov must be an object")
            else:
                ids = prov.get("fact_ids")
                if ids is not None and not isinstance(ids, list):
                    problems.append(where + "prov.fact_ids must be an array")

    counts = Counter(str(c.get("q") or "").strip().lower() for c in cards)
    for q, n in counts.items():
        if q and n > 1:
            problems.append(f"duplicate question text x{n}: {q[:70]}…")
    return problems


def card_id(card: dict) -> str:
    return qid(str(card.get("q") or ""))


def render_js_cards(cards: list[dict], begin: str, end: str) -> str:
    """Render a JS array region between sentinel comments (no wrapping array)."""
    out = [begin]
    by_sub: list[tuple[str, list[dict]]] = []
    seen: dict[str, int] = {}
    for c in cards:
        sub = str(c.get("subcategory") or "")
        if sub not in seen:
            seen[sub] = len(by_sub)
            by_sub.append((sub, []))
        by_sub[seen[sub]][1].append(c)
    for sub, group in by_sub:
        label = sub or "ungrouped"
        out.append(f"\n// ---------- {label} ----------")
        for c in group:
            cat = c.get("category") or ""
            lines = [
                f'{{category:{_js(cat)}, subcategory:{_js(sub)}, q:{_js(c.get("q") or "")},'
            ]
            if c.get("prov"):
                lines.append(f" prov:{json.dumps(c['prov'], ensure_ascii=False)},")
            lines.append(" choices:[")
            for ch in c.get("choices") or []:
                lines.append(
                    f'  {{t:{_js(ch.get("t") or "")}, ok:{"true" if ch.get("ok") else "false"}, why:{_js(ch.get("why") or "")}}},'
                )
            lines[-1] = lines[-1].rstrip(",")
            lines.append("]},")
            out.append("\n".join(lines))
    out.append(end)
    return "\n".join(out)


def _js(s: Any) -> str:
    return json.dumps("" if s is None else str(s), ensure_ascii=False)
