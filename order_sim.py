"""Order-processing simulation.

Demonstrates three things a distributed system has to get right:

1. Versioned event ordering - updates are applied strictly in version order,
   even if they arrive out of order, and duplicates are ignored.
2. Missing events - a later version is held as pending instead of letting the
   order jump ahead.
3. Stale workers - every write carries a fencing token, and a write with a
   token older than one the store has already seen is rejected.

Run:  python order_sim.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

APPLIED = "APPLIED"
BUFFERED = "BUFFERED"
DUPLICATE = "DUPLICATE"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    version: int
    status: str


@dataclass
class OrderState:
    version: int = 0                 # last version applied (0 = nothing yet)
    status: str = "(none)"
    pending: dict[int, OrderEvent] = field(default_factory=dict)
    highest_token: int = 0           # highest fencing token seen so far
    history: list[str] = field(default_factory=list)


class OrderStore:
    """The single source of truth for orders.

    Ordering is decided by the version number carried in each event, never by
    arrival time. Writers are fenced by a monotonically increasing token.
    """

    def __init__(self, log=print):
        self._orders: dict[str, OrderState] = {}
        self._log = log

    def state(self, order_id: str) -> OrderState:
        return self._orders.setdefault(order_id, OrderState())

    def missing_versions(self, order_id: str) -> list[int]:
        """Versions we are still waiting for before pending events can apply."""
        st = self.state(order_id)
        if not st.pending:
            return []
        return [v for v in range(st.version + 1, max(st.pending)) if v not in st.pending]

    # ---- 1 & 2: versioned ordering, duplicates, missing events -------------

    def receive(self, event: OrderEvent) -> str:
        st = self.state(event.order_id)
        tag = f"[{event.order_id}] v{event.version} {event.status}"

        if event.version <= st.version:
            self._log(f"  {tag}: DUPLICATE - already applied (current v{st.version}), ignored")
            return DUPLICATE

        if event.version in st.pending:
            self._log(f"  {tag}: DUPLICATE - already buffered as pending, ignored")
            return DUPLICATE

        if event.version > st.version + 1:
            st.pending[event.version] = event
            self._log(
                f"  {tag}: BUFFERED - waiting for missing version(s) "
                f"{self.missing_versions(event.order_id)}; order stays at "
                f"v{st.version} {st.status}"
            )
            return BUFFERED

        self._apply(st, event)
        # Applying this version may unblock buffered later versions.
        while st.version + 1 in st.pending:
            self._apply(st, st.pending.pop(st.version + 1), from_buffer=True)
        return APPLIED

    def _apply(self, st: OrderState, event: OrderEvent, from_buffer: bool = False) -> None:
        old = f"v{st.version} {st.status}"
        st.version, st.status = event.version, event.status
        st.history.append(event.status)
        note = " (released from pending buffer)" if from_buffer else ""
        self._log(
            f"  [{event.order_id}] v{event.version} {event.status}: APPLIED{note} - "
            f"{old} -> v{st.version} {st.status}"
        )

    # ---- 3: fencing tokens --------------------------------------------------

    def write(self, worker: str, token: int, event: OrderEvent) -> str:
        """A worker's write. Checked against the fencing token first."""
        st = self.state(event.order_id)
        if token < st.highest_token:
            self._log(
                f"  [{event.order_id}] {worker} (token {token}) writes v{event.version} "
                f"{event.status}: REJECTED - stale token, store has already seen "
                f"token {st.highest_token}"
            )
            return REJECTED
        st.highest_token = token
        self._log(
            f"  [{event.order_id}] {worker} (token {token}) writes v{event.version} "
            f"{event.status}: token accepted"
        )
        return self.receive(event)


class LockService:
    """Hands out leases. Each lease comes with a strictly increasing token."""

    def __init__(self):
        self._next_token = 0

    def acquire(self, worker: str, log=print) -> int:
        self._next_token += 1
        log(f"  lock service: {worker} acquires the lease, fencing token = {self._next_token}")
        return self._next_token


def show(store: OrderStore, order_id: str) -> None:
    st = store.state(order_id)
    pending = {v: e.status for v, e in sorted(st.pending.items())}
    print(
        f"  => final state of {order_id}: v{st.version} {st.status} | "
        f"history: {' -> '.join(st.history) or '(empty)'} | "
        f"pending: {pending or 'none'} | missing: {store.missing_versions(order_id) or 'none'}"
    )


def scenario_ordering() -> OrderStore:
    print("\n=== Scenario 1: out-of-order delivery + duplicate (v2, v1, v2 again) ===")
    store = OrderStore()
    oid = "order-1001"
    for ev in (
        OrderEvent(oid, 2, "PAID"),
        OrderEvent(oid, 1, "CREATED"),
        OrderEvent(oid, 2, "PAID"),
    ):
        print(f"deliver v{ev.version} {ev.status}")
        store.receive(ev)
    show(store, oid)
    return store


def scenario_missing() -> OrderStore:
    print("\n=== Scenario 2: missing event (only v3 arrives; v1 and v2 never do) ===")
    store = OrderStore()
    oid = "order-2002"
    ev = OrderEvent(oid, 3, "SHIPPED")
    print(f"deliver v{ev.version} {ev.status}")
    store.receive(ev)
    show(store, oid)

    print("deliver v1 CREATED (v2 is still missing)")
    store.receive(OrderEvent(oid, 1, "CREATED"))
    show(store, oid)
    return store


def scenario_stale_worker() -> OrderStore:
    print("\n=== Scenario 3: stale worker rejected by fencing token ===")
    store = OrderStore()
    locks = LockService()
    oid = "order-3003"

    token_a = locks.acquire("worker-A")
    store.write("worker-A", token_a, OrderEvent(oid, 1, "CREATED"))
    print("  worker-A stalls (e.g. long GC pause); its lease expires")

    token_b = locks.acquire("worker-B")
    store.write("worker-B", token_b, OrderEvent(oid, 2, "PAID"))

    print("  worker-A wakes up, still believes it holds the lease")
    store.write("worker-A", token_a, OrderEvent(oid, 3, "SHIPPED"))

    store.write("worker-B", token_b, OrderEvent(oid, 3, "SHIPPED"))
    show(store, oid)
    return store


if __name__ == "__main__":
    scenario_ordering()
    scenario_missing()
    scenario_stale_worker()
