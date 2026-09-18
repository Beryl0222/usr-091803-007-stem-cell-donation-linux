"""通知投递：定向生成、可重发、送达有回执。

- 首次生成即记录一次发送尝试（通道按人员可用通道选择，缺省短信）。
- 送达状态由外部回执事件 EV_DELIVERY_RECEIPT 翻转，重发不重置已送达事实。
- 被受众策略抑制的通知同样落库（suppressed=True），便于审计“为何没打扰某人”。
"""

from . import states as S
from .clock import iso
from .routing import Audience

# template -> (主题, 正文模板)。{xxx} 占位由 context 填充。
TEMPLATES = {
    "match_success_coord": ("配型成功通知｜{code}", "病例 {code} 非血缘配型成功。{reason}。"),
    "match_success_donor_affairs": ("配型成功｜{code}", "病例 {code}：{reason}。"),
    "match_success_physician": ("配型成功（医疗知会）｜{code}", "病例 {code}：{reason}。"),
    "screening_coord": ("筛查结果｜{code}", "{summary}。{reason}。"),
    "screening_pass_physician": ("供者筛查通过｜{code}", "病例 {code}：{reason}。"),
    "screening_fail_donor_affairs": ("供者筛查未过｜{code}", "病例 {code}：{reason}。"),
    "screening_fail_physician": ("供者筛查未过（预警）｜{code}", "病例 {code}：{reason}。"),
    "consent_granted_coord": ("供者同意已确认｜{code}", "病例 {code} 同意版本 v{version}：{reason}。"),
    "consent_granted_physician": ("供者同意已确认｜{code}", "病例 {code}：{reason}。"),
    "consent_withdrawn_coord": ("供者撤回同意（紧急）｜{code}", "病例 {code} 同意撤回版本 v{version}：{reason}。"),
    "consent_withdrawn_donor_affairs": ("供者撤回同意｜{code}", "病例 {code}：{reason}。"),
    "consent_withdrawn_physician": ("立即停止预处理｜{code}", "病例 {code}：{reason}。"),
    "consent_withdrawn_collection": ("释放采集资源｜{code}", "病例 {code}：{reason}。"),
    "consent_withdrawn_carrier": ("取消取件冷链｜{code}", "病例 {code}：{reason}。"),
    "slot_proposed_coord": ("采集窗口待确认｜{code}", "拟议窗口 {window}：{reason}。"),
    "slot_proposed_collection": ("采集窗口待院方确认｜{code}", "拟议窗口 {window}：{reason}。"),
    "slot_proposed_physician": ("采集窗口变动知会｜{code}", "拟议窗口 {window}：{reason}。"),
    "slot_confirmed_donor_affairs": ("采集窗口已确认｜{code}", "确认窗口 {window}：{reason}。"),
    "slot_confirmed_collection": ("锁定采集排班｜{code}", "确认窗口 {window}：{reason}。"),
    "slot_confirmed_carrier": ("排定取件冷链｜{code}", "确认窗口 {window}：{reason}。"),
    "slot_confirmed_receiving": ("预留受者接收窗口｜{code}", "确认窗口 {window}：{reason}。"),
    "slot_confirmed_physician": ("锁定预处理与回输排班｜{code}", "确认窗口 {window}：{reason}。"),
    "slot_confirmed_coord": ("采集窗口落定｜{code}", "确认窗口 {window}：{reason}。"),
    "reschedule_coord": ("采集改期｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "reschedule_collection": ("采集改期，请重核排班｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "reschedule_carrier": ("取件冷链改期｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "reschedule_physician": ("采集改期（预警）｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "reschedule_receiving": ("受者接收窗口调整｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "reschedule_donor_affairs": ("改期需供者重新确认｜{code}", "新窗口 {window}（原因：{reason_text}）。"),
    "slot_cancel_coord": ("采集排期取消｜{code}", "病例 {code}：{reason}。"),
    "slot_cancel_collection": ("取消手术间/床位预留｜{code}", "病例 {code}：{reason}。"),
    "slot_cancel_carrier": ("取消取件冷链计划｜{code}", "病例 {code}：{reason}。"),
    "slot_cancel_physician": ("采集取消（预警）｜{code}", "病例 {code}：{reason}。"),
    "alt_donor_coord": ("替代供者已启用｜{code}", "替代病例 {alt_code}：{reason}。"),
    "alt_donor_donor_affairs": ("联系替代供者｜{code}", "替代病例 {alt_code}：{reason}。"),
    "alt_donor_physician": ("替代供者已启用（预警）｜{code}", "替代病例 {alt_code}：{reason}。"),
    "alt_donor_receiving": ("原接收计划挂起｜{code}", "替代病例 {alt_code}：{reason}。"),
    "collection_started_local": ("采集已开始｜{code}", "病例 {code}：{reason}。"),
    "collection_started_coord": ("采集中｜{code}", "病例 {code}：{reason}。"),
    "collection_completed_coord": ("采集完成｜{code}", "病例 {code}：{reason}。"),
    "collection_completed_carrier": ("产品即将交接取件｜{code}", "病例 {code}：{reason}。"),
    "collection_completed_receiving": ("准备接收入库｜{code}", "病例 {code}：{reason}。"),
    "handover_pickup": ("已取件启运｜{code}", "产品 {product_code}：{reason}。"),
    "handover_in_transit": ("产品已启运｜{code}", "产品 {product_code}：{reason}。"),
    "handover_delivery": ("产品送达，请核封入库｜{code}", "产品 {product_code}：{reason}。"),
    "handover_delivered_physician": ("产品已到院｜{code}", "产品 {product_code}：{reason}。"),
    "handover_coord": ("交接完成｜{code}", "产品 {product_code}：{reason}。"),
    "temp_alert_carrier": ("温控超限（紧急处置）｜{code}", "产品 {product_code} 实测 {temp}：{reason}。"),
    "temp_alert_physician": ("温控超限（待医学决策）｜{code}", "产品 {product_code} 实测 {temp}：{reason}。"),
    "temp_alert_receiving": ("到达后暂停常规入库｜{code}", "产品 {product_code}：{reason}。"),
    "temp_alert_coord": ("温控异常（督办闭环）｜{code}", "产品 {product_code} 实测 {temp}：{reason}。"),
    "temp_resolved_coord": ("温控异常已闭环｜{code}", "产品 {product_code} 处置={disp}：{reason}。"),
    "temp_resolved_carrier": ("按处置结论执行｜{code}", "产品 {product_code} 处置={disp}：{reason}。"),
    "temp_resolved_receiving_continue": ("恢复接收入库准备｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_physician_continue": ("继续运输，按计划回输｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_physician_waiver": ("签署特许放行｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_receiving_waiver": ("凭特许放行单接收｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_physician_recollect": ("产品报废，等待重采｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_collection_recollect": ("准备重新采集｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_donor_affairs_recollect": ("重新采集需供者确认｜{code}", "产品 {product_code}：{reason}。"),
    "temp_resolved_physician_unknown": ("温控处置结论待人工跟进｜{code}", "产品 {product_code}：{reason}。"),
    "delivered_physician": ("产品入库，请下达回输医嘱｜{code}", "产品 {product_code}：{reason}。"),
    "delivered_receiving": ("入库登记完成｜{code}", "产品 {product_code}：{reason}。"),
    "delivered_coord": ("产品送达｜{code}", "产品 {product_code}：{reason}。"),
    "infused_coord": ("回输完成，关闭病例｜{code}", "病例 {code}：{reason}。"),
    "infused_donor_affairs": ("捐献流程完成，安排随访｜{code}", "病例 {code}：{reason}。"),
    "case_cancel_coord": ("病例取消｜{code}", "病例 {code}：{reason}。"),
    "case_cancel_collection": ("病例取消，释放资源｜{code}", "病例 {code}：{reason}。"),
    "case_cancel_carrier": ("病例取消，终止运输｜{code}", "病例 {code}：{reason}。"),
    "case_cancel_physician": ("病例取消（预警）｜{code}", "病例 {code}：{reason}。"),
}

DEFAULT_TEMPLATE = ("协同通知｜{code}", "{reason}。")


def _choose_channel(person) -> str:
    for ch in person.channels:
        if ch.kind in ("sms", "email"):
            return ch.kind
    return "sms"


def _render(template, context):
    subject_t, body_t = TEMPLATES.get(template, DEFAULT_TEMPLATE)
    ctx = {"code": "", "reason": "", "window": "", "product_code": "",
           "temp": "", "version": "", "disp": "", "alt_code": "",
           "reason_text": context.get("reason", ""), **context}

    def fill(text):
        out = text
        for k, v in ctx.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    return fill(subject_t), fill(body_t)


class Notifier:
    def __init__(self, store, directory, clock, audit):
        self.store = store
        self.directory = directory
        self.clock = clock
        self.audit = audit

    def emit(self, *, case, event, audience: Audience, context: dict | None = None):
        """由一次已应用事件生成其全部通知（含被抑制者留痕）。"""
        from .models import Notification
        context = context or {}
        made = []
        for t in audience.targets:
            person = self.directory.people[t.person_id]
            subject, body = _render(t.template, {**context, "reason": t.reason})
            n = Notification(
                id=self.store.next_id("ntf"),
                case_id=case.id,
                template=t.template,
                level=t.level,
                audience_role=t.role,
                audience_org_id=t.org_id,
                recipient_person_id=person.id,
                subject=subject,
                body=body,
                context={"reason": t.reason, **context},
                created_utc=self.clock.now(),
                created_by_event_id=event.id,
            )
            self.store.notifications[n.id] = n
            self._send_attempt(n, person, event)
            made.append(n)
        for s in audience.suppressed:
            subject, body = _render("suppressed", {**context, "reason": s.reason})
            n = Notification(
                id=self.store.next_id("ntf"),
                case_id=case.id,
                template="suppressed",
                level=S.N_INFO,
                audience_role=s.role,
                audience_org_id=s.org_id,
                recipient_person_id=s.person_id,
                subject=f"（未打扰）{case.code}",
                body=s.reason,
                context={"reason": s.reason, **context},
                created_utc=self.clock.now(),
                created_by_event_id=event.id,
                suppressed=True,
                suppress_reason=s.reason,
            )
            self.store.notifications[n.id] = n
            made.append(n)
        return made

    def _send_attempt(self, notification, person, event):
        channel = _choose_channel(person)
        attempt = {
            "attempt": len(notification.sends) + 1,
            "at_utc": iso(self.clock.now()),
            "channel": channel,
            "address_kind": channel,
            "status": "sent",
            "event_id": event.id if event else None,
        }
        notification.sends.append(attempt)
        self.store.log_dispatch({
            "notification_id": notification.id,
            "case_id": notification.case_id,
            "recipient_person_id": person.id,
            "role": notification.audience_role,
            "channel": channel,
            "level": notification.level,
            "at_utc": attempt["at_utc"],
            "kind": "send",
        })

    def resend(self, notification_id: str):
        """允许重发：追加一次发送尝试，不改变既有记录。"""
        from .errors import NotFound, RuleViolation
        n = self.store.notifications.get(notification_id)
        if not n:
            raise NotFound(f"通知不存在: {notification_id}")
        if n.suppressed:
            raise RuleViolation("被抑制的通知不发送；如需触达请人工新建通知")
        person = self.directory.people[n.recipient_person_id]
        self._send_attempt(n, person, None)
        self.audit.append(
            ts=self.clock.now(), actor_id="system", action="notification.resend",
            case_id=n.case_id, ref_type="notification", ref_id=n.id,
            payload={"attempt": len(n.sends)},
        )
        return n

    def record_receipt(self, *, notification, receipt_event, channel):
        """外部送达回执翻转送达状态；重复回执不产生新效果。"""
        if notification.delivered_at_utc is None:
            notification.delivered_at_utc = self.clock.now()
            notification.delivery_receipt_event = receipt_event.id
        notification.sends.append({
            "attempt": len(notification.sends) + 1,
            "at_utc": iso(self.clock.now()),
            "channel": channel,
            "status": "delivered",
            "event_id": receipt_event.id,
        })
        self.store.log_dispatch({
            "notification_id": notification.id,
            "case_id": notification.case_id,
            "recipient_person_id": notification.recipient_person_id,
            "role": notification.audience_role,
            "channel": channel,
            "at_utc": iso(self.clock.now()),
            "kind": "receipt",
        })
