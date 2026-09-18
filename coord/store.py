"""带锁的内存存储与幂等事件索引。

同一外部事件可能经短信、中心回调等渠道重复到达。ingest 以
idempotency_key（显式提供，或按事件类型由自然键推导）去重：
第一次落库并返回 applied=True，之后重复到达只回显同一事件、applied=False，
状态绝不二次推进。
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime

from .clock import iso
from .errors import AlreadyAdvanced


@dataclass
class EventRecord:
    id: str
    external_id: str
    type: str
    case_id: str | None
    payload: dict
    idempotency_key: str
    received_utc: datetime
    actor_id: str
    source: str
    applied: bool = True
    duplicate_of: str | None = None
    effect_note: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "external_id": self.external_id,
            "type": self.type,
            "case_id": self.case_id,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "received_utc": iso(self.received_utc),
            "actor_id": self.actor_id,
            "source": self.source,
            "applied": self.applied,
            "duplicate_of": self.duplicate_of,
            "effect_note": self.effect_note,
        }


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.searches: dict[str, object] = {}
        self.cases: dict[str, object] = {}
        self.products: dict[str, object] = {}
        self.events: list[EventRecord] = []
        self._event_index: dict[str, str] = {}   # idempotency_key -> event.id
        self._seq = 0
        self.notifications: dict[str, object] = {}
        self.notification_log: list = []        # 全部发送动作（含通道）

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}_{self._seq:05d}"

    # ---- 事件幂等 ----
    def has_event(self, key: str) -> str | None:
        return self._event_index.get(key)

    def record_event(self, record: EventRecord, *, duplicate_of: str | None = None):
        with self.lock:
            existing = self._event_index.get(record.idempotency_key)
            if existing is not None:
                raise AlreadyAdvanced(existing)
            record.duplicate_of = duplicate_of
            self.events.append(record)
            self._event_index[record.idempotency_key] = record.id
            return record

    def mark_duplicate(self, key: str, incoming: EventRecord) -> EventRecord:
        """重复事件也留痕，但 applied=False、不触发任何状态变化。"""
        with self.lock:
            original_id = self._event_index[key]
            incoming.applied = False
            incoming.duplicate_of = original_id
            incoming.effect_note = "重复外部事件，已忽略，状态未推进"
            self.events.append(incoming)
            return incoming

    def get_event(self, event_id: str) -> EventRecord | None:
        for e in self.events:
            if e.id == event_id:
                return e
        return None

    def find_case(self, *, code=None, donor_person_id=None, active_only=True):
        for c in self.cases.values():
            if code is not None and c.code != code:
                continue
            if donor_person_id is not None and c.donor_person_id != donor_person_id:
                continue
            if active_only and c.superseded:
                continue
            return c
        return None

    def require_case(self, case_id: str):
        c = self.cases.get(case_id)
        if not c:
            from .errors import NotFound
            raise NotFound(f"病例不存在: {case_id}")
        return c

    def require_product(self, product_id: str):
        p = self.products.get(product_id)
        if not p:
            from .errors import NotFound
            raise NotFound(f"采集物不存在: {product_id}")
        return p

    def log_dispatch(self, entry: dict):
        with self.lock:
            self.notification_log.append(entry)
