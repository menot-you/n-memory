"""The cost of delivery is absolute and measured; the horizon is reported.

HISTORY, because it is the lesson and not a detail — three justifications, two wrong:

1. `nmemory-curator` set `INBOX_ROWS = 8` based on "~65 calls/day".
2. The first iteration of this campaign set 20 based on "~15 calls/day". The
   independent review showed that live `nmemory_context_show` traffic spans THREE
   days — 104, 21, 13 — the first 3.4× the sum of the others and real work, the
   last in progress; and that across the entire window at 8 rows there were 1104 deliveries
   versus 321 writes, so "the inbox was losing ground" was false. Three data points
   do not support any rate.
3. The second iteration justified 20 by "≤30 % of `token_budget`". Also false:
   the inbox is appended AFTER the lanes and never touches the
   `spent`/`token_budget` accounting — it is ADDITIVE, not concurrent.

What remains is what is measured and the session pays for: the ABSOLUTE COST in tokens per call. The
anchor is what the previous form already charged, because it is the only number that
no one has to defend — it was already in production.
"""

from __future__ import annotations

from nmemory.queries.inbox import (
    INBOX_HEADLINE_MAX_CHARS,
    INBOX_NEWEST,
    INBOX_OLDEST,
    INBOX_ROWS,
)

# Measured with `inbox_candidates` against the live store on 2026-09-15: 589 tokens
# without headline and 1393 with the full 141-char headline (the independent witness
# re-measured 589/1393 against my 576/1361 — the set of candidates shifted
# between the two readings, and her number is the one that stays). With the short
# 60-char headline, the installed release served 20 rows in 974 tokens in a
# real session: 48.7 per row. I round UP, because an underestimated
# cost per row lets the ceiling pass a number that the session actually pays.
MEASURED_TOKENS_PER_ROW_SHORT_HEADLINE = 49

# What the previous form ALREADY charged in production: 8 rows × full headline,
# 2142 chars measured ≈ 535-557 tokens depending on the scale. It is not a chosen
# ceiling: it is the price that the owner was already living with before this campaign, and is the only
# anchor that no one has to defend.
#
# Why it IS NOT a slice of `token_budget`, which was the previous justification and
# was fiction: the inbox is appended to `res` AFTER the lanes (`server.py`) and the
# `spent`/`token_budget` accounting of `context.py` never sees it. 580 against
# 2000 would be the ratio between two numbers that do not share a pool — and would be wrong in
# both directions, because the real response costs up to 2000 PLUS the delivery, and in a
# call where retrieve returns little, delivery can be the largest part
# of it. Worse, it coupled this test to a knob in another subsystem: lowering
# `DEFAULT_TOKEN_BUDGET` to 1000 turned it red without the inbox changing a byte.
# Caught by independent review.
PREVIOUS_FORM_COST = 557
PREVIOUS_FORM_ROWS = 8

# How much above the previous price delivery may go. Doubling is the line: for 2×
# the cost it delivers 2.5× the rows AND keeps the clause that decides to read.
MAX_COST_MULTIPLE = 2.0


def test_the_inbox_costs_no_more_than_double_what_the_old_form_cost() -> None:
    cost = INBOX_ROWS * MEASURED_TOKENS_PER_ROW_SHORT_HEADLINE
    ceiling = PREVIOUS_FORM_COST * MAX_COST_MULTIPLE
    assert cost <= ceiling, (
        f"{INBOX_ROWS} rows × {MEASURED_TOKENS_PER_ROW_SHORT_HEADLINE} tok = "
        f"{cost} additive tokens on every nmemory_context_show call, over the "
        f"{ceiling:.0f} that doubling the previous form allows"
    )


def test_the_inbox_delivers_more_than_the_old_form_did() -> None:
    """The other margin: paying more and delivering the same is not a trade.

    Without this, `INBOX_ROWS = 8` would pass the cost ceiling and the queue of 2906
    would never drain — that was how the previous form survived.
    """
    assert INBOX_ROWS >= PREVIOUS_FORM_ROWS * 2, (
        f"{INBOX_ROWS} rows is not materially more than the {PREVIOUS_FORM_ROWS}"
        " the previous form delivered, and the queue is 2906 capsules deep — "
        "paying more to deliver the same is not a trade"
    )


def test_the_headline_is_short_enough_to_pay_for_itself() -> None:
    """The headline returns because dropping it loses, but the full one costs 1393.

    Measured: the handle gives ~48 hyphenated lowercase chars versus 141 for the
    full headline, and the clause by which an agent decides to read falls on the
    lost side. The short one is the tradeoff; the full one does not fit within the cost ceiling.
    """
    assert 0 < INBOX_HEADLINE_MAX_CHARS < 141, (
        "an inbox headline must be shorter than the full one it is cut from"
    )
    full_headline_cost = INBOX_ROWS * 70
    assert full_headline_cost > PREVIOUS_FORM_COST * MAX_COST_MULTIPLE, (
        "if the full headline fitted the ceiling, the short one would be a "
        "cut with no reason — check the measurement before trusting this"
    )


def test_the_budget_is_split_in_half_between_the_two_ends() -> None:
    """The ratio, not just the presence of the two ends.

    The delivery tests require `archive[0] in ids` and `recent in ids`, which
    `1/19` also satisfies — the independent review caught that nothing measured the
    RATIO that the design asserts. Half backward keeps the tail draining
    (1853 older than 7 days in `pynott`), half forward makes "from here
    onward" arrive immediately (1042 within 7 days): both sides are of the same
    order of magnitude, so the budget is half and half. Changing the ratio is a
    decision with a number, not a silent tweak.
    """
    assert INBOX_OLDEST == INBOX_NEWEST, (
        "both ends solve problems of the same order of magnitude (1853 "
        "against 1042 candidates) — an unbalanced ratio needs the number "
        "that justifies it"
    )
    assert INBOX_OLDEST + INBOX_NEWEST == INBOX_ROWS, (
        "the total is the sum of the ends, or the measured cost stops being the cost"
    )
