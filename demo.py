#!/usr/bin/env python3
"""端到端业务演示：一个跨越分库、采集医院、冷链、受者医院的完整协同。

运行：python3 demo.py
演示重点：
1. 短信与中心回调重复到达，同一配型事件只推进一次；
2. 窗口按各中心所在地时间解释；
3. 供者撤回（改期后）只惊动真正要动作的人；
4. 替代供者启用，新病例重走流程；
5. 途中温控异常 → 特许放行，处置闭环；
6. 任一采集物的监管时间线（同意版本、交接人、异常结论、哈希链校验）；
7. 不同角色看到的脱敏视图。
"""

import json
from datetime import datetime, timezone

from coord import states as S
from coord.app import seed_demo
from coord.clock import Clock
from coord.identity import Person
from coord.projection import project_case
from coord.timeline import build_product_timeline

C_RESET, C_DIM, C_BOLD = "\033[0m", "\033[2m", "\033[1m"
C_GREEN, C_RED, C_YELLOW, C_CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def banner(title):
    print(f"\n{C_BOLD}{C_CYAN}━━━ {title} ━━━{C_RESET}")


def show_notifications(result, only_unsuppressed=False):
    for n in result.get("notifications", []):
        if only_unsuppressed and n["suppressed"]:
            continue
        tag = f"{C_YELLOW}未打扰{C_RESET}" if n["suppressed"] else f"{C_GREEN}通知{C_RESET}"
        print(f"  {tag} → {n['audience_role']:<18} {n['subject']}")
        if n["suppressed"]:
            print(f"         {C_DIM}原因：{n['suppress_reason']}{C_RESET}")


def main():
    clock = Clock(datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc))
    from coord.app import App
    app = App(clock)
    seed_demo(app)
    wf = app.workflow

    def go(etype, payload, ext, source="api", actor="u_coord", **kw):
        return wf.ingest(etype, payload, external_id=ext, source=source,
                         actor_id=actor, **kw)

    banner("1. 受者医院发起检索，分库收到非血缘配型成功（短信 + 中心回调重复到达）")
    sr = wf.open_search(actor_id="u_doc", transplant_org_id="HOSP-SH",
                        hla_summary="HLA 高分辨 10/10 相合", urgency="urgent")
    match_payload = {"search_id": sr.id, "donor_person_id": "donor-77",
                     "donor_org_id": "REG-XJ", "collection_org_id": "HOSP-BJ",
                     "carrier_org_id": "COLD-CHAIN"}
    r1 = go(S.EV_MATCH_SUCCESS, match_payload, "SMS-20260918-01", source="sms")
    cid = r1["case_id"]
    print(f"  首次（短信） applied={r1['applied']}，病例 {r1['case_id']} 进入 {r1['case_phase']}")
    r1b = go(S.EV_MATCH_SUCCESS, match_payload, "CB-RETRY-01", source="callback")
    print(f"  重复（回调） applied={r1b['applied']}，duplicate_of={r1b['duplicate_of']}：状态不二次推进")
    show_notifications(r1)

    banner("2. 筛查通过 → 供者签署同意 v1（历史版本不可改写）")
    go(S.EV_SCREENING_RESULT,
       {"pass": True, "summary": "高分辨复核与体检合格", "exam_ref": "HR-EX-09"},
       "CB-200", "callback", case_id=cid)
    go(S.EV_CONSENT_GRANTED,
       {"document_ref": "CONS-2026-0778", "signed_at": "2026-09-16T10:30:00+08:00",
        "scopes": [S.SCOPE_HR_TYPING, S.SCOPE_MEDICAL_EXAM, S.SCOPE_COLLECTION, S.SCOPE_FOLLOWUP]},
       "CB-201", "callback", "u_daff", case_id=cid)

    banner("3. 采集窗口：各中心按所在地时间解释")
    go(S.EV_SLOT_PROPOSED,
       {"start_local": "2026-09-25T09:00", "end_local": "2026-09-25T14:00",
        "reason": "初拟窗口"}, "CB-210", "sms", "u_coll", case_id=cid)
    go(S.EV_SLOT_CONFIRMED, {}, "CB-211", "callback", case_id=cid)
    slot = app.store.cases[cid].slots[0]
    from coord.clock import local_view
    bj = local_view(slot.planned_start_utc, "Asia/Shanghai")
    xj = local_view(slot.planned_start_utc, "Asia/Urumqi")
    print(f"  同一绝对时刻 {slot.to_dict()['planned_start_utc']}")
    print(f"  采集医院（{bj['tz']}）解释为 {bj['date']} {bj['time']}")
    print(f"  新疆分库（{xj['tz']}）解释为 {xj['date']} {xj['time']}")

    banner("4. 受者床位紧张触发改期，供者侧也需重新确认；随后窗口重定")
    r = go(S.EV_SLOT_RESCHEDULE, {
        "reason": "受者层流病房床位被占用", "requested_by": "recipient",
        "new_start_local": "2026-09-27T09:00",
        "new_end_local": "2026-09-27T14:00"}, "CB-220", "phone", case_id=cid)
    show_notifications(r, only_unsuppressed=True)
    go(S.EV_SLOT_CONFIRMED, {"slot_version": 2}, "CB-221", "callback", case_id=cid)

    banner("5. 供者在采集前撤回同意：只通知此刻必须动作的人")
    r = go(S.EV_CONSENT_WITHDRAWN, {
        "document_ref": "WDN-2026-0901", "signed_at": "2026-09-26T18:00:00+08:00",
        "note": "供者家庭原因"}, "CB-300", "hotline", "u_daff", case_id=cid)
    print(f"  病例阶段 → {app.store.cases[cid].phase}；生效同意置空，原 v1 保留")
    show_notifications(r)
    versions = app.store.cases[cid].consent_versions
    print(f"  同意链：v{versions[0].version} {versions[0].kind}（原件 {versions[0].document_ref}，未改写）"
          f" → v{versions[1].version} {versions[1].kind}（supersedes v{versions[1].supersedes_version}）")

    banner("6. 启用替代供者：旧病例挂起留痕，新病例重走全流程")
    app.directory.add_person(Person("donor-91", "替代供者", "志愿捐献者", (),
                                    "REG-XJ", [], ""))
    r = go(S.EV_ALT_DONOR, {"donor_person_id": "donor-91", "case_code": "CASE-2026-A91"},
           "CB-400", actor="u_coord", case_id=cid)
    new_id = r["case_id"]
    print(f"  旧病例 {cid} superseded=True，replaced_by={new_id}")
    print(f"  新病例 {new_id}（{r['case_phase']}），同意/窗口全部重来")

    # 新病例快速走完全流程
    go(S.EV_SCREENING_RESULT, {"pass": True, "summary": "合格", "exam_ref": "HR-EX-91"},
       "CB-500", case_id=new_id)
    go(S.EV_CONSENT_GRANTED,
       {"document_ref": "CONS-2026-0911", "signed_at": "2026-09-20T10:00:00+08:00"},
       "CB-501", "callback", "u_daff", case_id=new_id)
    go(S.EV_SLOT_PROPOSED,
       {"start_local": "2026-09-28T09:00", "end_local": "2026-09-28T14:00"},
       "CB-510", actor="u_coll", case_id=new_id)
    go(S.EV_SLOT_CONFIRMED, {}, "CB-511", case_id=new_id)
    go(S.EV_COLLECTION_STARTED, {}, "CB-520", actor="u_coll", case_id=new_id)
    product_code = "HSC-2026-A91"
    go(S.EV_COLLECTION_COMPLETED,
       {"product_code": product_code, "collected_by": "u_coll"},
       "CB-521", actor="u_coll", case_id=new_id)
    go(S.EV_HANDOVER, {
        "product_code": product_code, "from_person_id": "u_coll",
        "to_org_id": "COLD-CHAIN", "to_person_id": "u_car",
        "temp_c": 5.1, "at_utc": "2026-09-28T02:20:00Z"},
       "CB-530", "device", "u_car", case_id=new_id)

    banner("7. 途中温控异常：只惊动处置者与决策者，采集医院不被打扰")
    r = go(S.EV_TEMP_ALERT, {
        "product_code": product_code, "temp_c": 11.8, "duration_minutes": 35,
        "detected_at_utc": "2026-09-28T03:40:00Z"}, "IOT-ALERT-1", "iot", "u_car",
        case_id=new_id)
    show_notifications(r)
    prod = next(p for p in app.store.products.values() if p.code == product_code)
    exc = prod.excursions[-1]

    banner("8. 主治医生评估后特许放行（waiver 留档），运输继续")
    go(S.EV_TEMP_RESOLVED, {
        "excursion_id": exc.id, "disposition": S.DISP_RELEASE_WAIVER,
        "waiver_ref": "WV-2026-031", "note": "短时升温，复检细胞活性与无菌合格，医学特许放行"},
       "CB-600", actor="u_doc", case_id=new_id)
    go(S.EV_HANDOVER, {
        "product_code": product_code, "from_person_id": "u_car",
        "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
        "temp_c": 6.2, "at_utc": "2026-09-28T07:05:00Z"},
       "CB-610", "device", "u_recv", case_id=new_id)
    go(S.EV_DELIVERY, {"product_code": product_code}, "CB-620", actor="u_recv", case_id=new_id)
    r = go(S.EV_INFUSION, {
        "product_code": product_code, "physician_id": "u_doc",
        "infused_at": "2026-09-28T09:30:00Z"}, "CB-630", actor="u_doc", case_id=new_id)
    print(f"  回输完成，病例阶段 → {r['case_phase']}")

    banner("9. 监管复核：面向采集物的完整时间线")
    tl = build_product_timeline(
        store=app.store, directory=app.directory, audit=app.audit,
        clock=app.clock, product_code=product_code,
        viewer=app.directory.people["u_reg"])
    print(f"  节点数 {len(tl['nodes'])}；交接 {len(tl['handovers'])} 棒；"
          f"温控异常 {len(tl['excursions'])} 起")
    print(f"  采集采用同意版本：v{tl['adopted_consent_version']}"
          f"（{tl['adopted_consent']['document_ref']}）")
    for h in tl["handovers"]:
        print(f"    交接#{h['seq']} {h['from_person']['name']} → {h['to_person']['name']}"
              f"｜{h['temp_c']}℃ 合格={h['temp_ok']} 封条完好={h['sealed']}")
    e0 = tl["excursions"][0]
    print(f"  异常处置：{e0['temp_c']}℃ / {e0['duration_minutes']}min"
          f" → {e0['outcome']}（决定人 {e0['decider']['name']}，放行单 {e0['waiver_ref']}）")
    print(f"  哈希链完整性：{C_GREEN if tl['integrity']['ok'] else C_RED}"
          f"{tl['integrity']['ok']}{C_RESET}，head={tl['integrity']['head'][:16]}…")

    banner("10. 按职责最小化展示")
    case = app.store.cases[new_id]
    car_view = project_case(case, app.directory.people["u_car"], app.directory, app.store)
    doc_view = project_case(case, app.directory.people["u_doc"], app.directory, app.store)
    print(f"  押运员视角供者：{car_view['donor']}；可见 HLA：{'hla_summary' in car_view}")
    print(f"  主治医生视角供者：masked={doc_view['donor']['masked']}；"
          f"HLA={doc_view.get('hla_summary')}；同意书编号可见={bool(doc_view['consent_versions'] and doc_view['consent_versions'][0]['document_ref'])}")

    print(f"\n{C_BOLD}演示完成。{C_RESET}")


if __name__ == "__main__":
    main()
