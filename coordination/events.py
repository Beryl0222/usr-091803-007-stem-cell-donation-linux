"""仅追加事件台账（append-only ledger）。

三条核心保证：
1. 幂等：同一外部事件（短信网关回执、中心回调）携带相同 external_event_id
   时，重复到达只返回首次记录，绝不二次推进状态、不重复通知。
   业务命令也可带 idempotency_key，按病例作用域去重。
2. 防篡改：每条事件含前一条哈希，形成哈希链；监管可独立校验整条链。
3. 可追溯：状态完全由事件流重放得到，不依赖可变主记录。
"""

import hashlib
import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .clock import UTC, now_utc
from .models import Actor


def _canonical(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _hash_event(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + "|" + _canonical(body)).encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64


@dataclass
class Event:
    seq: int
    case_id: str
    event_type: str
    timestamp: str          # ISO UTC
    actor_role: str
    actor_party: str
    payload: dict = field(default_factory=dict)
    idem_key: str = ""           # 病例作用域幂等键
    external_source: str = ""    # 外部系统标识（sms_gateway / his / ...）
    external_event_id: str = ""  # 外部事件唯一 ID（全局去重）
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_line(cls, line: str) -> "Event":
        return cls(**json.loads(line))


class DuplicateExternalEvent(Exception):
    """携带幂等标识的事件重复到达。first 为首次记录，调用方应短路不推进。"""

    def __init__(self, first: Event):
        super().__init__(f"重复事件已忽略: {first.event_type}")
        self.first = first


class Ledger:
    """线程安全的仅追加台账，可持久化到 JSONL（每行一条事件）。"""

    def __init__(self, path: str = ""):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._by_dedup: dict[str, Event] = {}
        self._path = path
        if path:
            self._load()

    # ---- 读取 / 重放 -------------------------------------------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._ingest(Event.from_line(line), persist=False)
        except FileNotFoundError:
            pass

    def _ingest(self, event: Event, persist: bool):
        # 重放或追加时登记去重索引；重复持久化数据不应出现，出现即数据损坏。
        token = self._dedup_token(event)
        if token and token in self._by_dedup:
            raise ValueError(f"台账存在重复去重键: {token}")
        self._events.append(event)
        if token:
            self._by_dedup[token] = event
        if persist and self._path:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(event.to_line() + "\n")
                fh.flush()

    @staticmethod
    def _dedup_token(event: Event) -> str:
        if event.external_source and event.external_event_id:
            return f"ext:{event.external_source}:{event.external_event_id}"
        if event.idem_key:
            return f"case:{event.case_id}:{event.event_type}:{event.idem_key}"
        return ""

    # ---- 写入 ---------------------------------------------------------
    def append(self, *, case_id: str, event_type: str, payload: dict,
               actor: Actor, idem_key: str = "",
               external_source: str = "", external_event_id: str = "",
               timestamp: datetime = None) -> Event:
        """追加一条事件。命中幂等去重时抛出 DuplicateExternalEvent（不写入）。"""
        with self._lock:
            candidate = Event(
                seq=len(self._events) + 1,
                case_id=case_id,
                event_type=event_type,
                timestamp=(timestamp or now_utc()).astimezone(UTC).isoformat(),
                actor_role=actor.role.value,
                actor_party=actor.party_id,
                payload=payload or {},
                idem_key=idem_key or "",
                external_source=external_source or "",
                external_event_id=external_event_id or "",
            )
            token = self._dedup_token(candidate)
            if token and token in self._by_dedup:
                raise DuplicateExternalEvent(self._by_dedup[token])

            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            body = {
                "case_id": candidate.case_id,
                "event_type": candidate.event_type,
                "timestamp": candidate.timestamp,
                "actor_role": candidate.actor_role,
                "actor_party": candidate.actor_party,
                "payload": candidate.payload,
                "idem_key": candidate.idem_key,
                "external_source": candidate.external_source,
                "external_event_id": candidate.external_event_id,
            }
            candidate.prev_hash = prev_hash
            candidate.hash = _hash_event(prev_hash, body)
            self._ingest(candidate, persist=True)
            return candidate

    # ---- 查询 ---------------------------------------------------------
    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def events_for(self, case_id: str) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.case_id == case_id]

    def case_ids(self) -> list[str]:
        with self._lock:
            seen = []
            for e in self._events:
                if e.case_id not in seen:
                    seen.append(e.case_id)
            return seen

    def verify_chain(self) -> dict:
        """独立校验哈希链完整性，供监管复核。"""
        with self._lock:
            prev = GENESIS_HASH
            for e in self._events:
                body = {
                    "case_id": e.case_id,
                    "event_type": e.event_type,
                    "timestamp": e.timestamp,
                    "actor_role": e.actor_role,
                    "actor_party": e.actor_party,
                    "payload": e.payload,
                    "idem_key": e.idem_key,
                    "external_source": e.external_source,
                    "external_event_id": e.external_event_id,
                }
                if e.prev_hash != prev:
                    return {"ok": False, "broken_at_seq": e.seq,
                            "reason": "prev_hash 不衔接"}
                if _hash_event(prev, body) != e.hash:
                    return {"ok": False, "broken_at_seq": e.seq,
                            "reason": "内容哈希不符，记录可能被篡改"}
                prev = e.hash
            return {"ok": True, "events": len(self._events),
                    "head": prev}
