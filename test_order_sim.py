import unittest

from order_sim import (
    APPLIED,
    BUFFERED,
    DUPLICATE,
    REJECTED,
    LockService,
    OrderEvent,
    OrderStore,
)


def quiet(*_args, **_kwargs):
    pass


class VersionedOrderingTest(unittest.TestCase):
    def setUp(self):
        self.store = OrderStore(log=quiet)

    def test_v2_v1_v2_again(self):
        oid = "o1"
        self.assertEqual(self.store.receive(OrderEvent(oid, 2, "PAID")), BUFFERED)
        self.assertEqual(self.store.state(oid).version, 0)
        self.assertEqual(self.store.receive(OrderEvent(oid, 1, "CREATED")), APPLIED)
        self.assertEqual(self.store.receive(OrderEvent(oid, 2, "PAID")), DUPLICATE)

        st = self.store.state(oid)
        self.assertEqual((st.version, st.status), (2, "PAID"))
        self.assertEqual(st.history, ["CREATED", "PAID"])
        self.assertEqual(st.pending, {})

    def test_duplicate_of_buffered_event_is_ignored(self):
        oid = "o1"
        self.store.receive(OrderEvent(oid, 2, "PAID"))
        self.assertEqual(self.store.receive(OrderEvent(oid, 2, "PAID")), DUPLICATE)
        self.assertEqual(len(self.store.state(oid).pending), 1)

    def test_missing_event_does_not_jump_ahead(self):
        oid = "o2"
        self.assertEqual(self.store.receive(OrderEvent(oid, 3, "SHIPPED")), BUFFERED)
        st = self.store.state(oid)
        self.assertEqual((st.version, st.status), (0, "(none)"))
        self.assertEqual(self.store.missing_versions(oid), [1, 2])

        self.store.receive(OrderEvent(oid, 1, "CREATED"))
        self.assertEqual((st.version, st.status), (1, "CREATED"))
        self.assertEqual(self.store.missing_versions(oid), [2])
        self.assertIn(3, st.pending)

    def test_gap_filled_releases_all_pending(self):
        oid = "o3"
        self.store.receive(OrderEvent(oid, 3, "SHIPPED"))
        self.store.receive(OrderEvent(oid, 2, "PAID"))
        self.store.receive(OrderEvent(oid, 1, "CREATED"))
        st = self.store.state(oid)
        self.assertEqual(st.history, ["CREATED", "PAID", "SHIPPED"])
        self.assertEqual(self.store.missing_versions(oid), [])


class FencingTokenTest(unittest.TestCase):
    def test_stale_worker_is_rejected(self):
        store = OrderStore(log=quiet)
        locks = LockService()
        oid = "o4"

        token_a = locks.acquire("A", log=quiet)
        self.assertEqual(store.write("A", token_a, OrderEvent(oid, 1, "CREATED")), APPLIED)

        token_b = locks.acquire("B", log=quiet)
        self.assertGreater(token_b, token_a)
        self.assertEqual(store.write("B", token_b, OrderEvent(oid, 2, "PAID")), APPLIED)

        # A's write would be the next valid version, but its token is stale.
        self.assertEqual(store.write("A", token_a, OrderEvent(oid, 3, "SHIPPED")), REJECTED)
        st = store.state(oid)
        self.assertEqual((st.version, st.status), (2, "PAID"))

        self.assertEqual(store.write("B", token_b, OrderEvent(oid, 3, "SHIPPED")), APPLIED)
        self.assertEqual(st.history, ["CREATED", "PAID", "SHIPPED"])


if __name__ == "__main__":
    unittest.main()
