import unittest

from orders import Order, list_orders
from report import report_order_ids


class OrdersTests(unittest.TestCase):
    def test_filters_inactive_and_preserves_order(self):
        values = [Order(2), Order(1, active=False), Order(3)]
        self.assertEqual(list_orders(values), [Order(2), Order(3)])

    def test_zero_limit_returns_empty_list(self):
        self.assertEqual(list_orders([Order(1)], limit=0), [])

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            list_orders([Order(1)], limit=-1)

    def test_consumer_uses_order_objects(self):
        self.assertEqual(report_order_ids([Order(7)]), [7])


if __name__ == "__main__":
    unittest.main()
