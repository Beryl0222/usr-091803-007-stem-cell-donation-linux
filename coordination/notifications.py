"""通知路由、重发与送达回执。

核心策略：通知对象 = 因该事件而产生"待办动作"的参与方。
- 配型成功：只通知供者侧医院与受者医院启动各自准备，承运方在排期前不被打扰。
- 排期/改期：只通知需要重新确认窗口的四方，各方收到的是自己所在地时间。
- 撤回同意：捐献者本人是动作发出者，不再通知本人；按所处阶段通知需善后的方。
- 途中温控异常：只通知承运方（处置）、协调员（决策）、受者医院（暂缓清髓/回输）；
  供者与采集医院无可执行动作，不通知。
- 放行/报废结论：只通知结论的执行方。

通知本身也是台账事件，可重发、记录每次尝试与送达回执；
回执经外部事件幂等去重，重复回调不会重复改变送达状态。
"""

from dataclasses import dataclass, field
from enum import Enum

from .models import (
    HandoverKind, Role, NotificationStatus,
)


class NotifyKind(str, Enum):
    WORKUP = "workup"                       # 启动配型后准备
    SCREENING_ARRANGE = "screening_arrange"
    SIGN_CONSENT = "sign_consent"
    PROCEED_SCHEDULING = "proceed_scheduling"
    CONSENT_WITHDRAWN_DISPOSITION = "consent_withdrawn_disposition"
    CONFIRM_WINDOW = "confirm_window"       # 需确认/重新确认窗口
    WINDOW_LOCKED = "window_locked"
    PREPARE_HANDOVER = "prepare_handover"
    PREPARE_RECEIPT = "prepare_receipt"
    EXCURSION_HANDLE = "excursion_handle"   # 承运方立即处置
    EXCURSION_DECIDE = "excursion_decide"   # 协调员评估决策
    EXCURSION_HOLD_INFUSION = "hold_infusion"  # 受者医院暂缓预处理/回输
    EXCURSION_RELEASED = "excursion_released"
    EXCURSION_DISCARDED = "excursion_discarded"
    VERIFY_AND_ACCEPT = "verify_and_accept"
    PRODUCT_REJECTED = "product_rejected"
    CASE_CANCELLED = "case_cancelled"
    DONOR_SWAPPED = "donor_swapped"
    INFUSION_DONE = "infusion_done"
    CASE_NOTE = "case_note"


@dataclass
class Target:
    party_id: str
    role: Role
    kind: str
    needs_action: bool
    urgency: str = "normal"       # normal | urgent
    vars: dict = field(default_factory=dict)


def _party(snap, role: Role):
    return {
        Role.DONOR: snap.donor_party_id,
        Role.DONOR_CENTER: snap.donor_center_party_id,
        Role.COURIER: snap.courier_party_id,
        Role.RECIPIENT_HOSPITAL: snap.recipient_party_id,
        Role.COORDINATOR: snap.coordinator_party_id,
    }.get(role, "")


def _window_vars(snap, party_id: str) -> dict:
    sch = snap.current_schedule
    tz = snap.party_tz(party_id)
    v = sch.window.view(tz)
    v["schedule_version"] = sch.schedule_version
    v["reason"] = sch.reason
    return v


def derive_targets(snap, event) -> list:
    """根据事件应用后的快照，推导需要通知的人。无待办则返回空列表。"""
    et = event.event_type
    p = event.payload
    targets = []

    def add(role, kind, *, needs_action=True, urgency="normal", vars_=None):
        pid = _party(snap, role)
        if pid:
            targets.append(Target(pid, role, kind if isinstance(kind, str) else kind.value,
                                  needs_action, urgency, vars_ or {}))

    if et == "case_opened":
        add(Role.DONOR_CENTER, NotifyKind.WORKUP,
            vars_={"side": "donor"})
        add(Role.RECIPIENT_HOSPITAL, NotifyKind.WORKUP,
            vars_={"side": "recipient"})

    elif et == "screening_started":
        add(Role.DONOR, NotifyKind.SCREENING_ARRANGE,
            vars_={"arrangements": p.get("arrangements", "")})
        add(Role.DONOR_CENTER, NotifyKind.SCREENING_ARRANGE, needs_action=True)

    elif et == "screening_passed":
        add(Role.DONOR, NotifyKind.SIGN_CONSENT, urgency="urgent")
        add(Role.COORDINATOR, NotifyKind.SIGN_CONSENT, needs_action=True,
            vars_={"follow_up": "见证并回收同意书"})

    elif et == "consent_granted":
        add(Role.COORDINATOR, NotifyKind.PROCEED_SCHEDULING)

    elif et == "consent_withdrawn":
        # 本人不通知。按阶段只通知有待办的一方：
        add(Role.COORDINATOR, NotifyKind.CONSENT_WITHDRAWN_DISPOSITION,
            urgency="urgent", vars_={"reason": p.get("reason", "")})
        add(Role.DONOR_CENTER, NotifyKind.CONSENT_WITHDRAWN_DISPOSITION,
            vars_={"action": "终止供者流程"})
        if snap.current_schedule is not None:
            # 已排期：运力与床位需要释放
            add(Role.COURIER, NotifyKind.CONSENT_WITHDRAWN_DISPOSITION,
                vars_={"action": "释放已排运力"})
            add(Role.RECIPIENT_HOSPITAL, NotifyKind.CONSENT_WITHDRAWN_DISPOSITION,
                urgency="urgent", vars_={"action": "调整受者预处理与床位"})

    elif et == "collection_scheduled":
        for role in (Role.DONOR, Role.DONOR_CENTER, Role.COURIER,
                     Role.RECIPIENT_HOSPITAL):
            pid = _party(snap, role)
            if pid:
                targets.append(Target(
                    pid, role, NotifyKind.CONFIRM_WINDOW.value, True, "urgent",
                    _window_vars(snap, pid)))

    elif et == "schedule_confirmed":
        # 协调员需要盯齐剩余确认
        add(Role.COORDINATOR, NotifyKind.CONFIRM_WINDOW, needs_action=not snap.all_confirmed(),
            vars_={"confirmed_by": p["party_id"],
                   "pending": [pid for pid, _ in snap.unconfirmed_parties()]})
        if snap.all_confirmed():
            for role in (Role.DONOR, Role.DONOR_CENTER, Role.COURIER,
                         Role.RECIPIENT_HOSPITAL):
                pid = _party(snap, role)
                if pid:
                    targets.append(Target(
                        pid, role, NotifyKind.WINDOW_LOCKED.value, True, "normal",
                        _window_vars(snap, pid)))

    elif et == "collection_completed":
        add(Role.COURIER, NotifyKind.PREPARE_HANDOVER, urgency="urgent",
            vars_={"product_id": snap.product["product_id"]})
        add(Role.RECIPIENT_HOSPITAL, NotifyKind.PREPARE_RECEIPT,
            vars_={"product_id": snap.product["product_id"]})

    elif et == "handover":
        if p["kind"] == HandoverKind.COLLECTION_TO_COURIER.value:
            add(Role.RECIPIENT_HOSPITAL, NotifyKind.PREPARE_RECEIPT, urgency="urgent")
            add(Role.COORDINATOR, NotifyKind.PREPARE_RECEIPT, needs_action=False)

    elif et == "shipment_excursion":
        # 只通知有处置职责的三方；供者/采集医院无动作，不通知。
        add(Role.COURIER, NotifyKind.EXCURSION_HANDLE, urgency="urgent",
            vars_={"temp_c": p["temp_c"], "low": p["limit_low_c"],
                   "high": p["limit_high_c"], "reading_local": p["reading_local"]})
        add(Role.COORDINATOR, NotifyKind.EXCURSION_DECIDE, urgency="urgent")
        add(Role.RECIPIENT_HOSPITAL, NotifyKind.EXCURSION_HOLD_INFUSION,
            urgency="urgent", vars_={"instruction": "在放行结论前暂缓清髓预处理与回输"})

    elif et == "excursion_resolved":
        result = p["result"]
        if result in ("released", "released_with_note"):
            add(Role.RECIPIENT_HOSPITAL, NotifyKind.EXCURSION_RELEASED,
                urgency="urgent", vars_={"note": p.get("note", "")})
            add(Role.COURIER, NotifyKind.EXCURSION_RELEASED,
                vars_={"action": "继续运输"})
        elif result == "diverted":
            add(Role.COURIER, NotifyKind.EXCURSION_RELEASED,
                vars_={"action": "按改送目的地执行", "note": p.get("note", "")})
            add(Role.RECIPIENT_HOSPITAL, NotifyKind.EXCURSION_HOLD_INFUSION,
                vars_={"instruction": "货物改送，等待进一步安排"})
        elif result == "discarded":
            add(Role.COORDINATOR, NotifyKind.EXCURSION_DISCARDED, urgency="urgent",
                vars_={"action": "启动替代供者或重排"})
            add(Role.RECIPIENT_HOSPITAL, NotifyKind.EXCURSION_DISCARDED,
                urgency="urgent", vars_={"action": "停止本次回输计划"})
            add(Role.DONOR_CENTER, NotifyKind.EXCURSION_DISCARDED,
                vars_={"action": "评估再次采集可行性"})
            add(Role.COURIER, NotifyKind.EXCURSION_DISCARDED,
                vars_={"action": "按处置单退回/销毁货物"})

    elif et == "shipment_delivered":
        add(Role.RECIPIENT_HOSPITAL, NotifyKind.VERIFY_AND_ACCEPT, urgency="urgent")

    elif et == "product_accepted":
        add(Role.COORDINATOR, NotifyKind.VERIFY_AND_ACCEPT, needs_action=False)
        add(Role.DONOR_CENTER, NotifyKind.VERIFY_AND_ACCEPT, needs_action=False,
            vars_={"closure": "采集物已被受者医院签收"})

    elif et == "product_rejected":
        add(Role.COORDINATOR, NotifyKind.PRODUCT_REJECTED, urgency="urgent",
            vars_={"reason": p.get("reason", "")})
        add(Role.COURIER, NotifyKind.PRODUCT_REJECTED,
            vars_={"action": "按退回/处置指令执行"})

    elif et == "infusion_completed":
        add(Role.COORDINATOR, NotifyKind.INFUSION_DONE, needs_action=False)
        add(Role.DONOR_CENTER, NotifyKind.INFUSION_DONE, needs_action=False)
        add(Role.DONOR, NotifyKind.INFUSION_DONE, needs_action=False,
            vars_={"message": "您的捐献已完成救治，谨致谢意（供患双盲）"})

    elif et == "case_cancelled":
        # 排除取消原因的造成方本人，按阶段通知需善后的方
        cause_role = p.get("cause_role", "")
        stage = p.get("stage", "")
        for role, action in [
            (Role.COORDINATOR, "归档并决定是否启动替代供者"),
            (Role.DONOR_CENTER, "终止供者侧流程"),
            (Role.COURIER, "释放运力" if stage in ("scheduled", "collected",
                                                    "in_transit") else None),
            (Role.RECIPIENT_HOSPITAL, "调整受者治疗与床位"),
        ]:
            if action is None or role.value == cause_role:
                continue
            add(role, NotifyKind.CASE_CANCELLED,
                urgency="urgent" if role == Role.COORDINATOR else "normal",
                vars_={"reason": p.get("reason", ""), "action": action})

    elif et == "donor_swapped":
        for role, action in [
            (Role.DONOR_CENTER, "原供者流程归档，准备承接替代供者"),
            (Role.COURIER, "原运力安排作废"),
            (Role.RECIPIENT_HOSPITAL, "等待替代供者病例排期"),
        ]:
            add(role, NotifyKind.DONOR_SWAPPED,
                vars_={"replacement_case_id": p.get("replacement_case_id"),
                       "action": action})

    return targets


# ---------------------------------------------------------------------------
# 通知投影与发送器

@dataclass
class Notification:
    notification_id: str
    case_id: str
    party_id: str
    role: str
    kind: str
    needs_action: bool
    urgency: str
    vars: dict
    source_event_seq: int
    status: str = NotificationStatus.PENDING.value
    attempts: list = field(default_factory=list)   # [{at, channel, result, detail}]
    delivered_at: str = ""
    external_message_id: str = ""


class NotificationProjection:
    """从台账事件还原所有通知及其送达状态。"""

    def __init__(self):
        self._items: dict[str, Notification] = {}
        self._by_ext_msg: dict[str, str] = {}

    def apply(self, e):
        p = e.payload
        if e.event_type == "notification_created":
            n = Notification(
                notification_id=p["notification_id"], case_id=e.case_id,
                party_id=p["party_id"], role=p["role"], kind=p["kind"],
                needs_action=p.get("needs_action", True),
                urgency=p.get("urgency", "normal"), vars=p.get("vars", {}),
                source_event_seq=p["source_event_seq"])
            self._items[n.notification_id] = n
            if p.get("external_message_id"):
                self._by_ext_msg[p["external_message_id"]] = n.notification_id
        elif e.event_type in ("notification_sent", "notification_resent"):
            n = self._items.get(p["notification_id"])
            if n:
                n.attempts.append({"at_utc": p["at_utc"], "channel": p.get("channel", "sms"),
                                   "result": "sent", "detail": p.get("detail", ""),
                                   "external_message_id": p.get("external_message_id", "")})
                # 外部消息 ID 在发送成功后才产生，于此登记以便送达回执反查
                if p.get("external_message_id"):
                    self._by_ext_msg[p["external_message_id"]] = n.notification_id
                if n.status != NotificationStatus.DELIVERED.value:
                    n.status = NotificationStatus.SENT.value
        elif e.event_type == "notification_failed":
            n = self._items.get(p["notification_id"])
            if n:
                n.attempts.append({"at_utc": p["at_utc"], "channel": p.get("channel", "sms"),
                                   "result": "failed", "detail": p.get("detail", "")})
                n.status = NotificationStatus.FAILED.value
        elif e.event_type == "notification_delivered":
            n = self._items.get(p["notification_id"])
            if n:
                n.status = NotificationStatus.DELIVERED.value
                n.delivered_at = p["at_utc"]

    def all(self) -> list:
        return list(self._items.values())

    def for_case(self, case_id) -> list:
        return [n for n in self._items.values() if n.case_id == case_id]

    def for_party(self, party_id) -> list:
        return [n for n in self._items.values() if n.party_id == party_id]

    def get(self, notification_id) -> Notification:
        return self._items.get(notification_id)

    def by_external_message(self, external_message_id) -> Notification:
        nid = self._by_ext_msg.get(external_message_id)
        return self._items.get(nid) if nid else None


def build_notification_projection(events) -> NotificationProjection:
    proj = NotificationProjection()
    for e in sorted(events, key=lambda x: x.seq):
        proj.apply(e)
    return proj
