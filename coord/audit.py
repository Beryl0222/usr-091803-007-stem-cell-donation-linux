"""监管可复核的哈希链审计记录。

每一条状态推进都生成一条 audit entry：
    entry_n = sha256(prev_hash || canonical_json(本条内容))
任何对历史的删改都会破坏链尾 hash；timeline 接口同时返回链指纹与逐条校验结果。
"""

import hashlib
import json

from .clock import iso


def canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuditEntry:
    def __init__(self, seq, ts, actor_id, action, case_id, product_id,
                 ref_type, ref_id, payload, prev_hash, event_id="", note=""):
        self.seq = seq
        self.ts = ts
        self.actor_id = actor_id
        self.action = action
        self.case_id = case_id
        self.product_id = product_id
        self.ref_type = ref_type
        self.ref_id = ref_id
        self.payload = payload
        self.prev_hash = prev_hash
        self.event_id = event_id
        self.note = note
        self.hash = self._compute()

    def _compute(self) -> str:
        body = canonical({
            "seq": self.seq,
            "ts": iso(self.ts),
            "actor_id": self.actor_id,
            "action": self.action,
            "case_id": self.case_id,
            "product_id": self.product_id,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "payload": self.payload,
            "event_id": self.event_id,
            "note": self.note,
            "prev_hash": self.prev_hash,
        })
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts_utc": iso(self.ts),
            "actor_id": self.actor_id,
            "action": self.action,
            "case_id": self.case_id,
            "product_id": self.product_id,
            "ref": {"type": self.ref_type, "id": self.ref_id},
            "payload": self.payload,
            "event_id": self.event_id,
            "note": self.note,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


GENESIS = "0" * 64


class AuditLog:
    def __init__(self):
        self.entries: list[AuditEntry] = []

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def append(self, *, ts, actor_id, action, case_id=None, product_id=None,
               ref_type="", ref_id="", payload=None, event_id="", note="") -> AuditEntry:
        entry = AuditEntry(
            seq=len(self.entries) + 1,
            ts=ts,
            actor_id=actor_id,
            action=action,
            case_id=case_id,
            product_id=product_id,
            ref_type=ref_type,
            ref_id=ref_id,
            payload=payload or {},
            prev_hash=self.head,
            event_id=event_id,
            note=note,
        )
        self.entries.append(entry)
        return entry

    def verify(self) -> dict:
        """自校验链完整性。"""
        prev = GENESIS
        for e in self.entries:
            if e.prev_hash != prev:
                return {"ok": False, "broken_at_seq": e.seq, "reason": "prev_hash 不匹配"}
            if e._compute() != e.hash:
                return {"ok": False, "broken_at_seq": e.seq, "reason": "内容哈希不匹配"}
            prev = e.hash
        return {"ok": True, "entries": len(self.entries), "head": self.head}
