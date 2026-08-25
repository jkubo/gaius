"""gaius.cards — validator is a GATE, not a report."""
from gaius.cards import validate_cards, card_id, render_js_cards
from gaius.leitner import qid


def _ok(t="short", why="because"):
    return {"t": t, "ok": True, "why": why}


def _no(t="also short", why="no"):
    return {"t": t, "ok": False, "why": why}


def test_happy_path_is_silent():
    cards = [{
        "category": "demo",
        "subcategory": "x",
        "q": "Which gate is real?",
        "choices": [
            _ok("fails closed on both branches of the test", "y"),
            _no("only tested the deny path of that gate once", "n1"),
            _no("a prompt telling the model it is read-only", "n2"),
            _no("a length check on the worker self-report", "n3"),
        ],
    }]
    assert validate_cards(cards) == []


def test_length_tell_is_a_failure():
    cards = [{
        "q": "pick one",
        "choices": [
            _ok("this correct answer is dramatically longer than the rest of them"),
            _no("short a", "n"),
            _no("short b", "n"),
            _no("short c", "n"),
        ],
    }]
    problems = validate_cards(cards)
    assert any("LENGTH TELL" in p for p in problems)


def test_spread_and_wrong_shape():
    too_spread = [{
        "q": "x",
        "choices": [
            _ok("abcdefghij", "y"),
            _no("a", "n"),
            _no("ab", "n"),
            _no("abc", "n"),
        ],
    }]
    assert any("spread" in p for p in validate_cards(too_spread))
    three = [{"q": "x", "choices": [_ok(), _no(), _no()]}]
    assert any("3 choices" in p for p in validate_cards(three))
    two_ok = [{"q": "x", "choices": [_ok(), _ok("other", "y"), _no(), _no("z", "n")]}]
    assert any("2 correct" in p for p in validate_cards(two_ok))


def test_duplicate_q_and_qid_stability():
    c = {
        "q": "same question",
        "choices": [_ok("alpha-option-text", "y"), _no("beta-option-text", "n"),
                    _no("gamma-option-text", "n"), _no("delta-option-text", "n")],
    }
    problems = validate_cards([c, dict(c)])
    assert any("duplicate" in p for p in problems)
    assert card_id(c) == qid("same question")


def test_render_includes_sentinels_and_prov():
    cards = [{
        "category": "kub0",
        "subcategory": "Tessera",
        "q": "What is tessera?",
        "prov": {"fact_ids": [1], "source": "spec", "as_of": "2026-08-20"},
        "choices": [
            _ok("control plane issues authorizations", "y"),
            _no("a worker grades itself", "n"),
            _no("an unfenced shell on the laptop", "n"),
            _no("a prompt that says read-only", "n"),
        ],
    }]
    js = render_js_cards(cards, "/* BEGIN */", "/* END */")
    assert js.startswith("/* BEGIN */")
    assert js.endswith("/* END */")
    assert "prov:" in js
    assert '"fact_ids": [1]' in js or '"fact_ids":[1]' in js
