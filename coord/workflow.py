"""事件驱动的协同工作流引擎。

一次 ingest 调用在同一把存储锁内完成：
  1. 幂等判定（自然键/显式 idem）——重复事件只留痕，不推进；
  2. 领域规则校验与状态迁移；
  3. 审计哈希链追加（每次状态推进都可复核）；
  4. 精准受众解析与通知生成。
"""

from . import states as S
from .audit import GENESIS  # noqa: F401  (便于外部引用链根)
from .clock import as_utc, interpret_local, iso, parse_instant
from .errors import RuleViolation, ValidationError
from .models import (
    Case, ConsentVersion, Handover, Product, SearchRequest, Slot, TempExcursion,
)
from .notifier import Notifier
from .routing import resolve as resolve_audience
from .store import EventRecord

# 允许的阶段迁移（终止/异常迁移在处理器内按规则放行）
ALLOWED = {
    S.MATCHED: {S.SCREENING, S.CANCELLED},
    S.SCREENING: {S.SCREENING, S.CONSENTED, S.DONOR_WITHDRAWN, S.CANCELLED},
    S.CONSENTED: {S.SCHEDULED, S.DONOR_WITHDRAWN, S.CANCELLED},
    S.SCHEDULED: {S.CONSENTED, S.COLLECTED, S.DONOR_WITHDRAWN, S.CANCELLED},
    S.COLLECTED: {S.IN_TRANSIT, S.RECOLLECT, S.CANCELLED, S.DONOR_WITHDRAWN},
    S.IN_TRANSIT: {S.DELIVERED, S.RECOLLECT},
    S.DELIVERED: {S.INFUSED, S.RECOLLECT},
    S.INFUSED: {S.CLOSED},
    S.DONOR_WITHDRAWN: set(),
    S.RECOLLECT: {S.CONSENTED, S.CANCELLED},
    S.CANCELLED: set(),
}

GRANT_SCOPES_DEFAULT = (S.SCOPE_HR_TYPING, S.SCOPE_MEDICAL_EXAM,
                        S.SCOPE_COLLECTION, S.SCOPE_FOLLOWUP)


def require_fields(payload, fields):
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise ValidationError(f"缺少必填字段: {', '.join(missing)}")


class Workflow:
    def __init__(self, store, directory, clock, audit):
        self.store = store
        self.directory = directory
        self.clock = clock
        self.audit = audit
        self.notifier = Notifier(store, directory, clock, audit)

    # ============ 建账 ============
    def open_search(self, *, actor_id, transplant_org_id, hla_summary,
                    urgency="routine", case_code_hint=""):
        org = self.directory.require_org(transplant_org_id)
        if org.kind != "transplant_hospital":
            raise ValidationError("检索需求必须由受者医院发起")
        with self.store.lock:
            sid = self.store.next_id("SR")
            code = case_code_hint or f"SRCH-{sid.split('_')[-1]}"
            search = SearchRequest(
                id=sid, case_code=code, transplant_org_id=org.id,
                hla_summary=hla_summary, urgency=urgency, created_utc=self.clock.now(),
            )
            self.store.searches[sid] = search
            self.audit.append(
                ts=self.clock.now(), actor_id=actor_id, action="search.opened",
                ref_type="search", ref_id=sid, payload=search.to_dict(),
            )
            return search

    # ============ 事件入口 ============
    def ingest(self, event_type, payload=None, *, external_id, source,
               actor_id="system", idem=None, case_id=None):
        payload = dict(payload or {})
        with self.store.lock:
            # 0) 送达回执走专门通道（不改病例状态）
            if event_type == S.EV_DELIVERY_RECEIPT:
                return self._handle_receipt(payload, external_id, source, actor_id, idem)

            # 1) 定位病例并计算自然键
            case = self._locate_case(event_type, payload, case_id)
            key = idem or self._natural_key(event_type, payload, case)

            existing_id = self.store.has_event(key)
            now = self.clock.now()
            record = EventRecord(
                id=self.store.next_id("ev"),
                external_id=external_id,
                type=event_type,
                case_id=case.id if case else payload.get("case_id"),
                payload=payload,
                idempotency_key=key,
                received_utc=now,
                actor_id=actor_id,
                source=source,
            )
            if existing_id is not None:
                dup = self.store.mark_duplicate(key, record)
                original = self.store.get_event(existing_id)
                return {
                    "applied": False,
                    "event": dup.to_dict(),
                    "duplicate_of": existing_id,
                    "case_id": case.id if case else None,
                    "notifications": [],
                    "message": f"与事件 {existing_id} 同一外部事项，状态仅推进一次",
                    "original_effect": getattr(original, "effect_note", ""),
                }

            # 2) 应用状态迁移
            if event_type == S.EV_MATCH_SUCCESS:
                case = self._h_match(payload, actor_id, record)
            else:
                if case is None:
                    raise ValidationError("无法定位病例；请在 payload 提供 case_id 或 case_code")
                handler = {
                    S.EV_SCREENING_RESULT: self._h_screening,
                    S.EV_CONSENT_GRANTED: self._h_consent_grant,
                    S.EV_CONSENT_WITHDRAWN: self._h_consent_withdraw,
                    S.EV_SLOT_PROPOSED: self._h_slot_propose,
                    S.EV_SLOT_CONFIRMED: self._h_slot_confirm,
                    S.EV_SLOT_RESCHEDULE: self._h_reschedule,
                    S.EV_SLOT_CANCEL: self._h_slot_cancel,
                    S.EV_ALT_DONOR: self._h_alt_donor,
                    S.EV_COLLECTION_STARTED: self._h_collection_start,
                    S.EV_COLLECTION_COMPLETED: self._h_collection_done,
                    S.EV_HANDOVER: self._h_handover,
                    S.EV_TEMP_ALERT: self._h_temp_alert,
                    S.EV_TEMP_RESOLVED: self._h_temp_resolve,
                    S.EV_DELIVERY: self._h_delivery,
                    S.EV_INFUSION: self._h_infusion,
                    S.EV_CASE_CANCEL: self._h_case_cancel,
                }.get(event_type)
                if handler is None:
                    raise ValidationError(f"未知事件类型: {event_type}")
                result = handler(payload, actor_id, record, case)
                case = result if isinstance(result, Case) else case

            record.case_id = case.id
            self.store.record_event(record)
            case.updated_utc = now

            # 3) 精准受众 + 通知
            product = self._resolve_product(event_type, payload)
            audience = resolve_audience(event_type, payload, case, product, self.directory)
            context = self._context(event_type, payload, case, product)
            notifications = self.notifier.emit(
                case=case, event=record, audience=audience, context=context,
            )

            record.effect_note = self._summarize(event_type, payload, case, product)
            return {
                "applied": True,
                "event": record.to_dict(),
                "case_id": case.id,
                "case_phase": case.phase,
                "notifications": [n.to_dict() for n in notifications],
                "message": record.effect_note,
            }

    # ---------- 自然键 ----------
    def _natural_key(self, event_type, p, case):
        cid = case.id if case else p.get("case_id") or p.get("case_code", "?")
        table = {
            S.EV_MATCH_SUCCESS: lambda: f"match:{p.get('search_id')}:{p.get('donor_person_id')}",
            S.EV_SCREENING_RESULT: lambda: f"screen:{cid}:{p.get('exam_ref', 'default')}",
            S.EV_CONSENT_GRANTED: lambda: f"grant:{cid}:{p.get('document_ref')}:{p.get('signed_at')}",
            S.EV_CONSENT_WITHDRAWN: lambda: f"withdraw:{cid}:{p.get('document_ref')}:{p.get('signed_at')}",
            S.EV_SLOT_PROPOSED: lambda: f"propose:{cid}:{p.get('start_local', p.get('start_utc'))}",
            S.EV_SLOT_CONFIRMED: lambda: f"confirm:{cid}:{self._slot_version(case, p)}",
            S.EV_SLOT_RESCHEDULE: lambda: f"resched:{cid}:{self._slot_version(case, p)}:{p.get('new_start_local', p.get('new_start_utc', 'none'))}",
            S.EV_SLOT_CANCEL: lambda: f"slotcancel:{cid}:{self._slot_version(case, p)}",
            S.EV_COLLECTION_STARTED: lambda: f"collstart:{cid}:{self._slot_version(case, p)}",
            S.EV_COLLECTION_COMPLETED: lambda: f"colldone:{cid}:{p.get('product_code')}",
            S.EV_HANDOVER: lambda: f"handover:{p.get('product_code')}:{p.get('from_org_id', '?')}>{p.get('to_org_id')}:{p.get('at_utc', 'now')}",
            S.EV_TEMP_ALERT: lambda: f"tempalert:{p.get('product_code')}:{p.get('detected_at_utc', p.get('alert_ref', 'now'))}",
            S.EV_TEMP_RESOLVED: lambda: f"tempres:{p.get('excursion_id')}:{p.get('disposition')}",
            S.EV_DELIVERY: lambda: f"delivery:{p.get('product_code')}",
            S.EV_INFUSION: lambda: f"infusion:{p.get('product_code')}",
            S.EV_CASE_CANCEL: lambda: f"casecancel:{cid}",
            S.EV_ALT_DONOR: lambda: f"alt:{p.get('search_id')}:{p.get('donor_person_id')}",
        }
        fn = table.get(event_type)
        if fn is None:
            raise ValidationError(f"事件类型 {event_type} 必须显式提供 idem 幂等键")
        return fn()

    @staticmethod
    def _slot_version(case, p):
        if p.get("slot_version") is not None:
            return p["slot_version"]
        slot = case.active_slot() if case else None
        return slot.version if slot else "none"

    def _locate_case(self, event_type, p, case_id):
        if event_type == S.EV_MATCH_SUCCESS:
            return None
        if event_type == S.EV_ALT_DONOR:
            sid = p.get("search_id")
            if sid:
                for c in self.store.cases.values():
                    if c.search_id == sid and not c.superseded:
                        return c
                raise ValidationError(f"检索 {sid} 无现存病例，无法启用替代供者")
            # 未显式给 search_id 时，用已定位的终态病例
            cid = case_id or p.get("case_id")
            if cid and cid in self.store.cases:
                return self.store.cases[cid]
            code = p.get("case_code")
            if code:
                return self.store.find_case(code=code, active_only=False)
            raise ValidationError("启用替代供者需提供 search_id 或 case_id")
        cid = case_id or p.get("case_id")
        if cid and cid in self.store.cases:
            return self.store.cases[cid]
        code = p.get("case_code")
        if code:
            found = self.store.find_case(code=code, active_only=False)
            if found:
                return found
        if event_type in (S.EV_COLLECTION_COMPLETED, S.EV_HANDOVER,
                          S.EV_TEMP_ALERT, S.EV_TEMP_RESOLVED,
                          S.EV_DELIVERY, S.EV_INFUSION) and p.get("product_code"):
            prod = next((x for x in self.store.products.values()
                         if x.code == p["product_code"]), None)
            if prod:
                return self.store.cases.get(prod.case_id)
        if cid:
            from .errors import NotFound
            raise NotFound(f"病例不存在: {cid}")
        return None

    # ---------- 阶段迁移 ----------
    def _move(self, case, new_phase, record, payload_extra=None, note=""):
        if new_phase != case.phase and new_phase not in ALLOWED.get(case.phase, set()):
            raise RuleViolation(
                f"非法状态迁移：{case.phase} → {new_phase}",
                details={"case": case.code, "from": case.phase, "to": new_phase},
            )
        old = case.phase
        case.phase = new_phase
        case.mark(new_phase)
        self.audit.append(
            ts=self.clock.now(), actor_id=record.actor_id, action="case.phase_change",
            case_id=case.id, ref_type="case", ref_id=case.id,
            payload={"from": old, "to": new_phase, **(payload_extra or {})},
            event_id=record.id, note=note,
        )

    # ============ 各事件处理器 ============
    def _h_match(self, p, actor_id, record):
        require_fields(p, ["search_id", "donor_person_id", "donor_org_id"])
        search = self.store.searches.get(p["search_id"])
        if not search:
            raise ValidationError(f"检索需求不存在: {p['search_id']}")
        donor = self.directory.people.get(p["donor_person_id"])
        if not donor:
            raise ValidationError(f"供者人员不存在: {p['donor_person_id']}")
        if donor.org_id != p["donor_org_id"]:
            raise ValidationError("供者归属机构与人员目录不一致")
        existing = next((c for c in self.store.cases.values()
                         if c.search_id == search.id
                         and c.donor_person_id == p["donor_person_id"]
                         and not c.superseded), None)
        if existing:
            raise RuleViolation("该供者在本检索下已有进行中病例")

        cid = self.store.next_id("CASE")
        seq = cid.split("_")[-1]
        case = Case(
            id=cid,
            code=p.get("case_code") or f"CASE-{self.clock.now().year}-{seq}",
            search_id=search.id,
            donor_person_id=donor.id,
            donor_org_id=p["donor_org_id"],
            transplant_org_id=search.transplant_org_id,
            collection_org_id=p.get("collection_org_id"),
            carrier_org_id=p.get("carrier_org_id"),
            phase=S.MATCHED,
            created_utc=self.clock.now(),
            updated_utc=self.clock.now(),
        )
        if case.collection_org_id:
            self.directory.require_org(case.collection_org_id)
        if case.carrier_org_id:
            self.directory.require_org(case.carrier_org_id)
        self.store.cases[cid] = case
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="case.opened",
            case_id=cid, ref_type="case", ref_id=cid,
            payload={"code": case.code, "search_id": search.id,
                     "donor_person_id": donor.id},
            event_id=record.id, note="非血缘配型成功，建案",
        )
        return case

    def _h_screening(self, p, actor_id, record, case):
        require_fields(p, ["pass"])
        # 初次筛查推进阶段；同意/排期后的补充复查（如复检）允许更新结论，不回退阶段
        if case.phase not in (S.MATCHED, S.SCREENING, S.CONSENTED, S.SCHEDULED):
            raise RuleViolation(f"当前阶段 {case.phase} 不能录入筛查结果")
        case.screening_pass = bool(p["pass"])
        case.screening_summary = p.get("summary", case.screening_summary)
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="screening.recorded",
            case_id=case.id, ref_type="case", ref_id=case.id,
            payload={"pass": case.screening_pass,
                     "summary": case.screening_summary,
                     "exam_ref": p.get("exam_ref")},
            event_id=record.id,
        )
        if case.phase == S.MATCHED:
            self._move(case, S.SCREENING, record)

    def _next_consent_version(self, case):
        return (max((c.version for c in case.consent_versions), default=0)) + 1

    def _h_consent_grant(self, p, actor_id, record, case):
        require_fields(p, ["document_ref", "signed_at"])
        if case.screening_pass is not True:
            raise RuleViolation("供者筛查通过前不能登记采集同意")
        if case.phase not in (S.SCREENING, S.CONSENTED, S.SCHEDULED):
            raise RuleViolation(f"当前阶段 {case.phase} 不能登记同意")
        if any(c.document_ref == p["document_ref"] for c in case.consent_versions):
            raise RuleViolation("同意书编号已存在，拒绝重复登记（如为更新材料请使用新编号）")
        signed = parse_instant(p["signed_at"], self.directory.org_tz(case.donor_org_id))
        scopes = tuple(p.get("scopes") or GRANT_SCOPES_DEFAULT)
        version = self._next_consent_version(case)
        cv = ConsentVersion(
            case_id=case.id, version=version, kind=S.CONSENT_GRANT, scopes=scopes,
            donor_person_id=case.donor_person_id, signed_utc=signed,
            document_ref=p["document_ref"], recorded_by=actor_id,
            note=p.get("note", ""),
        )
        case.consent_versions.append(cv)
        case.effective_consent_version = version
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="consent.granted",
            case_id=case.id, ref_type="consent", ref_id=f"{case.id}/v{version}",
            payload=cv.to_dict(), event_id=record.id,
            note="追加同意新版本；历史版本保留不变",
        )
        if case.phase == S.SCREENING:
            self._move(case, S.CONSENTED, record)

    def _h_consent_withdraw(self, p, actor_id, record, case):
        if case.effective_consent_version is None:
            raise RuleViolation("无生效同意，不能撤回")
        if any(c.kind == S.CONSENT_WITHDRAWAL
               and c.supersedes_version == case.effective_consent_version
               for c in case.consent_versions):
            raise RuleViolation("该同意版本已被撤回，不能重复撤回")
        collected = [self.store.products[pid] for pid in case.product_ids
                     if pid in self.store.products]
        if any(prod.status in (S.PROD_IN_TRANSIT, S.PROD_DELIVERED, S.PROD_INFUSED)
               for prod in collected):
            raise RuleViolation("产品已进入运输/回输环节，撤回须走医疗与伦理应急流程，系统拒绝直接改写")
        require_fields(p, ["document_ref", "signed_at"])
        signed = parse_instant(p["signed_at"], self.directory.org_tz(case.donor_org_id))
        base = case.effective_consent_version
        version = self._next_consent_version(case)
        wv = ConsentVersion(
            case_id=case.id, version=version, kind=S.CONSENT_WITHDRAWAL, scopes=(),
            donor_person_id=case.donor_person_id, signed_utc=signed,
            document_ref=p["document_ref"], recorded_by=actor_id,
            supersedes_version=base, note=p.get("note", ""),
        )
        case.consent_versions.append(wv)
        case.effective_consent_version = None
        # 已确认窗口随撤回释放
        slot = case.active_slot()
        if slot and slot.status != S.SLOT_CANCELLED:
            slot.status = S.SLOT_CANCELLED
            slot.reason = f"供者撤回同意：{p.get('note', '')}".strip("：")
            self.audit.append(
                ts=self.clock.now(), actor_id=actor_id, action="slot.cancelled",
                case_id=case.id, ref_type="slot", ref_id=slot.id,
                payload={"version": slot.version, "reason": "供者撤回同意"},
                event_id=record.id,
            )
        # 已采集但未启运的产品标记拒绝使用
        for prod in collected:
            prod.status = S.PROD_REJECTED
            prod.rejection_reason = "供者撤回同意"
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="consent.withdrawn",
            case_id=case.id, ref_type="consent", ref_id=f"{case.id}/v{version}",
            payload=wv.to_dict(), event_id=record.id,
            note="撤回以新版本追加；原同意版本保留且不可改写",
        )
        self._move(case, S.DONOR_WITHDRAWN, record, {"supersedes_version": base})

    def _parse_window(self, case, p, prefix=""):
        org_id = case.collection_org_id
        if not org_id:
            raise RuleViolation("病例尚未指定采集医院，无法解释采集窗口")
        tz = self.directory.org_tz(org_id)
        sk, ek = f"{prefix}start_local", f"{prefix}end_local"
        su, eu = f"{prefix}start_utc", f"{prefix}end_utc"
        if p.get(sk) and p.get(ek):
            start = interpret_local(p[sk], tz)
            end = interpret_local(p[ek], tz)
        elif p.get(su) and p.get(eu):
            start = parse_instant(p[su])
            end = parse_instant(p[eu])
        else:
            raise ValidationError(f"需提供 {sk}/{ek}（采集医院当地时间）或 {su}/{eu}")
        if end <= start:
            raise ValidationError("窗口结束时间必须晚于开始时间")
        return start, end, tz

    def _h_slot_propose(self, p, actor_id, record, case):
        require_fields(p, ["collection_org_id"] if not case.collection_org_id else [])
        if p.get("collection_org_id"):
            self.directory.require_org(p["collection_org_id"])
            case.collection_org_id = p["collection_org_id"]
        if case.effective_consent_version is None:
            raise RuleViolation("缺少生效同意，不能排期")
        if case.phase not in (S.CONSENTED, S.SCHEDULED, S.RECOLLECT):
            raise RuleViolation(f"当前阶段 {case.phase} 不能拟议窗口")
        active = case.active_slot()
        if active and active.status == S.SLOT_CONFIRMED:
            raise RuleViolation("已确认窗口的变动必须走改期事件 slot.reschedule_requested")
        # 上一产品报废重采：同意仍有效，回到待排期重新拟议窗口
        if case.phase == S.RECOLLECT:
            self._move(case, S.CONSENTED, record, note="上一产品报废，供者同意仍有效，进入重采排期")
        start, end, tz = self._parse_window(case, p)
        version = (max((s.version for s in case.slots), default=0)) + 1
        slot = Slot(
            id=self.store.next_id("slot"),
            case_id=case.id, version=version, status=S.SLOT_PROPOSED,
            collection_org_id=case.collection_org_id,
            planned_start_utc=start, planned_end_utc=end, tz=tz,
            reason=p.get("reason", ""), requested_by=p.get("requested_by", ""),
            proposed_utc=self.clock.now(),
        )
        if active:
            active.status = S.SLOT_CANCELLED
            active.reason = "被更新的拟议窗口替代"
        case.slots.append(slot)
        case.active_slot_id = slot.id
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="slot.proposed",
            case_id=case.id, ref_type="slot", ref_id=slot.id,
            payload={**slot.to_dict()}, event_id=record.id,
            note=f"窗口按采集医院所在地时区 {tz} 解释",
        )

    def _h_slot_confirm(self, p, actor_id, record, case):
        slot = case.active_slot()
        if not slot or slot.status != S.SLOT_PROPOSED:
            raise RuleViolation("没有待确认的拟议窗口")
        if p.get("slot_version") and p["slot_version"] != slot.version:
            raise RuleViolation("确认的窗口版本与当前待确认版本不一致")
        if p.get("carrier_org_id"):
            self.directory.require_org(p["carrier_org_id"])
            case.carrier_org_id = p["carrier_org_id"]
        slot.status = S.SLOT_CONFIRMED
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="slot.confirmed",
            case_id=case.id, ref_type="slot", ref_id=slot.id,
            payload={"version": slot.version,
                     "start_utc": iso(slot.planned_start_utc),
                     "end_utc": iso(slot.planned_end_utc)},
            event_id=record.id,
        )
        self._move(case, S.SCHEDULED, record)

    def _h_reschedule(self, p, actor_id, record, case):
        slot = case.active_slot()
        if not slot or slot.status != S.SLOT_CONFIRMED:
            raise RuleViolation("没有已确认窗口，无需改期")
        require_fields(p, ["reason"])
        old_version = slot.version
        slot.status = S.SLOT_CANCELLED
        slot.reason = f"改期：{p['reason']}"
        case.active_slot_id = None
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="slot.reschedule_requested",
            case_id=case.id, ref_type="slot", ref_id=slot.id,
            payload={"old_version": old_version, "reason": p["reason"],
                     "requested_by": p.get("requested_by", "")},
            event_id=record.id,
        )
        self._move(case, S.CONSENTED, record, note="改期后等待新窗口确认，同意与筛查结论仍然有效")
        # 改期事件可携带新窗口，一步生成待确认的拟议版本
        if p.get("new_start_local") or p.get("new_start_utc"):
            start, end, tz = self._parse_window(case, p, prefix="new_")
            nv = (max((s.version for s in case.slots), default=0)) + 1
            ns = Slot(
                id=self.store.next_id("slot"), case_id=case.id, version=nv,
                status=S.SLOT_PROPOSED, collection_org_id=case.collection_org_id,
                planned_start_utc=start, planned_end_utc=end, tz=tz,
                reason=f"改期：{p['reason']}", requested_by=p.get("requested_by", ""),
                proposed_utc=self.clock.now(),
            )
            case.slots.append(ns)
            case.active_slot_id = ns.id
            self.audit.append(
                ts=self.clock.now(), actor_id=actor_id, action="slot.proposed",
                case_id=case.id, ref_type="slot", ref_id=ns.id,
                payload=ns.to_dict(), event_id=record.id, note="改期附带的新拟议窗口",
            )

    def _h_slot_cancel(self, p, actor_id, record, case):
        slot = case.active_slot()
        if not slot or slot.status == S.SLOT_CANCELLED:
            raise RuleViolation("没有生效中的采集窗口")
        slot.status = S.SLOT_CANCELLED
        slot.reason = p.get("reason", "窗口取消")
        case.active_slot_id = None
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="slot.cancelled",
            case_id=case.id, ref_type="slot", ref_id=slot.id,
            payload={"version": slot.version, "reason": slot.reason},
            event_id=record.id,
        )
        if case.phase == S.SCHEDULED:
            self._move(case, S.CONSENTED, record)

    def _h_alt_donor(self, p, actor_id, record, case):
        require_fields(p, ["donor_person_id"])
        if case.phase not in (S.DONOR_WITHDRAWN, S.CANCELLED, S.RECOLLECT):
            raise RuleViolation("仅当原病例撤回/取消/重采时才能启用替代供者")
        donor = self.directory.people.get(p["donor_person_id"])
        if not donor:
            raise ValidationError(f"供者人员不存在: {p['donor_person_id']}")
        if donor.id == case.donor_person_id:
            raise ValidationError("替代供者不能与原供者相同")
        clash = next((c for c in self.store.cases.values()
                      if c.search_id == case.search_id
                      and c.donor_person_id == donor.id and not c.superseded), None)
        if clash:
            raise RuleViolation("该替代供者在本检索下已有进行中病例")
        cid = self.store.next_id("CASE")
        seq = cid.split("_")[-1]
        new_case = Case(
            id=cid,
            code=p.get("case_code") or f"CASE-{self.clock.now().year}-{seq}-A",
            search_id=case.search_id,
            donor_person_id=donor.id,
            donor_org_id=donor.org_id,
            transplant_org_id=case.transplant_org_id,
            collection_org_id=case.collection_org_id,
            carrier_org_id=case.carrier_org_id,
            phase=S.MATCHED,
            created_utc=self.clock.now(),
            updated_utc=self.clock.now(),
        )
        self.store.cases[cid] = new_case
        case.replaced_by_case_id = cid
        case.superseded = True
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="match.alternative_activated",
            case_id=case.id, ref_type="case", ref_id=cid,
            payload={"old_case": case.id, "new_case": cid,
                     "new_code": new_case.code, "donor_person_id": donor.id},
            event_id=record.id, note="原病例挂起保留，替代供者新建病例重走全流程",
        )
        return new_case

    def _effective_grant(self, case):
        if case.effective_consent_version is None:
            return None
        return case.consent(case.effective_consent_version)

    def _h_collection_start(self, p, actor_id, record, case):
        slot = case.active_slot()
        if not slot or slot.status != S.SLOT_CONFIRMED:
            raise RuleViolation("采集只能在已确认窗口内开始")
        grant = self._effective_grant(case)
        if not grant or S.SCOPE_COLLECTION not in grant.scopes:
            raise RuleViolation("生效同意未覆盖采集事项")
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="collection.started",
            case_id=case.id, ref_type="slot", ref_id=slot.id,
            payload={"slot_version": slot.version,
                     "consent_version": grant.version},
            event_id=record.id, note="采集中采用的同意版本已固定记录",
        )

    def _h_collection_done(self, p, actor_id, record, case):
        require_fields(p, ["product_code", "collected_by"])
        slot = case.active_slot()
        if not slot or slot.status != S.SLOT_CONFIRMED:
            raise RuleViolation("采集完成前必须存在已确认窗口")
        grant = self._effective_grant(case)
        if not grant or S.SCOPE_COLLECTION not in grant.scopes:
            raise RuleViolation("生效同意未覆盖采集事项")
        if any(x.code == p["product_code"] for x in self.store.products.values()):
            raise RuleViolation("产品码已存在，拒绝重复登记")
        collector = self.directory.people.get(p["collected_by"])
        if not collector or collector.org_id != case.collection_org_id:
            raise ValidationError("采集人必须属于采集医院")
        collected_at = parse_instant(p["collected_at"]) if p.get("collected_at") else self.clock.now()
        rng = tuple(p.get("temp_range", (2.0, 8.0)))
        prod = Product(
            id=self.store.next_id("PRD"), code=p["product_code"], case_id=case.id,
            donor_case_ref=f"DONOR-CASE/{case.code}", status=S.PROD_COLLECTED,
            collected_utc=collected_at, collection_org_id=case.collection_org_id,
            consent_version=grant.version, temp_range=rng,
            current_holder_org_id=case.collection_org_id,
            current_holder_person_id=collector.id,
        )
        self.store.products[prod.id] = prod
        case.product_ids.append(prod.id)
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="collection.completed",
            case_id=case.id, product_id=prod.id, ref_type="product", ref_id=prod.id,
            payload={"product_code": prod.code, "consent_version": grant.version,
                     "collected_by": collector.id, "collected_utc": iso(collected_at),
                     "temp_range": list(rng)},
            event_id=record.id,
            note=f"产品采用同意 v{grant.version}；采集人 {collector.id}",
        )
        self._move(case, S.COLLECTED, record)

    def _find_product(self, code):
        prod = next((x for x in self.store.products.values() if x.code == code), None)
        if not prod:
            from .errors import NotFound
            raise NotFound(f"采集物不存在: {code}")
        return prod

    def _resolve_product(self, event_type, p):
        """供通知路由解析关联产品（处置事件只有 excursion_id）。"""
        if p.get("product_code"):
            return next((x for x in self.store.products.values()
                         if x.code == p["product_code"]), None)
        if event_type == S.EV_TEMP_RESOLVED and p.get("excursion_id"):
            for prod in self.store.products.values():
                if any(e.id == p["excursion_id"] for e in prod.excursions):
                    return prod
        return None

    def _h_handover(self, p, actor_id, record, case):
        require_fields(p, ["product_code", "from_person_id", "to_org_id",
                           "to_person_id", "temp_c"])
        prod = self._find_product(p["product_code"])
        if prod.status in (S.PROD_REJECTED, S.PROD_INFUSED):
            raise RuleViolation(f"产品状态 {prod.status}，不能再交接")
        from_person = self.directory.people.get(p["from_person_id"])
        to_person = self.directory.people.get(p["to_person_id"])
        if not from_person or not to_person:
            raise ValidationError("交接人信息缺失")
        if from_person.org_id != prod.current_holder_org_id:
            raise RuleViolation("交出方与当前持有机构不符，交接链中断")
        if to_person.org_id != p["to_org_id"]:
            raise ValidationError("接收人不属于接收机构")
        to_org = self.directory.require_org(p["to_org_id"])
        low, high = prod.temp_range
        temp_ok = low <= float(p["temp_c"]) <= high
        sealed = bool(p.get("sealed", True))
        if not sealed and not p.get("broken_seal_accepted"):
            raise RuleViolation("封条破损且未获破损接收确认，拒绝交接")
        at = parse_instant(p["at_utc"]) if p.get("at_utc") else self.clock.now()
        hv = Handover(
            id=self.store.next_id("HO"), product_id=prod.id, seq=len(prod.handovers) + 1,
            from_org_id=from_person.org_id, from_person_id=from_person.id,
            to_org_id=to_org.id, to_person_id=to_person.id,
            at_utc=at, temp_c=float(p["temp_c"]), temp_ok=temp_ok, sealed=sealed,
            note=p.get("note", ""), event_id=record.id,
        )
        prod.handovers.append(hv)
        prod.current_holder_org_id = to_org.id
        prod.current_holder_person_id = to_person.id
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="chain.handover",
            case_id=case.id, product_id=prod.id, ref_type="handover", ref_id=hv.id,
            payload={**hv.to_dict()}, event_id=record.id,
            note=f"交接 #{hv.seq}：{from_person.id} → {to_person.id}；温度合格={temp_ok}，封条完好={sealed}",
        )
        # 承运方取件 → 运输中
        if to_org.kind == "carrier" and prod.status == S.PROD_COLLECTED:
            prod.status = S.PROD_IN_TRANSIT
            self._move(case, S.IN_TRANSIT, record)
        # 受者医院收货：暂挂“待正式接收”，正式接收由 delivery 事件确认
        if to_org.kind == "transplant_hospital":
            prod.status = "arrived_pending_acceptance"

    def _h_temp_alert(self, p, actor_id, record, case):
        require_fields(p, ["product_code", "temp_c", "duration_minutes"])
        prod = self._find_product(p["product_code"])
        if prod.status not in (S.PROD_IN_TRANSIT, "arrived_pending_acceptance"):
            raise RuleViolation("仅运输中/待接收产品可上报途中温控异常")
        detected = parse_instant(p["detected_at_utc"]) if p.get("detected_at_utc") else self.clock.now()
        exc = TempExcursion(
            id=self.store.next_id("EX"), product_id=prod.id, detected_utc=detected,
            reported_by=actor_id, temp_c=float(p["temp_c"]),
            allowed_range=tuple(prod.temp_range),
            duration_minutes=int(p["duration_minutes"]),
            status=S.TEMP_OPEN, note=p.get("note", ""),
        )
        prod.excursions.append(exc)
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="temp.excursion_reported",
            case_id=case.id, product_id=prod.id, ref_type="excursion", ref_id=exc.id,
            payload=exc.to_dict(), event_id=record.id,
            note=f"温控超限 {exc.temp_c}℃ 持续 {exc.duration_minutes} 分钟，待评估处置",
        )

    def _h_temp_resolve(self, p, actor_id, record, case):
        require_fields(p, ["excursion_id", "disposition"])
        exc = next((e for pid in case.product_ids
                    for e in self.store.products[pid].excursions
                    if e.id == p["excursion_id"]), None)
        if not exc:
            from .errors import NotFound
            raise NotFound(f"温控异常不存在: {p['excursion_id']}")
        if exc.status != S.TEMP_OPEN:
            raise RuleViolation("该异常已处置，结论不可改写；如需纠正请发起新事件")
        if p["disposition"] not in (S.DISP_CONTINUE, S.DISP_RELEASE_WAIVER, S.DISP_RECOLLECT):
            raise ValidationError("处置结论必须为 continue / release_with_waiver / recollect")
        if p["disposition"] == S.DISP_RELEASE_WAIVER and not p.get("waiver_ref"):
            raise ValidationError("特许放行必须附 waiver_ref 放行单编号")
        prod = self.store.products[exc.product_id]
        exc.status = S.TEMP_RESOLVED
        exc.disposition = p["disposition"]
        exc.decision_by = actor_id
        exc.decision_utc = self.clock.now()
        exc.waiver_ref = p.get("waiver_ref")
        exc.note = p.get("note", exc.note)
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="temp.excursion_resolved",
            case_id=case.id, product_id=prod.id, ref_type="excursion", ref_id=exc.id,
            payload=exc.to_dict(), event_id=record.id,
            note=f"处置结论：{p['disposition']}",
        )
        if p["disposition"] == S.DISP_RECOLLECT:
            prod.status = S.PROD_REJECTED
            prod.rejection_reason = f"温控超限报废（{exc.id}）"
            self._release_active_slot(case, record, f"产品报废重采（{exc.id}）")
            self._move(case, S.RECOLLECT, record)

    def _release_active_slot(self, case, record, reason):
        """重采/拒收等情形释放仍挂在病例上的确认窗口，留痕但不重复通知。"""
        slot = case.active_slot()
        if slot and slot.status != S.SLOT_CANCELLED:
            slot.status = S.SLOT_CANCELLED
            slot.reason = reason
            case.active_slot_id = None
            self.audit.append(
                ts=self.clock.now(), actor_id=record.actor_id,
                action="slot.cancelled", case_id=case.id,
                ref_type="slot", ref_id=slot.id,
                payload={"version": slot.version, "reason": reason},
                event_id=record.id,
            )

    def _h_delivery(self, p, actor_id, record, case):
        require_fields(p, ["product_code"])
        prod = self._find_product(p["product_code"])
        if prod.status not in ("arrived_pending_acceptance", S.PROD_IN_TRANSIT):
            raise RuleViolation(f"产品状态 {prod.status}，不能登记送达接收")
        accepted = p.get("accepted", True)
        if not accepted:
            prod.status = S.PROD_REJECTED
            prod.rejection_reason = p.get("reason", "受者医院接收检验拒收")
            self.audit.append(
                ts=self.clock.now(), actor_id=actor_id, action="product.rejected",
                case_id=case.id, product_id=prod.id, ref_type="product", ref_id=prod.id,
                payload={"reason": prod.rejection_reason}, event_id=record.id,
            )
            self._release_active_slot(case, record, "受者医院拒收，需重采")
            self._move(case, S.RECOLLECT, record)
            return
        if any(e.status == S.TEMP_OPEN for e in prod.excursions):
            raise RuleViolation("存在未闭环的温控异常，不能正式接收入库")
        prod.status = S.PROD_DELIVERED
        prod.delivered_utc = self.clock.now()
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="product.delivered",
            case_id=case.id, product_id=prod.id, ref_type="product", ref_id=prod.id,
            payload={"delivered_utc": iso(prod.delivered_utc),
                     "received_by": actor_id},
            event_id=record.id, note="受者医院核封、测温并正式接收入库",
        )
        self._move(case, S.DELIVERED, record)

    def _h_infusion(self, p, actor_id, record, case):
        require_fields(p, ["product_code", "physician_id"])
        prod = self._find_product(p["product_code"])
        if prod.status != S.PROD_DELIVERED:
            raise RuleViolation("只有已正式接收的产品可以回输")
        if any(e.status == S.TEMP_OPEN for e in prod.excursions):
            raise RuleViolation("温控异常未闭环，禁止回输")
        waived = [e for e in prod.excursions
                  if e.disposition == S.DISP_RELEASE_WAIVER]
        physician = self.directory.people.get(p["physician_id"])
        if not physician:
            raise ValidationError("回输医生信息缺失")
        prod.status = S.PROD_INFUSED
        prod.infused_utc = parse_instant(p["infused_at"]) if p.get("infused_at") else self.clock.now()
        prod.infusing_physician_id = physician.id
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="infusion.completed",
            case_id=case.id, product_id=prod.id, ref_type="product", ref_id=prod.id,
            payload={"physician_id": physician.id,
                     "infused_utc": iso(prod.infused_utc),
                     "waivers": [e.waiver_ref for e in waived]},
            event_id=record.id,
            note="回输完成；特许放行单随批可查" if waived else "回输完成",
        )
        self._move(case, S.INFUSED, record)
        self._move(case, S.CLOSED, record)

    def _h_case_cancel(self, p, actor_id, record, case):
        require_fields(p, ["reason"])
        slot = case.active_slot()
        if slot and slot.status != S.SLOT_CANCELLED:
            slot.status = S.SLOT_CANCELLED
            slot.reason = f"病例取消：{p['reason']}"
        case.cancel_reason = p["reason"]
        self.audit.append(
            ts=self.clock.now(), actor_id=actor_id, action="case.cancelled",
            case_id=case.id, ref_type="case", ref_id=case.id,
            payload={"reason": p["reason"]}, event_id=record.id,
        )
        self._move(case, S.CANCELLED, record)

    # ============ 送达回执 ============
    def _handle_receipt(self, p, external_id, source, actor_id, idem):
        require_fields(p, ["notification_id", "channel"])
        n = self.store.notifications.get(p["notification_id"])
        if not n:
            from .errors import NotFound
            raise NotFound(f"通知不存在: {p['notification_id']}")
        key = idem or f"receipt:{n.id}:{p['channel']}"
        if self.store.has_event(key):
            existing_id = self.store.has_event(key)
            rec = EventRecord(
                id=self.store.next_id("ev"), external_id=external_id,
                type=S.EV_DELIVERY_RECEIPT, case_id=n.case_id, payload=dict(p),
                idempotency_key=key, received_utc=self.clock.now(),
                actor_id=actor_id, source=source,
            )
            dup = self.store.mark_duplicate(key, rec)
            return {"applied": False, "event": dup.to_dict(),
                    "duplicate_of": existing_id, "notifications": [],
                    "message": "重复送达回执，忽略"}
        rec = EventRecord(
            id=self.store.next_id("ev"), external_id=external_id,
            type=S.EV_DELIVERY_RECEIPT, case_id=n.case_id, payload=dict(p),
            idempotency_key=key, received_utc=self.clock.now(),
            actor_id=actor_id, source=source,
        )
        self.store.record_event(rec)
        self.notifier.record_receipt(notification=n, receipt_event=rec, channel=p["channel"])
        return {"applied": True, "event": rec.to_dict(), "case_id": n.case_id,
                "notifications": [n.to_dict()],
                "message": f"通知 {n.id} 送达已确认"}

    # ============ 展示上下文 ============
    def _context(self, event_type, p, case, product):
        from .clock import local_view
        ctx = {"code": case.code}
        slot = case.active_slot()
        if slot:
            view = local_view(slot.planned_start_utc, slot.tz)
            end = local_view(slot.planned_end_utc, slot.tz)
            ctx["window"] = f"{view['date']} {view['time']}-{end['time']}（{slot.tz}）"
        if product:
            ctx["product_code"] = product.code
            ctx["temp"] = f"{p.get('temp_c', '?')}℃（允许 {product.temp_range[0]}~{product.temp_range[1]}℃）"
        if p.get("version") is not None:
            ctx["version"] = p["version"]
        elif case.effective_consent_version is not None:
            ctx["version"] = case.effective_consent_version
        if p.get("reason"):
            ctx["reason_text"] = p["reason"]
        if p.get("disposition"):
            ctx["disp"] = p["disposition"]
        alt = self.store.cases.get(case.replaced_by_case_id) if case.replaced_by_case_id else None
        if alt:
            ctx["alt_code"] = alt.code
        if event_type == S.EV_ALT_DONOR:
            ctx["alt_code"] = case.code
        return ctx

    def _summarize(self, event_type, p, case, product):
        notes = {
            S.EV_MATCH_SUCCESS: f"已建案 {case.code}，阶段={case.phase}",
            S.EV_SCREENING_RESULT: f"筛查 pass={case.screening_pass}，阶段={case.phase}",
            S.EV_CONSENT_GRANTED: f"同意 v{case.effective_consent_version} 生效，阶段={case.phase}",
            S.EV_CONSENT_WITHDRAWN: "同意已以撤回版本作废，阶段=donor_withdrawn",
            S.EV_SLOT_PROPOSED: "新窗口待确认",
            S.EV_SLOT_CONFIRMED: f"窗口已确认，阶段={case.phase}",
            S.EV_SLOT_RESCHEDULE: "原窗口取消，等待新窗口确认",
            S.EV_SLOT_CANCEL: "窗口已取消",
            S.EV_ALT_DONOR: f"替代供者病例 {case.code} 已建",
            S.EV_COLLECTION_STARTED: "采集已开始",
            S.EV_COLLECTION_COMPLETED: f"产品 {p.get('product_code')} 采集完成，阶段={case.phase}",
            S.EV_HANDOVER: f"产品 {p.get('product_code')} 完成一次交接",
            S.EV_TEMP_ALERT: "温控异常已登记，待处置",
            S.EV_TEMP_RESOLVED: f"温控异常处置={p.get('disposition')}，阶段={case.phase}",
            S.EV_DELIVERY: f"产品接收结果，阶段={case.phase}",
            S.EV_INFUSION: f"回输完成，阶段={case.phase}",
            S.EV_CASE_CANCEL: "病例已取消",
        }
        return notes.get(event_type, f"阶段={case.phase}")
