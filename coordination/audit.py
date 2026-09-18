"""监管可复核时间线。

面对任一采集物（按病例/采集物 ID），工作人员可生成一份完整、按时间排序、
可独立验证的时间线，明确：
- 每个关键节点（采集、每次交接、回输）当时采用的同意版本与文书哈希；
- 每次交接的交出人/接收人、容器、温度、凭证号；
- 每次温控异常的读数与最终处置结果、决策人；
- 排期各版本、各方确认与改期原因；
- 取消/替代供者的依据。

时间线末尾附台账哈希链校验结论与去重统计，监管无需信任本系统即可复核。
"""

from datetime import datetime

from .clock import format_local, now_utc
from .events import Ledger
from .models import (
    HandoverKind, ConsentAction, Role,
)
from .projection import replay
from .privacy import (
    project_identity, DONOR_FIELD_VISIBILITY, RECIPIENT_FIELD_VISIBILITY,
    donor_pseudonym, recipient_pseudonym,
)


def _consent_in_force(consents, seq: int):
    """截至某事件序号，最新的同意版本（含撤回版）。"""
    current = None
    for c in consents:
        if c.event_seq <= seq:
            current = c
    return current


def _consent_ref(consents, seq: int) -> dict:
    c = _consent_in_force(consents, seq)
    if not c:
        return {"consent_present": False}
    return {
        "consent_present": True,
        "version": c.version,
        "action": c.action,
        "document_version": c.document_version,
        "document_hash": c.document_hash,
        "signed_local": c.signed_local,
        "signed_tz": c.tz,
        "state": "granted" if c.action == ConsentAction.GRANT.value
                 else "withdrawn",
    }


_NODE_LABELS = {
    "case_opened": "建档（非血缘配型成功）",
    "screening_started": "启动供者筛查",
    "screening_passed": "筛查通过",
    "consent_granted": "签署知情同意",
    "consent_withdrawn": "撤回知情同意（新版本）",
    "collection_scheduled": "安排/调整采集窗口",
    "schedule_confirmed": "参与方确认窗口",
    "collection_completed": "采集完成",
    "handover": "采集物交接",
    "shipment_excursion": "温控异常",
    "excursion_resolved": "温控异常处置",
    "shipment_delivered": "送达受者医院",
    "product_accepted": "受者医院核验签收",
    "product_rejected": "受者医院拒收",
    "infusion_completed": "回输完成",
    "case_cancelled": "病例取消",
    "donor_swapped": "启用替代供者（本病例归档）",
    "case_note": "人工台账备注",
}

# 监管重点核对的关键节点：必须能指出采用的同意版本
_CRITICAL = {
    "collection_completed", "handover", "infusion_completed",
}


def build_timeline(ledger: Ledger, case_id: str, viewer_tz: str = "Asia/Shanghai",
                   viewer_role: Role = Role.REGULATOR) -> dict:
    events = sorted(
        [e for e in ledger.events_for(case_id)
         if not e.event_type.startswith("notification_")],
        key=lambda e: e.seq)
    if not events:
        raise KeyError(f"病例不存在: {case_id}")
    snap = replay(events)

    entries = []
    confirmations = {}
    for e in events:
        p = e.payload
        at_utc = e.timestamp
        entry = {
            "seq": e.seq,
            "node": e.event_type,
            "label": _NODE_LABELS.get(e.event_type, e.event_type),
            "at_utc": at_utc,
            "at_local": format_local(datetime.fromisoformat(at_utc), viewer_tz),
            "viewer_tz": viewer_tz,
            "actor_role": e.actor_role,
            "actor_party": e.actor_party,
            "critical": e.event_type in _CRITICAL,
            "consent_in_force": _consent_ref(snap.consents, e.seq),
            "detail": {},
        }

        if e.event_type == "case_opened":
            entry["detail"] = {
                "product_type": snap.product_type,
                "donor": (project_identity(snap.donor_identity, viewer_role,
                                           DONOR_FIELD_VISIBILITY)
                          or {"pseudonym": donor_pseudonym(case_id)}),
                "recipient": (project_identity(snap.recipient_identity, viewer_role,
                                               RECIPIENT_FIELD_VISIBILITY)
                              or {"pseudonym": recipient_pseudonym(case_id)}),
                "parties": {
                    "donor_center": snap.donor_center_party_id,
                    "courier": snap.courier_party_id,
                    "recipient_hospital": snap.recipient_party_id,
                    "coordinator": snap.coordinator_party_id,
                },
                "timezones": {
                    "donor": snap.donor_tz, "donor_center": snap.donor_center_tz,
                    "courier": snap.courier_tz, "recipient": snap.recipient_tz,
                },
            }

        elif e.event_type in ("consent_granted", "consent_withdrawn"):
            entry["detail"] = {
                "version": p["version"], "action": p["action"],
                "document_version": p["document_version"],
                "document_hash": p["document_hash"],
                "signed_local": p["signed_local"], "tz": p["tz"],
                "witness_party": p.get("witness_party", ""),
                "statement": p.get("statement", ""),
                "reason": p.get("reason", ""),
                "immutable": "已确认同意不得改写；撤回以新版本生效，旧版本原样保留",
            }

        elif e.event_type == "collection_scheduled":
            entry["detail"] = {
                "schedule_version": p["schedule_version"],
                "window": {
                    "start_utc": p["start_utc"], "end_utc": p["end_utc"],
                    "start_local": format_local(
                        datetime.fromisoformat(p["start_utc"]), viewer_tz),
                    "end_local": format_local(
                        datetime.fromisoformat(p["end_utc"]), viewer_tz),
                    "declared_by_center_tz": p.get("declared_tz", ""),
                },
                "reason": p.get("reason", ""),
                "proposed_by": p.get("proposed_by", ""),
            }

        elif e.event_type == "schedule_confirmed":
            confirmations.setdefault(p["party_id"], []).append(e.seq)
            entry["detail"] = {"party_id": p["party_id"],
                               "confirmed_local": p["local"], "tz": p["tz"]}

        elif e.event_type == "collection_completed":
            entry["detail"] = {
                "product_id": p["product_id"], "volume_ml": p.get("volume_ml"),
                "adopted_consent_version": p["effective_consent_version"],
                "adopted_consent_document_hash": p.get("consent_document_hash", ""),
                "all_parties_confirmed": snap.all_confirmed(),
            }

        elif e.event_type == "handover":
            label = ("采集医院 → 冷链承运方"
                     if p["kind"] == HandoverKind.COLLECTION_TO_COURIER.value
                     else "冷链承运方 → 受者医院")
            entry["detail"] = {
                "handover": label,
                "from_party": p["from_party"], "handover_from_person": p["from_person"],
                "to_party": p["to_party"], "handover_to_person": p["to_person"],
                "product_temp_c": p["product_temp_c"],
                "container_id": p["container_id"],
                "evidence_ref": p["evidence_ref"],
                "adopted_consent_version": p["effective_consent_version"],
                "dual_signature": bool(p["from_person"] and p["to_person"]),
            }

        elif e.event_type == "shipment_excursion":
            entry["detail"] = {
                "temp_c": p["temp_c"],
                "allowed_range_c": [p["limit_low_c"], p["limit_high_c"]],
                "reading_local": p["reading_local"], "tz": p["tz"],
                "reported_by": p.get("reported_by", ""),
                "resolution": None,  # 处置结果在后续节点补全
            }

        elif e.event_type == "excursion_resolved":
            entry["detail"] = {
                "result": p["result"],
                "result_label": {
                    "released": "医学评估后放行",
                    "released_with_note": "附条件放行",
                    "discarded": "报废",
                    "diverted": "改送",
                }.get(p["result"], p["result"]),
                "decided_by": p["decided_by"],
                "decided_local": format_local(
                    datetime.fromisoformat(p["decided_utc"]), viewer_tz),
                "note": p.get("note", ""),
            }

        elif e.event_type in ("product_accepted", "product_rejected"):
            entry["detail"] = {
                "by_person": p.get("by", ""),
                "reason_or_note": p.get("reason", p.get("note", "")),
            }

        elif e.event_type == "infusion_completed":
            entry["detail"] = {
                "operator": p.get("operator", ""),
                "product_id": p.get("product_id", ""),
                "adopted_consent_version": p.get("effective_consent_version"),
            }

        elif e.event_type == "case_cancelled":
            entry["detail"] = {
                "reason": p.get("reason", ""), "cause_role": p.get("cause_role", ""),
                "stage": p.get("stage", ""),
            }

        elif e.event_type == "donor_swapped":
            entry["detail"] = {
                "replacement_case_id": p.get("replacement_case_id", ""),
                "reason": p.get("reason", ""),
            }

        elif e.event_type == "case_note":
            entry["detail"] = {"text": p.get("text", "")}

        entries.append(entry)

    # 回填每个异常的处置结果
    pending = []
    for entry in entries:
        if entry["node"] == "shipment_excursion":
            pending.append(entry)
        elif entry["node"] == "excursion_resolved" and pending:
            target = pending.pop(0)
            target["detail"]["resolution"] = {
                "at_seq": entry["seq"], **entry["detail"]}

    chain = ledger.verify_chain()
    dup_count = sum(
        1 for e in ledger.events_for(case_id)
        if e.external_event_id or e.idem_key)

    return {
        "case_id": case_id,
        "product_id": snap.product["product_id"] if snap.product else None,
        "status": snap.status.value,
        "generated_for_timezone": viewer_tz,
        "generated_utc": now_utc().isoformat(),
        "consent_ledger": [
            {"version": c.version, "action": c.action,
             "document_version": c.document_version, "document_hash": c.document_hash,
             "signed_local": c.signed_local, "tz": c.tz,
             "witness_party": c.witness_party, "event_seq": c.event_seq}
            for c in snap.consents
        ],
        "current_consent_state": snap.consent_state.value,
        "entries": entries,
        "open_excursions": [
            {"detected_utc": x.detected_utc, "temp_c": x.temp_c}
            for x in snap.excursions if x.resolution is None],
        "integrity": chain,
        "idempotency_markers": dup_count,
    }


def render_timeline_text(timeline: dict) -> str:
    """把时间线渲染为便于打印归档的纯文本。"""
    lines = [
        f"监管复核时间线  病例 {timeline['case_id']}"
        f"  采集物 {timeline.get('product_id') or '（尚未采集）'}",
        f"当前状态: {timeline['status']}    "
        f"当前同意状态: {timeline['current_consent_state']}    "
        f"展示时区: {timeline['generated_for_timezone']}",
        "-" * 78,
    ]
    for en in timeline["entries"]:
        mark = "★" if en["critical"] else " "
        lines.append(
            f"{mark}#{en['seq']:>3} {en['at_local']}  {en['label']}"
            f"  [{en['actor_role']}]")
        c = en["consent_in_force"]
        if en["critical"]:
            if c.get("consent_present"):
                lines.append(
                    f"      采用同意: v{c['version']}({c['state']}) "
                    f"文书 {c['document_version']} hash={c['document_hash'][:16]}…")
            else:
                lines.append("      采用同意: 【缺失！】")
        d = en["detail"]
        if d:
            for k, v in d.items():
                if v not in (None, "", [], {}):
                    lines.append(f"        - {k}: {v}")
    lines.append("-" * 78)
    ok = "通过" if timeline["integrity"]["ok"] else "失败"
    lines.append(f"哈希链校验: {ok}（事件 {timeline['integrity'].get('events')} 条）")
    if timeline["open_excursions"]:
        lines.append(f"未闭合温控异常: {len(timeline['open_excursions'])} 起")
    return "\n".join(lines)
