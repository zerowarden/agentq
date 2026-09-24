from orders import Order, list_orders


def report_order_ids(orders: list[Order]) -> list[int]:
    return [order.order_id for order in list_orders(orders)]
