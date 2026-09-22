# Ported from ts/budget.ts. Pure: it never reads a clock or counts a token;
# the caller charges what it measured. fixtures/budget/v0.json is the contract
# both language ports must satisfy - do not change behavior here without
# checking it still passes that fixture.
#
# A ledger of turns, time, and tokens against caps. Lifted from a chat agent's
# per-question budget (a turn cap, a wall-clock cap, an input-plus-output
# token cap), where exhaustion ends the loop with "the findings so far"
# instead of an error. This part keeps the accounting and the exhaustion
# verdict; what to do when exhausted stays with the caller.
#
# Departures from the source: every cap is optional (None: no cap); a budget
# is exhausted when the amount used reaches its cap, for all three resources
# alike (the source compared turns with >= but time and tokens with >); the
# ledger holds 64-bit integers and its additions saturate at 2^63 - 1.
#
# Caps and amounts are non-negative integers up to 2^63 - 1; a float that is
# integral counts as its integer, as everywhere in this spec. Anything else is
# out of contract, like a string handed to the breaker: these are the caller's
# own literals, not data a verdict must speak to, so they raise. A cap or an
# amount that is None is absent - no cap, nothing used - and the keyword-only
# signatures refuse a field the contract does not name.

from __future__ import annotations

from typing import Any, Optional

from haltrule.contract import integer
from haltrule.messages import show
from haltrule.verdict import verdict

# 2^63 - 1: the largest value every port's ledger holds.
_LEDGER_MAX = 2**63 - 1


def _ledger(value: Any, what: str) -> int:
    if value is None:  # absent: nothing used
        return 0
    return integer(value, what, 0, _LEDGER_MAX)


def _cap(value: Any, what: str) -> Optional[int]:
    return None if value is None else _ledger(value, what)


def _saturating_add(used: int, amount: int) -> int:
    return min(used + amount, _LEDGER_MAX)


class Budget:
    def __init__(
        self,
        *,
        max_turns: Any = None,
        time_budget_ms: Any = None,
        token_budget: Any = None,
    ) -> None:
        self.max_turns = _cap(max_turns, "max_turns")
        self.time_budget_ms = _cap(time_budget_ms, "time_budget_ms")
        self.token_budget = _cap(token_budget, "token_budget")
        self.turns_used = 0
        self.ms_used = 0
        self.tokens_used = 0

    def charge(self, *, turns: Any = 0, ms: Any = 0, tokens: Any = 0) -> dict[str, Any]:
        """Add what was used and say where the budget stands: "warning" with
        the first exhausted resource, in the order turns, time, tokens; "ok"
        while every capped resource is below its cap. A charge of nothing
        reports the current state, and an exhausted budget stays exhausted."""
        # Every amount is read before any is added: a refused charge changes nothing.
        turns = _ledger(turns, "turns")
        ms = _ledger(ms, "ms")
        tokens = _ledger(tokens, "tokens")
        self.turns_used = _saturating_add(self.turns_used, turns)
        self.ms_used = _saturating_add(self.ms_used, ms)
        self.tokens_used = _saturating_add(self.tokens_used, tokens)
        if self.max_turns is not None and self.turns_used >= self.max_turns:
            return verdict(
                "warning",
                "budget_turns",
                f"turns exhausted: {self.turns_used} of {self.max_turns} used",
            )
        if self.time_budget_ms is not None and self.ms_used >= self.time_budget_ms:
            return verdict(
                "warning",
                "budget_time",
                f"time exhausted: {self.ms_used} of {self.time_budget_ms} ms used",
            )
        if self.token_budget is not None and self.tokens_used >= self.token_budget:
            return verdict(
                "warning",
                "budget_tokens",
                f"tokens exhausted: {self.tokens_used} of {self.token_budget} used",
            )

        return verdict(
            "ok",
            "budget_ok",
            "within budget: "
            f"turns {show(self.turns_used, self.max_turns)}, "
            f"ms {show(self.ms_used, self.time_budget_ms)}, "
            f"tokens {show(self.tokens_used, self.token_budget)}",
        )
