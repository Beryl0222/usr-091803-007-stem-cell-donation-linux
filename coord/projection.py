"""按职责最小化展示（字段级 RBAC 脱敏）。

双盲红线：供者身份对受者医院不可见；受者侧不存储患者姓名等标识，
供者侧只见病例代号。工作人员姓名（交接链上的院内人员）不属于供/患 PII，
在交接记录中保留，以便责任追溯。
"""

from . import states as S
from .clock import local_view
from .identity import (
    CARRIER_HANDLER, COLLECTION_STAFF, COORDINATOR, DONOR_AFFAIRS,
    RECEIVING_STAFF, REGULATOR, TRANSPLANT_PHYSICIAN,
)

# 各角色可见性
# donor_pii   : 供者姓名/人员标识
# hla         : HLA 配型摘要
# consent_doc : 同意书编号、签署时间等原件定位信息
# all_notes   : 病例备注全文
# all_notifications: 非发给本人的通知
_ROLE_RULES = {
    REGULATOR: dict(donor_pii=True, hla=True, consent_doc=True,
                      all_notes=True, all_notifications=True, any_case=True),
    COORDINATOR: dict(donor_pii=True, hla=True, consent_doc=True,
                        all_notes=True, all_notifications=True),
    DONOR_AFFAIRS: dict(donor_pii=True, hla=False, consent_doc=True,
                          all_notes=False, all_notifications=False),
    COLLECTION_STAFF: dict(donor_pii=False, hla=False, consent_doc=False,
                             all_notes=False, all_notifications=False),
    CARRIER_HANDLER: dict(donor_pii=False, hla=False, consent_doc=False,
                            all_notes=False, all_notifications=False),
    RECEIVING_STAFF: dict(donor_pii=False, hla=False, consent_doc=False,
                            all_notes=False, all_notifications=False),
    TRANSPLANT_PHYSICIAN: dict(donor_pii=False, hla=True, consent_doc=False,
                                 all_notes=True, all_notifications=False),
}


def rules_for(viewer) -> dict:
    for role in viewer.roles:
        if role in _ROLE_RULES:
            return _ROLE_RULES[role]
    return dict(donor_pii=False, hla=False, consent_doc=False,
                all_notes=False, all_notifications=False, any_case=False)


def case_org_scopes(viewer):
    """该人员可访问的机构维度集合。"""
    rules = rules_for(viewer)
    if rules.get("any_case"):
        return None  # 不受限
    scopes = set()
    if viewer.has(REGULATOR):
        return None
    if viewer.has(COORDINATOR) or viewer.has(DONOR_AFFAIRS):
        scopes.add(("donor_org", viewer.org_id))
    if viewer.has(COLLECTION_STAFF):
        scopes.add(("collection_org", viewer.org_id))
    if viewer.has(CARRIER_HANDLER):
        scopes.add(("carrier_org", viewer.org_id))
    if viewer.has(RECEIVING_STAFF) or viewer.has(TRANSPLANT_PHYSICIAN):
        scopes.add(("transplant_org", viewer.org_id))
    return scopes


def can_access_case(case, viewer) -> bool:
    scopes = case_org_scopes(viewer)
    if scopes is None:
        return True
    for kind, org_id in scopes:
        if kind == "donor_org" and case.donor_org_id == org_id:
            return True
        if kind == "collection_org" and case.collection_org_id == org_id:
            return True
        if kind == "carrier_org" and case.carrier_org_id == org_id:
            return True
        if kind == "transplant_org" and case.transplant_org_id == org_id:
            return True
    return False


def _masked_donor(case, rules, directory):
    if rules["donor_pii"]:
        person = directory.people.get(case.donor_person_id)
        return {"person_id": case.donor_person_id,
                "name": person.name if person else None,
                "masked": False}
    return {"person_id": None, "name": None, "masked": True,
            "pseudonym": f"DONOR/{case.code}"}


def _project_consent(cv, rules):
    base = cv.to_dict()
    if not rules["consent_doc"]:
        base["document_ref"] = None
        base["note"] = None if not rules["all_notes"] else base.get("note")
        base["recorded_by"] = None
        base["donor_person_id"] = None
    return base


def _project_slot(slot, directory):
    tz = slot.tz
    out = slot.to_dict()
    out["planned_start_local"] = local_view(slot.planned_start_utc, tz)
    out["planned_end_local"] = local_view(slot.planned_end_utc, tz)
    return out


def project_case(case, viewer, directory, store, *, include_products=True):
    rules = rules_for(viewer)
    search = store.searches.get(case.search_id)
    data = {
        "id": case.id,
        "code": case.code,
        "phase": case.phase,
        "donor": _masked_donor(case, rules, directory),
        "organizations": {
            "donor_registry": case.donor_org_id,
            "collection_hospital": case.collection_org_id,
            "carrier": case.carrier_org_id,
            "transplant_hospital": case.transplant_org_id,
        },
        "screening": {
            "pass": case.screening_pass,
            "summary": case.screening_summary if rules["hla"] or rules["all_notes"] else None,
        },
        "effective_consent_version": case.effective_consent_version,
        "consent_versions": [_project_consent(c, rules) for c in case.consent_versions],
        "slots": [_project_slot(s, directory) for s in case.slots],
        "active_slot_id": case.active_slot_id,
        "product_codes": [],
        "replaced_by_case_id": case.replaced_by_case_id,
        "superseded": case.superseded,
        "cancel_reason": case.cancel_reason if rules["all_notes"] or case.cancel_reason else None,
        "created_utc": case.to_dict()["created_utc"],
        "updated_utc": case.to_dict()["updated_utc"],
    }
    if rules["hla"] and search:
        data["hla_summary"] = search.hla_summary
        data["urgency"] = search.urgency
    if include_products:
        data["product_codes"] = [
            store.products[pid].code for pid in case.product_ids if pid in store.products
        ]
    return data


def project_product(product, viewer, directory):
    rules = rules_for(viewer)
    data = product.to_dict()
    # 采集物视图默认不含供者标识，只保留匿名引用
    data["donor_case_ref"] = product.donor_case_ref
    # 承运方只需交接与温控，不看病例归属与回输医生等受者侧安排
    if viewer.has(CARRIER_HANDLER):
        data["case_id"] = None
        data["infusing_physician_id"] = None
    handovers = []
    for h in product.handovers:
        hd = h.to_dict()
        fp = directory.people.get(h.from_person_id)
        tp = directory.people.get(h.to_person_id)
        # 交接双方是工作人员，姓名保留以落实责任
        hd["from_person_name"] = fp.name if fp else None
        hd["to_person_name"] = tp.name if tp else None
        handovers.append(hd)
    data["handovers"] = handovers
    return data


def visible_notifications(case_id, viewer, store):
    rules = rules_for(viewer)
    out = []
    for n in store.notifications.values():
        if n.case_id != case_id:
            continue
        if rules["all_notifications"] or n.recipient_person_id == viewer.id:
            out.append(n)
    return sorted(out, key=lambda x: x.created_utc)
