# quiz — Leitner human review

`gaius quiz` is the v0.2 headline: a spaced-repetition loop over corpus facts.
It is the **legitimate producer** of `confidence_source='human'`. An agent
answering the quiz forges the corpus_audit trust anchor; the CLI refuses to
mutate from a known agent environment or a non-tty. `--report` is read-only
and is the calibration view (miss-rate by domain).

```
gaius quiz --budget 10          # interactive; requires a human tty
gaius quiz --report             # box histogram + miss-rate; agents may run this
gaius quiz --report --domain networking
```

## What it is not

- It is not a gate in front of `inject`. Facts still inject at `review_state='auto'`.
- It is not `gaius agent-review`. That verb is queue hygiene and **never** writes
  `confidence_source='human'`.
- It is not a flashcard app. Question decks are caller-owned. This package
  ships the scheduler (`gaius.leitner`) and the card *validator* (`gaius.cards`);
  it does not ship anyone's questions.

## Box policy

Weighted draw, not calendar intervals (those remain a later config overlay):

| box | meaning | draw weight |
|-----|---------|-------------|
| unseen (`n=0`) | never asked | 8 |
| 0 | just missed | 12 |
| 1 | one success | 6 |
| 2 | | 3 |
| 3 | | 2 |
| 4 | mastered | 1 |
| 5 | parked | 1 |

A miss resets the box to 0. A first `y` (fact still true) writes
`confidence_source='human'` and `review_state='confirmed'`. Repeats only move
the box — they do **not** increment `confirmation_count`, so reciting a fact
cannot inflate inject rank. `n` rejects (`outcome='rejected'`, drops from
inject) and resets the box.

`fact_type='live'` rows are excluded: they expire faster than a useful interval.

## Card decks

`gaius.cards.validate_cards` is the mechanical gate for multiple-choice decks
(4 choices, one correct, every `why` filled, no length-tell, no duplicate `q`).
`gaius.leitner.qid` hashes question text the same way a typical static HTML
deck does, so editing `q` deliberately resets that card's box. Bind cards to
corpus rows with optional `prov.fact_ids` if you want fact rejection to retire
the card; that wiring lives in the caller, not here.
