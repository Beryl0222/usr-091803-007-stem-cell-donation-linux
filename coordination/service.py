"""应用服务：命令处理、状态机守卫、同意版本规则与通知副作用。

所有写操作都：
1. 重放得到当前快照 -> 校验前置条件（状态、有效同意版本、窗口确认）；
2. 向仅追加台账写一条领域事件；
3. 重放后按"谁有待办"派生目标，写通知事件并经发送器投递。

外部回调（温控读数、短信送达、医院系统确认）携带 external_source/event_id，
重复到达时命中幂等去重，直接返回首次结果，绝不二次推进。
"""

import secrets
from datetime import datetime

from .clock import UTC, Window, now_utc, to_instant, format_local, parse_tz
from .events import Ledger, DuplicateExternalEvent
from .models import (
    Actor, CaseStatus, ConsentAction, ConsentState, HandoverKind,
    ProductType, ShipmentStatus, ExcursionResult,
)
from .projection import replay, IllegalTransition
from .notifications import derive_targets, build_notification_projection


class CommandError(Exception):
    """命令被拒绝（状态不允许/前置条件缺失/参数非法）。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class SmsChannel:
    """短信/消息通道抽象。生产实现对接网关；测试用假通道。

    返回 (external_message_id, ok, detail)。
    """

    def send(self, *, party_id, role, kind, urgency, vars_) -> tuple:
        raise NotImplementedError


class StubSmsChannel(SmsChannel):
    """内存假通道：记录每次发送，默认成功。可用 failures 模拟失败。"""

    def __init__(self, fail: set | None = None):
        self.sent = []
        self._fail = fail or set()

    def send(self, *, party_id, role, kind, urgency, vars_):
        ext = f"extmsg-{len(self.sent) + 1:06d}"
        ok = kind not in self._fail
        self.sent.append({"external_message_id": ext, "party_id": party_id,
                          "role": role, "kind": kind, "urgency": urgency,
                          "vars": vars_, "ok": ok})
        return ext, ok, "stub"


class CoordinationService:
    def __init__(self, ledger: Ledger, channel: SmsChannel | None = None,
                 clock=now_utc):
        self.ledger = ledger
        self.channel = channel or StubSmsChannel()
        self.clock = clock

    # ===================================================================
    # 内部辅助
    # ===================================================================
    def _snap(self, case_id: str):
        try:
            return replay(self.ledger.events_for(case_id))
        except IllegalTransition:
            raise CommandError(f"病例不存在: {case_id}")

    def _require_status(self, snap, allowed, verb: str):
        if snap.status not in allowed:
            raise CommandError(
                f"当前状态 {snap.status.value} 不允许 {verb}")

    def _require_consent_granted(self, snap, verb: str):
        if snap.consent_state != ConsentState.GRANTED:
            raise CommandError(f"{verb} 需要当前有效的知情同意"
                               f"（当前: {snap.consent_state.value}）")
        return snap.effective_consent

    def _append(self, case_id, event_type, payload, actor, **kw):
        """追加事件；命中外部幂等去重时原样抛给上层命令处理。"""
        return self.ledger.append(
            case_id=case_id, event_type=event_type, payload=payload,
            actor=actor, timestamp=kw.pop("timestamp", self.clock()), **kw)

    def _emit_notifications(self, case_id, source_event, coordinator_actor):
        """领域事件落账后派生并投递通知。通知派生本身是确定性的。"""
        snap = self._snap(case_id)
        targets = derive_targets(snap, source_event)
        created = []
        for t in targets:
            notification_id = f"N{source_event.seq}-{t.party_id}"
            ev = self._append(case_id, "notification_created", {
                "notification_id": notification_id,
                "party_id": t.party_id, "role": t.role.value,
                "kind": t.kind, "needs_action": t.needs_action,
                "urgency": t.urgency, "vars": t.vars,
                "source_event_seq": source_event.seq,
            }, coordinator_actor, idem_key=f"notif:{notification_id}")
            created.append(ev)
            self._dispatch(case_id, notification_id, t, coordinator_actor)
        return created

    def _dispatch(self, case_id, notification_id, target, actor):
        try:
            ext_id, ok, detail = self.channel.send(
                party_id=target.party_id, role=target.role.value,
                kind=target.kind, urgency=target.urgency, vars_=target.vars)
        except Exception as exc:  # 通道异常不影响领域事件，记失败可重发
            ext_id, ok, detail = "", False, str(exc)
        if ok:
            self._append(case_id, "notification_sent", {
                "notification_id": notification_id, "channel": "sms",
                "external_message_id": ext_id, "detail": detail,
                "at_utc": self.clock().astimezone(UTC).isoformat(),
            }, actor, idem_key=f"send:{notification_id}:{ext_id}")
        else:
            self._append(case_id, "notification_failed", {
                "notification_id": notification_id, "channel": "sms",
                "detail": detail,
                "at_utc": self.clock().astimezone(UTC).isoformat(),
            }, actor, idem_key=f"fail:{notification_id}:{secrets.token_hex(2)}")

    # ===================================================================
    # 1) 配型
    # ===================================================================
    def open_case(self, actor: Actor, *, donor_identity: dict,
                  recipient_identity: dict, parties: dict,
                  product_type: str = ProductType.PBSC.value,
                  timezones: dict | None = None,
                  case_id: str = "", idem_key: str = ""):
        case_id = case_id or _new_id("CASE")
        tz = timezones or {}
        payload = {
            "donor_identity": donor_identity,
            "recipient_identity": recipient_identity,
            "product_type": product_type,
            "donor_party_id": parties["donor"],
            "recipient_party_id": parties["recipient_hospital"],
            "donor_center_party_id": parties["donor_center"],
            "courier_party_id": parties["courier"],
            "coordinator_party_id": parties.get("coordinator", actor.party_id),
            "donor_tz": tz.get("donor", "Asia/Shanghai"),
            "recipient_tz": tz.get("recipient_hospital", "Asia/Shanghai"),
            "donor_center_tz": tz.get("donor_center", "Asia/Shanghai"),
            "courier_tz": tz.get("courier", "Asia/Shanghai"),
        }
        for name in payload["donor_tz"], payload["recipient_tz"], \
                  payload["donor_center_tz"], payload["courier_tz"]:
            parse_tz(name)
        if product_type not in (ProductType.PBSC.value, ProductType.MARROW.value):
            raise CommandError(f"未知采集物类型: {product_type}")
        ev = self._append(case_id, "case_opened", payload, actor,
                          idem_key=idem_key or case_id)
        self._emit_notifications(case_id, ev, actor)
        return case_id

    # ===================================================================
    # 2) 筛查
    # ===================================================================
    def start_screening(self, actor, case_id, *, arrangements="", idem_key=""):
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.MATCHED}, "启动筛查")
        ev = self._append(case_id, "screening_started",
                          {"arrangements": arrangements}, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def pass_screening(self, actor, case_id, *, idem_key=""):
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.SCREENING}, "筛查通过")
        ev = self._append(case_id, "screening_passed", {}, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    # ===================================================================
    # 3) 同意（版本化：只追加新版本，永不改写）
    # ===================================================================
    def grant_consent(self, actor, case_id, *, document_version: str,
                      document_hash: str, signed_local_iso: str,
                      witness_party: str = "", statement: str = "",
                      idem_key=""):
        snap = self._snap(case_id)
        self._require_status(
            snap,
            {CaseStatus.CONSENT_PENDING, CaseStatus.CONSENTED, CaseStatus.SCHEDULED},
            "签署同意")
        if snap.consent_state == ConsentState.GRANTED:
            raise CommandError("已存在生效同意；不能重复签署或直接改写，"
                               "如需变更须先撤回再签署新版本")
        version = len(snap.consents) + 1
        signed_at = to_instant(signed_local_iso, snap.donor_tz)
        ev = self._append(case_id, "consent_granted", {
            "version": version, "action": ConsentAction.GRANT.value,
            "document_version": document_version, "document_hash": document_hash,
            "signed_at_utc": signed_at.isoformat(),
            "signed_local": format_local(signed_at, snap.donor_tz),
            "tz": snap.donor_tz, "witness_party": witness_party,
            "statement": statement or "本人已知情并自愿同意捐献造血干细胞",
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev, version

    def withdraw_consent(self, actor, case_id, *, document_version: str,
                         document_hash: str, reason: str = "",
                         signed_local_iso: str | None = None, idem_key=""):
        """撤回：不删除/不改写已确认同意，而是追加 withdraw 新版本。"""
        snap = self._snap(case_id)
        # 采集开始后不再接受撤回（物理节点已发生）
        self._require_status(
            snap,
            {CaseStatus.CONSENT_PENDING, CaseStatus.CONSENTED, CaseStatus.SCHEDULED},
            "撤回同意")
        if snap.consent_state != ConsentState.GRANTED:
            raise CommandError("没有可撤回的生效同意")
        version = len(snap.consents) + 1
        signed_at = (to_instant(signed_local_iso, snap.donor_tz)
                     if signed_local_iso else self.clock())
        ev = self._append(case_id, "consent_withdrawn", {
            "version": version, "action": ConsentAction.WITHDRAW.value,
            "document_version": document_version, "document_hash": document_hash,
            "reason": reason,
            "signed_at_utc": signed_at.astimezone(UTC).isoformat(),
            "signed_local": format_local(signed_at, snap.donor_tz),
            "tz": snap.donor_tz,
            "statement": "本人撤回前述知情同意",
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev, version

    # ===================================================================
    # 4) 排期 / 改期
    # ===================================================================
    def schedule_collection(self, actor, case_id, *, start_local_iso: str,
                            end_local_iso: str, tz: str | None = None,
                            reason: str = "", idem_key=""):
        snap = self._snap(case_id)
        self._require_status(
            snap, {CaseStatus.CONSENTED, CaseStatus.SCHEDULED}, "安排/改期采集")
        self._require_consent_granted(snap, "安排采集")
        tz = tz or snap.donor_center_tz
        window = Window.from_local(start_local_iso, end_local_iso, tz)
        version = len(snap.schedules) + 1
        ev = self._append(case_id, "collection_scheduled", {
            "schedule_version": version,
            "start_utc": window.start.isoformat(),
            "end_utc": window.end.isoformat(),
            "declared_tz": tz,
            "reason": reason or ("初次排期" if version == 1 else "改期"),
            "proposed_by": actor.party_id,
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev, version

    def confirm_schedule(self, actor, case_id, *, party_id: str,
                         external_source="", external_event_id="", idem_key=""):
        """一方确认窗口。医院系统回调可用外部事件 ID 保证只确认一次。"""
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.SCHEDULED}, "确认窗口")
        sch = snap.current_schedule
        if party_id in sch.confirmations:
            # 重复确认是良性重放：返回既有事实，不写事件、不重复通知。
            return ("already_confirmed", sch.confirmations[party_id])
        tz = snap.party_tz(party_id)
        instant = self.clock()
        try:
            ev = self._append(case_id, "schedule_confirmed", {
                "party_id": party_id,
                "at_utc": instant.astimezone(UTC).isoformat(),
                "local": format_local(instant, tz), "tz": tz,
            }, actor, external_source=external_source,
               external_event_id=external_event_id, idem_key=idem_key)
        except DuplicateExternalEvent as dup:
            return ("duplicate_external", dup.first)
        self._emit_notifications(case_id, ev, actor)
        return ("confirmed", ev)

    # ===================================================================
    # 5) 采集
    # ===================================================================
    def complete_collection(self, actor, case_id, *, product_id: str,
                            volume_ml: int | None = None, idem_key=""):
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.SCHEDULED}, "完成采集")
        self._require_consent_granted(snap, "采集")
        if not snap.all_confirmed():
            pending = [r.value for _, r in snap.unconfirmed_parties()]
            raise CommandError(f"采集窗口尚未经各方确认，缺: {pending}")
        consent = snap.effective_consent
        at = self.clock()
        ev = self._append(case_id, "collection_completed", {
            "product_id": product_id, "volume_ml": volume_ml,
            "at_utc": at.astimezone(UTC).isoformat(),
            # 关键：采集发生时固化所采用的同意版本，日后撤回不改变此事实
            "effective_consent_version": consent.version,
            "consent_document_hash": consent.document_hash,
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    # ===================================================================
    # 6) 样本交接（双人双签，记录交接人）
    # ===================================================================
    def handover(self, actor, case_id, *, kind: str, from_person: str,
                 to_person: str, product_temp_c: float, container_id: str,
                 evidence_ref: str, idem_key=""):
        snap = self._snap(case_id)
        if kind == HandoverKind.COLLECTION_TO_COURIER.value:
            self._require_status(snap, {CaseStatus.COLLECTED}, "采集物移交承运方")
            expected_from, expected_to = snap.donor_center_party_id, snap.courier_party_id
        elif kind == HandoverKind.COURIER_TO_RECIPIENT.value:
            self._require_status(snap, {CaseStatus.IN_TRANSIT}, "承运方送达受者医院")
            if snap.shipment_status != ShipmentStatus.IN_TRANSIT:
                raise CommandError("货物处于待处置状态，不能交接")
            expected_from, expected_to = snap.courier_party_id, snap.recipient_party_id
        else:
            raise CommandError(f"未知交接类型: {kind}")
        consent = self._require_consent_granted(snap, "交接")
        at = self.clock()
        ev = self._append(case_id, "handover", {
            "kind": kind,
            "from_party": expected_from, "from_person": from_person,
            "to_party": expected_to, "to_person": to_person,
            "product_temp_c": product_temp_c, "container_id": container_id,
            "evidence_ref": evidence_ref,
            "at_utc": at.astimezone(UTC).isoformat(),
            "effective_consent_version": consent.version,
        }, actor, idem_key=idem_key)
        if kind == HandoverKind.COLLECTION_TO_COURIER.value:
            self._emit_notifications(case_id, ev, actor)
        return ev

    # ===================================================================
    # 7) 温控运输
    # ===================================================================
    def report_excursion(self, actor, case_id, *, temp_c: float,
                         limit_low_c: float, limit_high_c: float,
                         reading_local_iso: str,
                         external_source: str, external_event_id: str):
        """温控设备/承运系统上报异常。外部事件重复到达只处理一次。"""
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.IN_TRANSIT}, "上报温控异常")
        if limit_low_c <= temp_c <= limit_high_c:
            raise CommandError("读数仍在允许区间内，不构成温控异常")
        at = to_instant(reading_local_iso, snap.courier_tz)
        payload = {
            "temp_c": temp_c, "limit_low_c": limit_low_c,
            "limit_high_c": limit_high_c,
            "detected_utc": at.isoformat(),
            "reading_local": format_local(at, snap.courier_tz),
            "tz": snap.courier_tz, "reported_by": actor.party_id,
        }
        try:
            ev = self._append(case_id, "shipment_excursion", payload, actor,
                              external_source=external_source,
                              external_event_id=external_event_id)
        except DuplicateExternalEvent as dup:
            return ("duplicate_external", dup.first)
        self._emit_notifications(case_id, ev, actor)
        return ("recorded", ev)

    def resolve_excursion(self, actor, case_id, *, result: str,
                          note: str = "", idem_key=""):
        snap = self._snap(case_id)
        if snap.shipment_status != ShipmentStatus.QUARANTINED:
            raise CommandError("当前没有待处置的温控异常")
        valid = {r.value for r in ExcursionResult} - {ExcursionResult.OPEN.value}
        if result not in valid:
            raise CommandError(f"处置结果必须是: {sorted(valid)}")
        at = self.clock()
        ev = self._append(case_id, "excursion_resolved", {
            "result": result, "decided_by": actor.party_id,
            "decided_utc": at.astimezone(UTC).isoformat(), "note": note,
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def deliver(self, actor, case_id, *, idem_key=""):
        snap = self._snap(case_id)
        self._require_status(snap, {CaseStatus.IN_TRANSIT}, "送达登记")
        ev = self._append(case_id, "shipment_delivered",
                          {"at_utc": self.clock().astimezone(UTC).isoformat()},
                          actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def accept_product(self, actor, case_id, *, by_person: str,
                       note: str = "", idem_key=""):
        snap = self._snap(case_id)
        if snap.shipment_status != ShipmentStatus.DELIVERED:
            raise CommandError("仅已送达待核验的货物可签收")
        at = self.clock()
        ev = self._append(case_id, "product_accepted", {
            "by": by_person, "at_utc": at.astimezone(UTC).isoformat(),
            "note": note,
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def reject_product(self, actor, case_id, *, by_person: str,
                       reason: str, idem_key=""):
        snap = self._snap(case_id)
        if snap.shipment_status != ShipmentStatus.DELIVERED:
            raise CommandError("仅已送达待核验的货物可拒收")
        ev = self._append(case_id, "product_rejected", {
            "by": by_person, "reason": reason,
            "at_utc": self.clock().astimezone(UTC).isoformat(),
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    # ===================================================================
    # 8) 回输
    # ===================================================================
    def complete_infusion(self, actor, case_id, *, operator: str, idem_key=""):
        snap = self._snap(case_id)
        if snap.shipment_status != ShipmentStatus.ACCEPTED:
            raise CommandError("货物须经受者医院签收后方可回输")
        self._require_status(snap, {CaseStatus.IN_TRANSIT}, "登记回输")
        at = self.clock()
        ev = self._append(case_id, "infusion_completed", {
            "operator": operator, "at_utc": at.astimezone(UTC).isoformat(),
            "product_id": snap.product["product_id"],
            "effective_consent_version": snap.product["effective_consent_version"],
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    # ===================================================================
    # 取消 / 替代供者
    # ===================================================================
    def cancel_case(self, actor, case_id, *, reason: str,
                    cause_role: str = "", stage: str = "", idem_key=""):
        snap = self._snap(case_id)
        terminal = {CaseStatus.CANCELLED, CaseStatus.INFUSED, CaseStatus.SUPERSEDED}
        self._require_status(snap, set(CaseStatus) - terminal, "取消病例")
        ev = self._append(case_id, "case_cancelled", {
            "reason": reason, "cause_role": cause_role,
            "stage": stage or snap.status.value,
            "by": actor.party_id,
            "at_utc": self.clock().astimezone(UTC).isoformat(),
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def swap_donor(self, actor, case_id, *, reason: str,
                   replacement_case_id: str, idem_key=""):
        """启动替代供者：原病例归档为 superseded，仅通知需善后的方。"""
        snap = self._snap(case_id)
        terminal = {CaseStatus.CANCELLED, CaseStatus.INFUSED, CaseStatus.SUPERSEDED}
        self._require_status(snap, set(CaseStatus) - terminal, "替代供者")
        if replacement_case_id == case_id:
            raise CommandError("替代病例不能是自身")
        ev = self._append(case_id, "donor_swapped", {
            "replacement_case_id": replacement_case_id, "reason": reason,
            "at_utc": self.clock().astimezone(UTC).isoformat(),
        }, actor, idem_key=idem_key)
        self._emit_notifications(case_id, ev, actor)
        return ev

    def add_note(self, actor, case_id, text: str, idem_key=""):
        return self._append(case_id, "case_note", {"text": text}, actor,
                            idem_key=idem_key)

    # ===================================================================
    # 通知重发与送达回执
    # ===================================================================
    def resend_notification(self, actor, notification_id: str):
        """通知允许重发：每次尝试都留痕。"""
        proj = build_notification_projection(self.ledger.all_events())
        n = proj.get(notification_id)
        if not n:
            raise CommandError(f"通知不存在: {notification_id}")
        ext_id, ok, detail = self.channel.send(
            party_id=n.party_id, role=n.role, kind=n.kind,
            urgency=n.urgency, vars_=n.vars)
        attempt = len(n.attempts) + 1
        if ok:
            ev = self._append(n.case_id, "notification_resent", {
                "notification_id": notification_id, "channel": "sms",
                "external_message_id": ext_id, "detail": detail,
                "attempt": attempt,
                "at_utc": self.clock().astimezone(UTC).isoformat(),
            }, actor, idem_key=f"resend:{notification_id}:{attempt}")
        else:
            ev = self._append(n.case_id, "notification_failed", {
                "notification_id": notification_id, "channel": "sms",
                "detail": detail, "attempt": attempt,
                "at_utc": self.clock().astimezone(UTC).isoformat(),
            }, actor, idem_key=f"resendfail:{notification_id}:{attempt}")
        return ev, ok

    def record_delivery_receipt(self, actor, *, external_message_id: str,
                                external_source: str, external_event_id: str,
                                delivered_local_iso: str | None = None):
        """短信网关送达回调。重复回调只推进一次送达状态。"""
        proj = build_notification_projection(self.ledger.all_events())
        n = proj.by_external_message(external_message_id)
        if not n:
            raise CommandError(f"找不到对应消息: {external_message_id}")
        tz = self._snap(n.case_id).party_tz(n.party_id)
        at = (to_instant(delivered_local_iso, tz) if delivered_local_iso
              else self.clock())
        payload = {"notification_id": n.notification_id,
                   "external_message_id": external_message_id,
                   "at_utc": at.astimezone(UTC).isoformat(),
                   "local": format_local(at, tz), "tz": tz}
        try:
            ev = self._append(n.case_id, "notification_delivered", payload, actor,
                              external_source=external_source,
                              external_event_id=external_event_id)
        except DuplicateExternalEvent as dup:
            return ("duplicate_external", dup.first)
        return ("delivered", ev)

    # ===================================================================
    # 查询
    # ===================================================================
    def get_case(self, case_id: str):
        return self._snap(case_id)

    def list_cases(self):
        out = []
        for cid in self.ledger.case_ids():
            try:
                out.append(self._snap(cid))
            except CommandError:
                continue
        return out
