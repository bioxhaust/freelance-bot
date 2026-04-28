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


class Exchange(ABC):
    source_name: str

    @abstractmethod
    async def fetch_orders(self) -> list[Order]:
        raise NotImplementedError

    def debug_context(self) -> str:
        return ""

    def order_age_minutes(self, order: Order) -> Optional[int]:
        return None
