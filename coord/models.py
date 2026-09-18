"""领域对象：检索需求、捐献病例、同意版本、时间槽、采集物、交接链、温控异常、通知。"""

from dataclasses import dataclass, field
from datetime import datetime

from . import states as S
from .clock import iso


@dataclass
class SearchRequest:
    """受者医院发起的非血缘检索需求（供者侧只见病例代号，不见患者身份）。"""

    id: str
    case_code: str                 # 对外协同用匿名代号
    transplant_org_id: str
    hla_summary: str
    urgency: str = "routine"       # routine / urgent
    created_utc: datetime = None

    def to_dict(self):
        return {
            "id": self.id,
            "case_code": self.case_code,
            "transplant_org_id": self.transplant_org_id,
            "hla_summary": self.hla_summary,
            "urgency": self.urgency,
            "created_utc": iso(self.created_utc) if self.created_utc else None,
        }


@dataclass
class ConsentVersion:
    """同意的一个不可变版本。

    已确认的 grant 不能被改写或删除；撤回不是修改旧版本，而是追加一个
    更高版本号的 withdrawal，且必须引用其撤回的版本。
    """

    case_id: str
    version: int
    kind: str                      # grant / withdrawal
    scopes: tuple
    donor_person_id: str
    signed_utc: datetime
    document_ref: str
    recorded_by: str
    supersedes_version: int | None = None   # withdrawal 引用被撤回的 grant
    note: str = ""

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "version": self.version,
            "kind": self.kind,
            "scopes": list(self.scopes),
            "donor_person_id": self.donor_person_id,
            "signed_utc": iso(self.signed_utc),
            "document_ref": self.document_ref,
            "recorded_by": self.recorded_by,
            "supersedes_version": self.supersedes_version,
            "note": self.note,
        }


@dataclass
class Slot:
    """采集时间槽（每次改期生成新版本，旧版本保留并标记 cancelled）。"""

    id: str
    case_id: str
    version: int
    status: str
    collection_org_id: str
    planned_start_utc: datetime
    planned_end_utc: datetime
    tz: str                        # 以采集医院所在地时区解释
    reason: str = ""
    requested_by: str = ""
    proposed_utc: datetime = None

    def to_dict(self):
        return {
            "id": self.id,
            "case_id": self.case_id,
            "version": self.version,
            "status": self.status,
            "collection_org_id": self.collection_org_id,
            "planned_start_utc": iso(self.planned_start_utc),
            "planned_end_utc": iso(self.planned_end_utc),
            "tz": self.tz,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "proposed_utc": iso(self.proposed_utc) if self.proposed_utc else None,
        }


@dataclass
class Handover:
    """一次实物交接：谁在何时把采集物交给谁，双方确认、温度读数留痕。"""

    id: str
    product_id: str
    seq: int
    from_org_id: str
    from_person_id: str
    to_org_id: str
    to_person_id: str
    at_utc: datetime
    temp_c: float
    temp_ok: bool
    sealed: bool
    note: str = ""
    event_id: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "seq": self.seq,
            "from_org_id": self.from_org_id,
            "from_person_id": self.from_person_id,
            "to_org_id": self.to_org_id,
            "to_person_id": self.to_person_id,
            "at_utc": iso(self.at_utc),
            "temp_c": self.temp_c,
            "temp_ok": self.temp_ok,
            "sealed": self.sealed,
            "note": self.note,
            "event_id": self.event_id,
        }


@dataclass
class TempExcursion:
    """途中温控异常及其处置闭环。"""

    id: str
    product_id: str
    detected_utc: datetime
    reported_by: str
    temp_c: float
    allowed_range: tuple           # (low, high)
    duration_minutes: int
    status: str = S.TEMP_OPEN
    disposition: str | None = None
    decision_by: str | None = None
    decision_utc: datetime | None = None
    waiver_ref: str | None = None
    note: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "detected_utc": iso(self.detected_utc),
            "reported_by": self.reported_by,
            "temp_c": self.temp_c,
            "allowed_range": list(self.allowed_range),
            "duration_minutes": self.duration_minutes,
            "status": self.status,
            "disposition": self.disposition,
            "decision_by": self.decision_by,
            "decision_utc": iso(self.decision_utc) if self.decision_utc else None,
            "waiver_ref": self.waiver_ref,
            "note": self.note,
        }


@dataclass
class Product:
    """采集物（造血干细胞产品）：监管追溯的中心对象。"""

    id: str
    code: str                      # 产品码（冷链与受者侧可见）
    case_id: str
    donor_case_ref: str            # 供者侧匿名引用
    status: str
    collected_utc: datetime | None = None
    collection_org_id: str = ""
    consent_version: int | None = None    # 本批采集物实际采用的同意版本
    temp_range: tuple = (2.0, 8.0)
    current_holder_org_id: str | None = None
    current_holder_person_id: str | None = None
    handovers: list = field(default_factory=list)
    excursions: list = field(default_factory=list)
    delivered_utc: datetime | None = None
    infused_utc: datetime | None = None
    infusing_physician_id: str | None = None
    rejection_reason: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "case_id": self.case_id,
            "donor_case_ref": self.donor_case_ref,
            "status": self.status,
            "collected_utc": iso(self.collected_utc) if self.collected_utc else None,
            "collection_org_id": self.collection_org_id,
            "consent_version": self.consent_version,
            "temp_range": list(self.temp_range),
            "current_holder_org_id": self.current_holder_org_id,
            "current_holder_person_id": self.current_holder_person_id,
            "handovers": [h.to_dict() for h in self.handovers],
            "excursions": [e.to_dict() for e in self.excursions],
            "delivered_utc": iso(self.delivered_utc) if self.delivered_utc else None,
            "infused_utc": iso(self.infused_utc) if self.infused_utc else None,
            "infusing_physician_id": self.infusing_physician_id,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class Notification:
    """一条定向通知：可重发，每次发送与送达都留痕。"""

    id: str
    case_id: str
    template: str
    level: str
    audience_role: str
    audience_org_id: str | None
    recipient_person_id: str
    subject: str
    body: str
    context: dict
    created_utc: datetime
    created_by_event_id: str
    sends: list = field(default_factory=list)      # [{attempt, at, channel, status}]
    delivered_at_utc: datetime | None = None
    delivery_receipt_event: str | None = None
    suppressed: bool = False                        # 受众判定为无需打扰
    suppress_reason: str = ""

    def is_delivered(self) -> bool:
        return self.delivered_at_utc is not None

    def to_dict(self):
        return {
            "id": self.id,
            "case_id": self.case_id,
            "template": self.template,
            "level": self.level,
            "audience_role": self.audience_role,
            "audience_org_id": self.audience_org_id,
            "recipient_person_id": self.recipient_person_id,
            "subject": self.subject,
            "body": self.body,
            "context": self.context,
            "created_utc": iso(self.created_utc),
            "created_by_event_id": self.created_by_event_id,
            "sends": self.sends,
            "delivered_at_utc": iso(self.delivered_at_utc) if self.delivered_at_utc else None,
            "delivery_receipt_event": self.delivery_receipt_event,
            "suppressed": self.suppressed,
            "suppress_reason": self.suppress_reason,
        }


@dataclass
class Case:
    """一个非血缘捐献病例的完整协同状态。"""

    id: str
    code: str
    search_id: str
    donor_person_id: str
    donor_org_id: str                 # 供者归属分库
    transplant_org_id: str
    collection_org_id: str | None = None
    carrier_org_id: str | None = None
    phase: str = S.MATCHED
    created_utc: datetime = None
    updated_utc: datetime = None
    screening_summary: str = ""
    screening_pass: bool | None = None
    effective_consent_version: int | None = None   # 当前生效版本；撤回后置空
    consent_versions: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    active_slot_id: str | None = None
    product_ids: list = field(default_factory=list)
    replaced_by_case_id: str | None = None         # 本供者撤回后启用的替代病例
    superseded: bool = False
    cancel_reason: str = ""
    milestones: list = field(default_factory=list)  # 曾到达的阶段（撤回/取消后仍可判定卷入程度）

    def mark(self, phase):
        if phase not in self.milestones:
            self.milestones.append(phase)

    def ever_reached(self, phase) -> bool:
        return phase in self.milestones

    def active_slot(self) -> Slot | None:
        for slot in self.slots:
            if slot.id == self.active_slot_id:
                return slot
        return None

    def consent(self, version: int) -> ConsentVersion | None:
        for c in self.consent_versions:
            if c.version == version:
                return c
        return None

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "search_id": self.search_id,
            "donor_person_id": self.donor_person_id,
            "donor_org_id": self.donor_org_id,
            "transplant_org_id": self.transplant_org_id,
            "collection_org_id": self.collection_org_id,
            "carrier_org_id": self.carrier_org_id,
            "phase": self.phase,
            "created_utc": iso(self.created_utc) if self.created_utc else None,
            "updated_utc": iso(self.updated_utc) if self.updated_utc else None,
            "screening_summary": self.screening_summary,
            "screening_pass": self.screening_pass,
            "effective_consent_version": self.effective_consent_version,
            "consent_versions": [c.to_dict() for c in self.consent_versions],
            "slots": [s.to_dict() for s in self.slots],
            "active_slot_id": self.active_slot_id,
            "product_ids": list(self.product_ids),
            "replaced_by_case_id": self.replaced_by_case_id,
            "superseded": self.superseded,
            "cancel_reason": self.cancel_reason,
            "milestones": list(self.milestones),
        }
