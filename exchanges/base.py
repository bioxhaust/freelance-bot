from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    title: str
    description: str
    price: str
    deadline: str
    client: str
    source: str
    url: str
    # Kwork buyer stat: share of this buyer's posted projects that ended
    # with a hire, plus how many projects that share is based on.
    # FL.ru does not publish an equivalent number.
    client_hire_percent: Optional[int] = None
    client_orders_count: Optional[int] = None

    def hire_phrase(self) -> Optional[str]:
        """Short Russian label for the card, or None when the exchange has no stat."""
        if self.client_hire_percent is None:
            return None
        percent = self.client_hire_percent
        count = self.client_orders_count
        if count is None:
            return f"нанял в {percent}% заказов"
        return f"нанял в {percent}% из {_orders_genitive(count)}"


class Exchange(ABC):
    source_name: str

    @abstractmethod
    async def fetch_orders(self) -> list[Order]:
        raise NotImplementedError

    def debug_context(self) -> str:
        return ""

    def order_age_minutes(self, order: Order) -> Optional[int]:
        return None


def _orders_genitive(count: int) -> str:
    """«из N …»: 1 заказа, 21 заказа, иначе заказов."""
    n = abs(int(count))
    tail = n % 100
    if tail % 10 == 1 and tail != 11:
        word = "заказа"
    else:
        word = "заказов"
    return f"{n} {word}"
