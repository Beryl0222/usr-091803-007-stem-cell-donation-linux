"""病例阶段、事件类型与允许的状态迁移。"""

# ---- 病例阶段 ----
MATCHED = "matched"                # 非血缘配型成功，待启动确认
SCREENING = "screening"            # 高分辨分型 / 体检
CONSENTED = "consented"            # 有效同意齐备
SCHEDULED = "scheduled"            # 采集窗口已确认
COLLECTED = "collected"            # 采集完成，采集物入库
IN_TRANSIT = "in_transit"          # 冷链运输中
DELIVERED = "delivered"            # 送达受者医院
INFUSED = "infused"                # 已回输
CLOSED = "closed"                  # 正常关闭

# ---- 终止/异常阶段（保留完整记录，不可删除）----
DONOR_WITHDRAWN = "donor_withdrawn"      # 供者撤回，等待替代供者
CANCELLED = "cancelled"                  # 病例取消
RECOLLECT = "recollect"                  # 采集物报废，需重采/替代

CASE_PHASES = {
    MATCHED, SCREENING, CONSENTED, SCHEDULED, COLLECTED,
    IN_TRANSIT, DELIVERED, INFUSED, CLOSED,
    DONOR_WITHDRAWN, CANCELLED, RECOLLECT,
}

# ---- 外部事件类型（自然键去重的对象）----
EV_MATCH_SUCCESS = "match.success"                  # 配型成功通知
EV_SCREENING_RESULT = "screening.result"            # 筛查/体检结果
EV_CONSENT_GRANTED = "consent.granted"              # 同意签署
EV_CONSENT_WITHDRAWN = "consent.withdrawn"          # 同意撤回（新版本）
EV_SLOT_PROPOSED = "slot.proposed"
EV_SLOT_CONFIRMED = "slot.confirmed"
EV_SLOT_RESCHEDULE = "slot.reschedule_requested"    # 供者/病情/床位导致改期
EV_SLOT_CANCEL = "slot.cancelled"
EV_ALT_DONOR = "match.alternative_activated"        # 替代供者启用
EV_COLLECTION_STARTED = "collection.started"
EV_COLLECTION_COMPLETED = "collection.completed"
EV_HANDOVER = "chain.handover"                      # 任一交接节点确认
EV_TEMP_ALERT = "temp.excursion_reported"           # 途中温控异常
EV_TEMP_RESOLVED = "temp.excursion_resolved"
EV_DELIVERY = "product.delivered"
EV_INFUSION = "infusion.completed"
EV_CASE_CANCEL = "case.cancelled"
EV_DELIVERY_RECEIPT = "notify.delivery_receipt"     # 通知送达回执

# ---- 同意 ----
CONSENT_GRANT = "grant"
CONSENT_WITHDRAWAL = "withdrawal"
# 同意覆盖的事项范围
SCOPE_HR_TYPING = "hr_typing"          # 高分辨确认
SCOPE_MEDICAL_EXAM = "medical_exam"    # 体检
SCOPE_COLLECTION = "collection"        # 采集
SCOPE_FOLLOWUP = "followup"            # 随访

# ---- 时间槽 ----
SLOT_PROPOSED = "proposed"
SLOT_CONFIRMED = "confirmed"
SLOT_CANCELLED = "cancelled"

# ---- 采集物 ----
PROD_COLLECTED = "collected"
PROD_IN_TRANSIT = "in_transit"
PROD_DELIVERED = "delivered"
PROD_INFUSED = "infused"
PROD_REJECTED = "rejected"

# ---- 温控 ----
TEMP_OPEN = "open"
TEMP_ASSESSED = "assessed"
TEMP_RESOLVED = "resolved"
DISP_CONTINUE = "continue"                     # 评估后继续运输
DISP_RELEASE_WAIVER = "release_with_waiver"    # 特许放行（双方医生确认）
DISP_RECOLLECT = "recollect"                   # 报废重采/启用替代

# ---- 通知 ----
N_INFO = "info"
N_ACTION = "action"
N_ALERT = "alert"
