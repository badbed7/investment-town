from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: UUID
    ticker: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal | None = None


class PaperBroker:
    """In-memory paper broker placeholder for MVP development."""

    def submit(
        self,
        *,
        ticker: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal | None = None,
    ) -> PaperOrder:
        if quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        return PaperOrder(
            order_id=uuid4(),
            ticker=ticker.upper(),
            side=side,
            quantity=quantity,
            limit_price=limit_price,
        )
