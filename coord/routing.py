"""精准受众路由。

每次事件只生成“确有动作要做”的通知；对“看似相关、此刻却无需其处理”的一方，
生成 suppressed 留痕并说明原因（例如尚未签约的承运方、已完成交接的采集医院），
可审计但不打扰。
"""

from dataclasses import dataclass, field

from . import states as S
from .identity import (
    CARRIER_HANDLER, COLLECTION_STAFF, COORDINATOR, DONOR_AFFAIRS,
    RECEIVING_STAFF, TRANSPLANT_PHYSICIAN,
)


@dataclass
class Target:
    person_id: str
    role: str
    org_id: str | None
    level: str           # action / alert / info
    template: str
    reason: str          # 为什么需要此人处理（审计可见）


@dataclass
class Suppressed:
    person_id: str
    role: str
    org_id: str | None
    reason: str


@dataclass
class Audience:
    targets: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)

    def add(self, directory, org_id, role, level, template, reason, person_id=None):
        if person_id is None:
            people = directory.staff_of(org_id, role) if org_id else \
                [p for p in directory.people.values() if p.has(role)]
            if not people:
                return
            person_id = people[0].id
            org_id = people[0].org_id
        self.targets.append(Target(person_id, role, org_id, level, template, reason))

    def suppress(self, directory, org_id, role, reason):
        people = directory.staff_of(org_id, role) if org_id else \
            [p for p in directory.people.values() if p.has(role)]
        for p in people:
            self.suppressed.append(Suppressed(p.id, role, p.org_id, reason))


def reached(case, phase, store=None):
    """病例是否曾到达某阶段（以里程碑/已存在产物推断，撤回后仍可判定卷入程度）。"""
    if case.ever_reached(phase):
        return True
    if phase == S.COLLECTED:
        return bool(case.product_ids)
    if phase == S.IN_TRANSIT and store is not None:
        return any(
            any(h.to_org_id == case.carrier_org_id
                for h in store.products[pid].handovers)
            for pid in case.product_ids if pid in store.products
        )
    return False


def _coordinators(aud, directory, case, level, template, reason):
    people = [p for p in directory.people.values()
              if p.has(COORDINATOR) and p.org_id == case.donor_org_id]
    for p in people:
        aud.targets.append(Target(p.id, COORDINATOR, p.org_id, level, template, reason))


def resolve(event_type, payload, case, product, directory) -> Audience:
    aud = Audience()
    P = payload

    if event_type == S.EV_MATCH_SUCCESS:
        _coordinators(aud, directory, case, S.N_ACTION, "match_success_coord",
                      "建案并启动供者联系与高分辨安排")
        aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                "match_success_donor_affairs", "联系供者确认捐献意愿并安排高分辨采样")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_INFO,
                "match_success_physician", "获知配型成功，等待后续时间线")
        aud.suppress(directory, None, CARRIER_HANDLER, "尚未排期，承运方未卷入")

    elif event_type == S.EV_SCREENING_RESULT:
        _coordinators(aud, directory, case, S.N_ACTION, "screening_coord",
                      "根据筛查结果决定是否推进或启动备选")
        if P.get("pass"):
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_INFO,
                    "screening_pass_physician", "筛查通过，可进入同意与排期准备")
        else:
            aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                    "screening_fail_donor_affairs", "向供者说明筛查结论并跟进后续")
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                    "screening_fail_physician", "供者筛查未过，需准备替代供者方案")

    elif event_type == S.EV_CONSENT_GRANTED:
        _coordinators(aud, directory, case, S.N_ACTION, "consent_granted_coord",
                      "同意齐备，启动采集排期")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_INFO,
                "consent_granted_physician", "供者同意已确认，可提交期望采集窗口")

    elif event_type == S.EV_CONSENT_WITHDRAWN:
        # 关键场景：撤回只惊动“此刻确实要停下手里动作”的人
        _coordinators(aud, directory, case, S.N_ALERT, "consent_withdrawn_coord",
                      "供者撤回同意，立即冻结流程并启动替代供者")
        aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                "consent_withdrawn_donor_affairs", "接收并确认供者撤回，完成供者侧沟通")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "consent_withdrawn_physician", "立即停止预处理方案，等待替代供者时间线")
        if reached(case, S.SCHEDULED) and case.collection_org_id:
            aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                    "consent_withdrawn_collection", "释放已预留的采集手术间与床位")
        else:
            aud.suppress(directory, case.collection_org_id, COLLECTION_STAFF,
                         "尚未确认采集排期，采集医院无动作可撤销")
        if case.carrier_org_id and reached(case, S.SCHEDULED):
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "consent_withdrawn_carrier", "取消已排定的取件冷链计划")
        else:
            aud.suppress(directory, case.carrier_org_id, CARRIER_HANDLER,
                         "承运方尚未承接本病例取件任务，无需通知")

    elif event_type == S.EV_SLOT_PROPOSED:
        _coordinators(aud, directory, case, S.N_ACTION, "slot_proposed_coord",
                      "牵头四方确认时间窗口")
        aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                "slot_proposed_collection", "核对手术间与床位可否承接该窗口")
        if P.get("reason"):
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_INFO,
                    "slot_proposed_physician", f"窗口变动（{P['reason']}），预评估预处理节奏")

    elif event_type == S.EV_SLOT_CONFIRMED:
        aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                "slot_confirmed_donor_affairs", "通知并确认供者按窗口到院")
        aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                "slot_confirmed_collection", "锁定手术间、床位与采集耗材")
        if case.carrier_org_id:
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "slot_confirmed_carrier", "排定取件时刻、线路与温控箱")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                "slot_confirmed_receiving", "预留受者床位与接收检验窗口")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ACTION,
                "slot_confirmed_physician", "按窗口锁定预处理与回输排班")
        _coordinators(aud, directory, case, S.N_INFO, "slot_confirmed_coord", "窗口落定，监督执行")

    elif event_type == S.EV_SLOT_RESCHEDULE:
        # 改期：惊动需要为新窗口重做安排的人；发起方已知情，只做确认性动作
        reason = P.get("reason", "改期")
        requested_by = P.get("requested_by", "")
        _coordinators(aud, directory, case, S.N_ACTION, "reschedule_coord",
                      f"重建四方时间确认：{reason}")
        aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                "reschedule_collection", f"改期（{reason}），重新核对手术间与床位")
        if case.carrier_org_id:
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "reschedule_carrier", f"采集窗口改期（{reason}），重排取件冷链")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "reschedule_physician", f"采集改期（{reason}），评估并调整预处理/床位")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                "reschedule_receiving", "受者床位与接收窗口需随新时间调整")
        if requested_by != "donor":
            aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                    "reschedule_donor_affairs", "非供者原因改期，需与供者重新确认到院时间")
        else:
            aud.suppress(directory, case.donor_org_id, DONOR_AFFAIRS,
                         "改期由供者发起，供者侧已知情，待新窗口确认时再通知")

    elif event_type == S.EV_SLOT_CANCEL:
        _coordinators(aud, directory, case, S.N_ACTION, "slot_cancel_coord",
                      "释放已排期资源并评估后续")
        aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                "slot_cancel_collection", "取消手术间与床位预留")
        if case.carrier_org_id:
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "slot_cancel_carrier", "取消取件冷链计划")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "slot_cancel_physician", "采集取消，暂停预处理并等待新安排")

    elif event_type == S.EV_ALT_DONOR:
        _coordinators(aud, directory, case, S.N_ACTION, "alt_donor_coord",
                      "衔接原受者与替代供者病例的时间线")
        aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                "alt_donor_donor_affairs", "联系替代供者启动高分辨、体检与同意流程")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "alt_donor_physician", "替代供者已启用，按新时间线重订预处理计划")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_INFO,
                "alt_donor_receiving", "原床位/接收计划挂起，等待新窗口")
        aud.suppress(directory, case.carrier_org_id, CARRIER_HANDLER,
                     "替代供者尚无排期，承运方在新窗口确认后再承接")

    elif event_type == S.EV_COLLECTION_STARTED:
        aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_INFO,
                "collection_started_local", "采集已开始，院内状态同步")
        _coordinators(aud, directory, case, S.N_INFO, "collection_started_coord", "采集中，监控进度")

    elif event_type == S.EV_COLLECTION_COMPLETED:
        _coordinators(aud, directory, case, S.N_ACTION, "collection_completed_coord",
                      "督导入库交接与冷链启动")
        if case.carrier_org_id:
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "collection_completed_carrier", "产品即将入库交接，按计划到场取件")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                "collection_completed_receiving", "产品已采集，准备接收入库与检验")

    elif event_type == S.EV_HANDOVER:
        # 交接由双方当场确认；只通知链条上“下一个需要动的人”
        to_org = P.get("to_org_id")
        if to_org == case.carrier_org_id:
            aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                    "handover_pickup", "已取件，启运并持续温控监控")
            aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_INFO,
                    "handover_in_transit", "产品已启运，关注预计送达时刻")
        elif to_org == case.transplant_org_id:
            aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                    "handover_delivery", "产品送达，立即核封、测温、入库")
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ACTION,
                    "handover_delivered_physician", "产品已到院，确认回输安排")
        _coordinators(aud, directory, case, S.N_INFO, "handover_coord", "交接完成，链路留痕")

    elif event_type == S.EV_TEMP_ALERT:
        # 异常当下：运输处置者 + 放行/拒收决策者；采集医院此刻无动作
        aud.add(directory, product.current_holder_org_id or case.carrier_org_id,
                CARRIER_HANDLER, S.N_ALERT,
                "temp_alert_carrier", "途中温控超限：立即检查包装、冷媒与线路并采取处置")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "temp_alert_physician", "温控超限，需评估产品可用性并准备放行/拒收决策")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                "temp_alert_receiving", "产品到达后暂停常规入库，等待异常评估结论")
        _coordinators(aud, directory, case, S.N_ALERT, "temp_alert_coord",
                      "督办温控异常评估与处置闭环")
        aud.suppress(directory, case.collection_org_id, COLLECTION_STAFF,
                     "产品已离院，异常处置不涉及采集医院；若判定重采将另行通知")

    elif event_type == S.EV_TEMP_RESOLVED:
        disp = P.get("disposition")
        _coordinators(aud, directory, case, S.N_ACTION, "temp_resolved_coord",
                      f"温控异常闭环：{disp}，督办执行与归档")
        aud.add(directory, product.current_holder_org_id or case.carrier_org_id,
                CARRIER_HANDLER, S.N_ACTION,
                "temp_resolved_carrier", "按评估结论继续运输/退回/等待指令")
        if disp == S.DISP_CONTINUE:
            aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                    "temp_resolved_receiving_continue", "评估通过继续运输，恢复接收入库准备")
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_INFO,
                    "temp_resolved_physician_continue", "评估结论：继续运输，按原计划回输")
        elif disp == S.DISP_RELEASE_WAIVER:
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ACTION,
                    "temp_resolved_physician_waiver", "签署特许放行并记录医学理由后回输")
            aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_ACTION,
                    "temp_resolved_receiving_waiver", "凭特许放行单接收，随批归档豁免记录")
        elif disp == S.DISP_RECOLLECT:
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                    "temp_resolved_physician_recollect", "产品报废：停止回输，等待重采/替代时间线")
            aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                    "temp_resolved_collection_recollect", "需重新采集：等待协调员重排窗口与耗材")
            aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                    "temp_resolved_donor_affairs_recollect", "需重新采集，重新与供者确认时间与意愿")
        else:
            aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                    "temp_resolved_physician_unknown", f"未知处置结论 {disp}，需人工跟进")

    elif event_type == S.EV_DELIVERY:
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ACTION,
                "delivered_physician", "产品入库，下达回输前检验与回输医嘱")
        aud.add(directory, case.transplant_org_id, RECEIVING_STAFF, S.N_INFO,
                "delivered_receiving", "完成入库登记")
        _coordinators(aud, directory, case, S.N_INFO, "delivered_coord", "产品送达，等待回输")

    elif event_type == S.EV_INFUSION:
        _coordinators(aud, directory, case, S.N_ACTION, "infused_coord",
                      "回输完成，关闭病例并启动随访")
        aud.add(directory, case.donor_org_id, DONOR_AFFAIRS, S.N_ACTION,
                "infused_donor_affairs", "向供者反馈捐献流程完成并安排随访")
        aud.suppress(directory, case.carrier_org_id, CARRIER_HANDLER,
                     "承运责任已终结，回输环节无需承运方处理")
        aud.suppress(directory, case.collection_org_id, COLLECTION_STAFF,
                     "采集医院任务已终结，回输环节无需其处理")

    elif event_type == S.EV_CASE_CANCEL:
        # 只通知当前仍实际卷入的一方
        _coordinators(aud, directory, case, S.N_ACTION, "case_cancel_coord", "执行病例取消与资源释放")
        if reached(case, S.SCHEDULED):
            aud.add(directory, case.collection_org_id, COLLECTION_STAFF, S.N_ACTION,
                    "case_cancel_collection", "病例取消，释放手术间/床位")
            if case.carrier_org_id:
                aud.add(directory, case.carrier_org_id, CARRIER_HANDLER, S.N_ACTION,
                        "case_cancel_carrier", "病例取消，终止运输任务")
        aud.add(directory, case.transplant_org_id, TRANSPLANT_PHYSICIAN, S.N_ALERT,
                "case_cancel_physician", "病例取消，停止受者侧一切预处理安排")

    return aud
