"""Budget control — the component that makes the agent safe to run unattended.

Every rule here is fail-closed: when a number is missing, stale or ambiguous, the
guard refuses to spend rather than assuming the best case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import BudgetExceededError
from app.domain.plan import AccountSnapshot

# Comparisons in rubles; ignore sub-kopeck float noise only.
EPSILON = 1e-6


@dataclass
class BudgetGuard:
    """Tracks how much of the user's budget is still safely spendable.

    ``budget_rub`` is the user's hard ceiling. ``spendable_rub`` is what the agent
    is allowed to commit — the budget minus a safety margin that absorbs upstream
    price drift between planning and execution.
    """

    budget_rub: float
    safety_margin: float = 0.0
    committed_rub: float = 0.0
    account: AccountSnapshot | None = None
    events: list[str] = field(default_factory=list)

    @property
    def margin_rub(self) -> float:
        return round(self.budget_rub * self.safety_margin, 4)

    @property
    def spendable_rub(self) -> float:
        return round(self.budget_rub - self.margin_rub, 4)

    @property
    def remaining_rub(self) -> float:
        """Head-room left inside the spendable envelope."""
        return round(self.spendable_rub - self.committed_rub, 4)

    @property
    def hard_remaining_rub(self) -> float:
        """Head-room left against the user's absolute ceiling."""
        return round(self.budget_rub - self.committed_rub, 4)

    def can_afford(self, cost: float) -> bool:
        return cost <= self.remaining_rub + EPSILON

    def check(self, cost: float, *, label: str = "шаг") -> None:
        """Raise unless ``cost`` fits in both the spendable envelope and the ceiling."""
        if cost < 0:
            raise BudgetExceededError(f"Отрицательная стоимость для {label}: {cost}")
        if cost > self.hard_remaining_rub + EPSILON:
            raise BudgetExceededError(
                f"{label}: стоимость {cost:.2f}₽ превышает остаток бюджета "
                f"{self.hard_remaining_rub:.2f}₽ (лимит {self.budget_rub:.2f}₽)."
            )
        if cost > self.remaining_rub + EPSILON:
            raise BudgetExceededError(
                f"{label}: стоимость {cost:.2f}₽ не укладывается в безопасный остаток "
                f"{self.remaining_rub:.2f}₽ (резерв на изменение цены {self.margin_rub:.2f}₽)."
            )

    def commit(self, cost: float, *, label: str = "шаг") -> float:
        """Reserve/record ``cost``; raises before committing if it does not fit."""
        self.check(cost, label=label)
        self.committed_rub = round(self.committed_rub + cost, 4)
        self.events.append(f"{label}: -{cost:.2f}₽, остаток {self.remaining_rub:.2f}₽")
        return self.remaining_rub

    def release(self, cost: float, *, label: str = "возврат") -> None:
        """Give budget back (e.g. an auto-refunded failed generation)."""
        self.committed_rub = round(max(0.0, self.committed_rub - cost), 4)
        self.events.append(f"{label}: +{cost:.2f}₽, остаток {self.remaining_rub:.2f}₽")

    # -- account-level guards ---------------------------------------------
    def check_account(self, cost: float, *, label: str = "шаг") -> None:
        """Verify the *account* can pay, independently of the user's budget.

        Unknown balance in a spending context is treated as "cannot pay".
        """
        account = self.account
        if account is None:
            raise BudgetExceededError(
                f"{label}: состояние аккаунта неизвестно — списание запрещено (fail-closed)."
            )
        if account.balance_rub is None:
            raise BudgetExceededError(
                f"{label}: баланс аккаунта недоступен — списание запрещено (fail-closed)."
            )
        if cost > account.balance_rub + EPSILON:
            raise BudgetExceededError(
                f"{label}: стоимость {cost:.2f}₽ превышает баланс аккаунта "
                f"{account.balance_rub:.2f}₽."
            )
        daily_remaining = account.daily_remaining_rub
        if daily_remaining is not None and cost > daily_remaining + EPSILON:
            raise BudgetExceededError(
                f"{label}: стоимость {cost:.2f}₽ превышает дневной лимит траты "
                f"(осталось {daily_remaining:.2f}₽)."
            )

    def check_all(self, cost: float, *, label: str = "шаг") -> None:
        self.check(cost, label=label)
        self.check_account(cost, label=label)
