from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    order_id: int
    active: bool = True


def list_orders(orders: list[Order], limit: int | None = None) -> list[Order]:
    """Return active orders in input order, optionally limited."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be nonnegative")
    active = [order for order in orders if order.active]
    return active if limit is None else active[:limit]
