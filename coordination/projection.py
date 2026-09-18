"""事件重放投影：从仅追加台账还原病例当前状态与全部历史。

状态不由任何可变记录保存，而完全由事件流重放得到。
关键历史节点（采集、每次交接）在发生时快照"当时生效的同意版本"，
因此即便同意日后被新版本撤回，监管仍能确认每个节点采用的是哪一版同意。
"""

from dataclasses import dataclass, field
from datetime import datetime

from .clock import Window
from .models import (
    CaseStatus, ConsentAction, ConsentState, HandoverKind,
    ProductType, ShipmentStatus, Role,
)


class IllegalTransition(Exception):
    """当前状态不允许该命令，或前置条件不满足。"""


@dataclass
class ConsentVersion:
    version: int
    action: str
    document_version: str
    document_hash: str
    signed_at_utc: str
    signed_local: str
    tz: str
    witness_party: str
    statement: str
    event_seq: int


@dataclass
class ScheduleVersion:
    schedule_version: int
    window: Window
    reason: str
    proposed_by: str
    confirmations: dict = field(default_factory=dict)  # party -> {at_utc, local, tz}
    replaced: bool = False


@dataclass
class Handover:
    kind: str
    at_utc: str
    from_party: str
    from_person: str
    to_party: str
    to_person: str
    product_temp_c: float
    container_id: str
    evidence_ref: str          # 交接凭证/签字单号
    effective_consent_version: int  # 该节点采用的同意版本
    event_seq: int


@dataclass
class Excursion:
    detected_utc: str
    temp_c: float
    limit_low_c: float
    limit_high_c: float
    reading_local: str
    tz: str
    reported_by: str
    resolution: dict | None = None
    event_seq: int = 0


@dataclass
class CaseSnapshot:
    case_id: str
    status: CaseStatus = CaseStatus.MATCHED
    donor_identity: dict = field(default_factory=dict)
    recipient_identity: dict = field(default_factory=dict)
    donor_party_id: str = ""
    recipient_party_id: str = ""
    donor_center_party_id: str = ""
    courier_party_id: str = ""
    coordinator_party_id: str = ""
    donor_tz: str = "Asia/Shanghai"
    recipient_tz: str = "Asia/Shanghai"
    donor_center_tz: str = "Asia/Shanghai"
    courier_tz: str = "Asia/Shanghai"
    product_type: str = ""
    consents: list = field(default_factory=list)
    schedules: list = field(default_factory=list)
    product: dict | None = None
    handovers: list = field(default_factory=list)
    excursions: list = field(default_factory=list)
    shipment_status: ShipmentStatus = ShipmentStatus.PREPARING
    accepted: dict | None = None
    infusion: dict | None = None
    cancel: dict | None = None
    supersede: dict | None = None
    notes: list = field(default_factory=list)

    # ---- 派生视图 -----------------------------------------------------
    @property
    def consent_state(self) -> ConsentState:
        if not self.consents:
            return ConsentState.NONE
        return (ConsentState.GRANTED if self.consents[-1].action == ConsentAction.GRANT.value
                else ConsentState.WITHDRAWN)

    @property
    def effective_consent(self) -> ConsentVersion | None:
        return self.consents[-1] if self.consents else None

    @property
    def current_schedule(self) -> ScheduleVersion | None:
        for sch in reversed(self.schedules):
            if not sch.replaced:
                return sch
        return None

    def all_confirmed(self) -> bool:
        sch = self.current_schedule
        if not sch:
            return False
        required = {self.donor_party_id, self.donor_center_party_id,
                    self.courier_party_id, self.recipient_party_id}
        return required <= set(sch.confirmations.keys())

    def unconfirmed_parties(self) -> list:
        sch = self.current_schedule
        required = [
            (self.donor_party_id, Role.DONOR),
            (self.donor_center_party_id, Role.DONOR_CENTER),
            (self.courier_party_id, Role.COURIER),
            (self.recipient_party_id, Role.RECIPIENT_HOSPITAL),
        ]
        if not sch:
            return required
        return [(p, r) for p, r in required if p not in sch.confirmations]

    def party_tz(self, party_id: str) -> str:
        mapping = {
            self.donor_party_id: self.donor_tz,
            self.recipient_party_id: self.recipient_tz,
            self.donor_center_party_id: self.donor_center_tz,
            self.courier_party_id: self.courier_tz,
        }
        return mapping.get(party_id, "Asia/Shanghai")


def _as_window(payload) -> Window:
    return Window(
        datetime.fromisoformat(payload["start_utc"]),
        datetime.fromisoformat(payload["end_utc"]),
    )


# 事件类型 -> 应用到快照的变更
def apply_event(snap: CaseSnapshot, event):
    et = event.event_type
    p = event.payload
    seq = event.seq

    if et == "case_opened":
        snap.status = CaseStatus.MATCHED
        snap.donor_identity = p.get("donor_identity", {})
        snap.recipient_identity = p.get("recipient_identity", {})
        for key in ("donor_party_id", "recipient_party_id", "donor_center_party_id",
                    "courier_party_id", "coordinator_party_id"):
            setattr(snap, key, p.get(key, ""))
        for key in ("donor_tz", "recipient_tz", "donor_center_tz", "courier_tz"):
            setattr(snap, key, p.get(key, "Asia/Shanghai"))
        snap.product_type = p.get("product_type", ProductType.PBSC.value)

    elif et == "screening_started":
        snap.status = CaseStatus.SCREENING

    elif et == "screening_passed":
        snap.status = CaseStatus.CONSENT_PENDING

    elif et in ("consent_granted", "consent_withdrawn"):
        snap.consents.append(ConsentVersion(
            version=p["version"],
            action=p["action"],
            document_version=p["document_version"],
            document_hash=p["document_hash"],
            signed_at_utc=p["signed_at_utc"],
            signed_local=p["signed_local"],
            tz=p["tz"],
            witness_party=p.get("witness_party", ""),
            statement=p.get("statement", ""),
            event_seq=seq,
        ))
        if et == "consent_granted":
            snap.status = CaseStatus.CONSENTED

    elif et == "collection_scheduled":
        if snap.current_schedule:
            snap.current_schedule.replaced = True
        snap.schedules.append(ScheduleVersion(
            schedule_version=p["schedule_version"],
            window=_as_window(p),
            reason=p.get("reason", ""),
            proposed_by=p.get("proposed_by", ""),
        ))
        snap.status = CaseStatus.SCHEDULED

    elif et == "schedule_confirmed":
        sch = snap.current_schedule
        sch.confirmations[p["party_id"]] = {
            "at_utc": p["at_utc"], "local": p["local"], "tz": p["tz"]}

    elif et == "collection_completed":
        snap.status = CaseStatus.COLLECTED
        snap.product = {
            "product_id": p["product_id"],
            "volume_ml": p.get("volume_ml"),
            "collected_utc": p["at_utc"],
            "effective_consent_version": p["effective_consent_version"],
            "consent_document_hash": p.get("consent_document_hash", ""),
        }

    elif et == "handover":
        snap.handovers.append(Handover(
            kind=p["kind"], at_utc=p["at_utc"],
            from_party=p["from_party"], from_person=p["from_person"],
            to_party=p["to_party"], to_person=p["to_person"],
            product_temp_c=p["product_temp_c"], container_id=p["container_id"],
            evidence_ref=p["evidence_ref"],
            effective_consent_version=p["effective_consent_version"],
            event_seq=seq,
        ))
        if p["kind"] == HandoverKind.COLLECTION_TO_COURIER.value:
            snap.status = CaseStatus.IN_TRANSIT
            snap.shipment_status = ShipmentStatus.IN_TRANSIT

    elif et == "shipment_excursion":
        snap.shipment_status = ShipmentStatus.QUARANTINED
        snap.excursions.append(Excursion(
            detected_utc=p["detected_utc"], temp_c=p["temp_c"],
            limit_low_c=p["limit_low_c"], limit_high_c=p["limit_high_c"],
            reading_local=p["reading_local"], tz=p["tz"],
            reported_by=p.get("reported_by", ""), event_seq=seq))

    elif et == "excursion_resolved":
        exc = snap.excursions[-1]
        exc.resolution = {
            "result": p["result"], "decided_by": p["decided_by"],
            "decided_utc": p["decided_utc"], "note": p.get("note", ""),
            "event_seq": seq,
        }
        # 放行类回到运输中；报废则交拒收/替代流程处理
        if p["result"] in ("released", "released_with_note", "diverted"):
            snap.shipment_status = ShipmentStatus.IN_TRANSIT

    elif et == "shipment_delivered":
        snap.shipment_status = ShipmentStatus.DELIVERED

    elif et == "product_accepted":
        snap.shipment_status = ShipmentStatus.ACCEPTED
        snap.accepted = {"by": p["by"], "at_utc": p["at_utc"],
                         "note": p.get("note", "")}

    elif et == "product_rejected":
        snap.shipment_status = ShipmentStatus.REJECTED
        snap.accepted = {"by": p["by"], "at_utc": p["at_utc"],
                         "reason": p.get("reason", "")}

    elif et == "infusion_completed":
        snap.status = CaseStatus.INFUSED
        snap.infusion = {"at_utc": p["at_utc"], "operator": p.get("operator", ""),
                         "effective_consent_version": p.get("effective_consent_version")}

    elif et == "case_cancelled":
        snap.status = CaseStatus.CANCELLED
        snap.cancel = {"reason": p.get("reason", ""), "by": p.get("by", ""),
                       "at_utc": p["at_utc"], "stage": p.get("stage", "")}

    elif et == "donor_swapped":
        snap.status = CaseStatus.SUPERSEDED
        snap.supersede = {"replacement_case_id": p["replacement_case_id"],
                          "reason": p.get("reason", ""), "at_utc": p["at_utc"]}

    elif et == "case_note":
        snap.notes.append({"at_utc": event.timestamp, "by": event.actor_party,
                           "text": p.get("text", "")})

    # 通知类事件不改变病例主状态（在投影中由通知模块单独读取）


def replay(events) -> CaseSnapshot:
    events = sorted(events, key=lambda e: (e.seq, e.timestamp))
    snap: CaseSnapshot | None = None
    for e in events:
        if e.event_type == "case_opened":
            snap = CaseSnapshot(case_id=e.case_id)
        if snap is None:
            continue
        apply_event(snap, e)
    if snap is None:
        raise IllegalTransition("该病例尚无 case_opened 事件")
    return snap
