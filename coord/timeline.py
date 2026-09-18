"""监管可复核的完整时间线。

面对任一采集物，生成从配型到回输的节点序列，每个节点明确：
- 绝对时刻（UTC）与各相关机构所在地时间解释；
- 采用的同意版本（采集发生时固定，之后撤回也不改写历史）；
- 交接人与交接结论（温度、封条）；
- 温控异常的处置结果（继续/特许放行/报废重采）、决定人与放行单；
- 对应审计哈希链条目（prev_hash/hash）与整链校验结论。

监管角色调阅本身也写入审计（timeline.viewed）。
"""

from . import states as S
from .clock import local_view
from .errors import NotFound, PermissionError
from .projection import can_access_case, rules_for


def _org_name(directory, org_id):
    org = directory.orgs.get(org_id) if org_id else None
    return {"org_id": org_id, "name": org.name if org else None}


def _local_times(directory, dt, org_ids):
    out = {}
    for oid in sorted({o for o in org_ids if o}):
        org = directory.orgs.get(oid)
        if org:
            out[oid] = {"name": org.name, **local_view(dt, org.tz)}
    return out


def _person(directory, person_id):
    if not person_id:
        return None
    p = directory.people.get(person_id)
    if not p:
        return {"person_id": person_id, "name": None, "title": None,
                "org": _org_name(directory, None)}
    return {"person_id": p.id, "name": p.name, "title": p.title,
            "org": _org_name(directory, p.org_id)}


def _consent_snapshot(case, version):
    cv = case.consent(version) if version is not None else None
    if not cv:
        return None
    d = cv.to_dict()
    d["status_at_review"] = (
        "withdrawn" if any(
            w.kind == S.CONSENT_WITHDRAWAL and w.supersedes_version == version
            for w in case.consent_versions
        ) else "effective"
    )
    return d


def _node(audit_entry, directory, case):
    org_ids = [case.donor_org_id, case.collection_org_id,
               case.carrier_org_id, case.transplant_org_id]
    node = {
        "seq": audit_entry.seq,
        "ts_utc": audit_entry.to_dict()["ts_utc"],
        "action": audit_entry.action,
        "actor": _person(directory, audit_entry.actor_id),
        "event_id": audit_entry.event_id,
        "note": audit_entry.note,
        "local_times": _local_times(directory, audit_entry.ts, org_ids),
        "payload": audit_entry.payload,
        "hash_chain": {"prev_hash": audit_entry.prev_hash, "hash": audit_entry.hash},
    }
    return node


def build_product_timeline(*, store, directory, audit, clock, product_code, viewer):
    product = next((p for p in store.products.values() if p.code == product_code), None)
    if not product:
        raise NotFound(f"采集物不存在: {product_code}")
    case = store.require_case(product.case_id)
    if not can_access_case(case, viewer):
        raise PermissionError("无权查看该采集物时间线")

    relevant_orgs = [case.donor_org_id, case.collection_org_id,
                     case.carrier_org_id, case.transplant_org_id]

    # 病例级与产品级审计条目共同构成时间线（病例事件解释了产品为何存在/终止）
    entries = [e for e in audit.entries if e.case_id == case.id]
    nodes = [_node(e, directory, case) for e in entries]

    # 交接链：明确每一棒的交出人、接收人、温度与封条
    handovers = []
    for h in product.handovers:
        hd = h.to_dict()
        hd["from_person"] = _person(directory, h.from_person_id)
        hd["to_person"] = _person(directory, h.to_person_id)
        hd["from_org"] = _org_name(directory, h.from_org_id)
        hd["to_org"] = _org_name(directory, h.to_org_id)
        hd["local_times"] = _local_times(directory, h.at_utc, relevant_orgs)
        handovers.append(hd)

    # 异常处置闭环：检测 → 处置结论/决定人/放行单
    excursions = []
    for ex in product.excursions:
        ed = ex.to_dict()
        ed["reporter"] = _person(directory, ex.reported_by)
        ed["decider"] = _person(directory, ex.decision_by)
        ed["detected_local"] = _local_times(directory, ex.detected_utc, relevant_orgs)
        ed["outcome"] = {
            S.TEMP_OPEN: "待评估，运输/接收已挂起",
            S.TEMP_RESOLVED: {
                S.DISP_CONTINUE: "评估合格，继续运输并正常接收",
                S.DISP_RELEASE_WAIVER: "特许放行：凭医学放行单接收回输",
                S.DISP_RECOLLECT: "判定报废，进入重采/替代供者流程",
            }.get(ex.disposition, ex.disposition),
        }.get(ex.status, ex.status)
        excursions.append(ed)

    # 采集当时采用的同意（历史固定），以及当前是否已被撤回
    adopted_consent = _consent_snapshot(case, product.consent_version)

    # 时间窗解释
    slot = case.active_slot()
    slots = []
    for s in case.slots:
        sd = s.to_dict()
        sd["start_local"] = local_view(s.planned_start_utc, s.tz)
        sd["end_local"] = local_view(s.planned_end_utc, s.tz)
        slots.append(sd)

    verification = audit.verify()
    timeline = {
        "kind": "product_timeline",
        "product": {
            "code": product.code,
            "status": product.status,
            "donor_case_ref": product.donor_case_ref,
            "case_code": case.code,
            "temp_range": list(product.temp_range),
            "collected_utc": product.to_dict()["collected_utc"],
            "delivered_utc": product.to_dict()["delivered_utc"],
            "infused_utc": product.to_dict()["infused_utc"],
            "collection_org": _org_name(directory, product.collection_org_id),
            "current_holder": {
                "org": _org_name(directory, product.current_holder_org_id),
                "person": _person(directory, product.current_holder_person_id),
            },
        },
        "case_phase": case.phase,
        "adopted_consent_version": product.consent_version,
        "adopted_consent": adopted_consent,
        "consent_note": (
            "采集物在采集时绑定同意 v{}；即使其后供者追加撤回版本，"
            "历史节点采用的同意版本仍如实保留。".format(product.consent_version)
            if product.consent_version else "本采集物未绑定同意版本（异常）"
        ),
        "slots": slots,
        "handovers": handovers,
        "excursions": excursions,
        "nodes": nodes,
        "integrity": {
            "algorithm": "sha256_chain",
            **verification,
            "timeline_entries": len(entries),
        },
        "review_window": {
            "note": "所有 ts_utc 为同一绝对时刻；local_times 给出各中心所在地时间解释",
        },
    }

    # 调阅留痕（校验快照在追加前取得）
    audit.append(
        ts=clock.now(), actor_id=viewer.id, action="timeline.viewed",
        case_id=case.id, product_id=product.id,
        ref_type="product", ref_id=product.id,
        payload={"product_code": product.code, "viewer_roles": list(viewer.roles),
                 "integrity_head_at_view": verification.get("head")},
    )
    return timeline


def build_case_timeline(*, store, directory, audit, case_id, viewer, clock):
    case = store.require_case(case_id)
    if not can_access_case(case, viewer):
        raise PermissionError("无权查看该病例时间线")
    entries = [e for e in audit.entries if e.case_id == case.id]
    verification = audit.verify()
    timeline = {
        "kind": "case_timeline",
        "case_code": case.code,
        "phase": case.phase,
        "donor_ref": f"DONOR/{case.code}" if not rules_for(viewer)["donor_pii"] else case.donor_person_id,
        "organizations": {
            "donor_registry": _org_name(directory, case.donor_org_id),
            "collection_hospital": _org_name(directory, case.collection_org_id),
            "carrier": _org_name(directory, case.carrier_org_id),
            "transplant_hospital": _org_name(directory, case.transplant_org_id),
        },
        "consent_versions": [c.to_dict() for c in case.consent_versions],
        "effective_consent_version": case.effective_consent_version,
        "products": [p.code for pid in case.product_ids
                     for p in [store.products.get(pid)] if p],
        "nodes": [_node(e, directory, case) for e in entries],
        "integrity": {"algorithm": "sha256_chain", **verification},
    }
    audit.append(
        ts=clock.now(), actor_id=viewer.id, action="timeline.viewed",
        case_id=case.id, ref_type="case", ref_id=case.id,
        payload={"viewer_roles": list(viewer.roles),
                 "integrity_head_at_view": verification.get("head")},
    )
    return timeline
