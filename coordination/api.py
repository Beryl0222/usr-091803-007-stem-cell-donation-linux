"""HTTP/JSON 适配层：把 REST 请求翻译为应用服务命令。

鉴权采用简单的调用方头（联调用；生产替换为正式身份令牌）：
  X-Actor-Id    参与方/人员标识
  X-Actor-Role  角色（见 models.Role）
  X-Actor-Tz    该中心所在地时区（默认 Asia/Shanghai）

外部系统回调（温控、短信送达）在请求体中携带 external_source /
external_event_id，由台账保证同一外部事件只推进一次。
"""

from .models import Actor, Role
from .service import CoordinationService, CommandError
from .audit import build_timeline
from .views import case_for_party, party_inbox
from .events import DuplicateExternalEvent


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _actor(headers) -> Actor:
    def h(name):
        return headers.get(name, headers.get(name.lower(), ""))
    role_raw = h("X-Actor-Role") or Role.COORDINATOR.value
    try:
        role = Role(role_raw)
    except ValueError:
        raise ApiError(401, "unknown_role", f"未知角色: {role_raw}")
    party_id = h("X-Actor-Id")
    if not party_id:
        raise ApiError(401, "anonymous", "缺少 X-Actor-Id")
    return Actor(role=role, party_id=party_id,
                 tz=h("X-Actor-Tz") or "Asia/Shanghai")


def _require_role(actor: Actor, allowed: set, verb: str):
    if actor.role not in allowed:
        raise ApiError(403, "forbidden",
                       f"角色 {actor.role.value} 无权执行 {verb}")


# 命令型操作的角色范围
STAFF = {Role.COORDINATOR, Role.DONOR_CENTER}


class Api:
    def __init__(self, svc: CoordinationService):
        self.svc = svc

    # -----------------------------------------------------------------
    def handle(self, method, path, query, body, headers):
        actor = _actor(headers)
        p = [x for x in path.strip("/").split("/") if x]

        # GET 集合
        if method == "GET" and p == ["api", "cases"]:
            return 200, {"cases": [
                {"case_id": s.case_id, "status": s.status.value,
                 "consent_state": s.consent_state.value,
                 "shipment_status": s.shipment_status.value}
                for s in self.svc.list_cases()]}

        if method == "GET" and len(p) == 3 and p[:2] == ["api", "cases"]:
            viewer = actor.role
            if "viewer_role" in query:
                try:
                    viewer = Role(query["viewer_role"][0])
                except ValueError:
                    raise ApiError(400, "bad_viewer", "viewer_role 非法")
            return 200, case_for_party(self.svc.ledger, p[2], viewer)

        if method == "GET" and len(p) == 4 and p[:2] == ["api", "cases"] \
                and p[3] == "timeline":
            tz = query.get("tz", [actor.tz])[0]
            viewer = actor.role
            if "viewer_role" in query:
                viewer = Role(query["viewer_role"][0])
            try:
                return 200, build_timeline(self.svc.ledger, p[2], tz, viewer)
            except KeyError as exc:
                raise ApiError(404, "not_found", str(exc))

        if method == "GET" and len(p) == 4 and p[:2] == ["api", "parties"] \
                and p[3] == "inbox":
            return 200, party_inbox(self.svc.ledger, p[2])

        if method != "POST":
            raise ApiError(404, "not_found", "未知路由")

        try:
            return self._post(actor, p, body or {})
        except DuplicateExternalEvent as dup:
            # 任意命令命中外部/业务幂等去重：返回首次事实，不二次推进。
            return 200, {"result": "duplicate_external",
                         "first_event_seq": dup.first.seq}
        except CommandError as exc:
            # 违反状态机/同意/前置条件：冲突，调用方可读原因并据此纠正。
            raise ApiError(409, "illegal_command", str(exc))

    # -----------------------------------------------------------------
    def _post(self, actor: Actor, p, b):
        svc = self.svc

        if p == ["api", "cases"]:
            _require_role(actor, {Role.COORDINATOR}, "建档")
            try:
                cid = svc.open_case(
                    actor,
                    donor_identity=b["donor_identity"],
                    recipient_identity=b["recipient_identity"],
                    parties=b["parties"],
                    product_type=b.get("product_type", "pbsc"),
                    timezones=b.get("timezones"),
                    case_id=b.get("case_id", ""),
                    idem_key=b.get("idempotency_key", ""))
            except KeyError as exc:
                raise ApiError(400, "missing_field", f"缺少字段 {exc}")
            return 201, {"case_id": cid}

        if len(p) >= 3 and p[0] == "api" and p[1] == "cases":
            cid = p[2]
            tail = p[3:]
            return self._case_command(actor, cid, tail, b)

        if len(p) == 4 and p[:2] == ["api", "notifications"] and p[3] == "resend":
            _require_role(actor, {Role.COORDINATOR}, "重发通知")
            ev, ok = svc.resend_notification(actor, p[2])
            return 200, {"event_seq": ev.seq, "sent": ok}

        if p == ["api", "delivery-receipts"]:
            try:
                result = svc.record_delivery_receipt(
                    actor,
                    external_message_id=b["external_message_id"],
                    external_source=b["external_source"],
                    external_event_id=b["external_event_id"],
                    delivered_local_iso=b.get("delivered_local_iso"))
            except KeyError as exc:
                raise ApiError(400, "missing_field", f"缺少字段 {exc}")
            return 200, {"result": result[0], "event_seq": result[1].seq}

        raise ApiError(404, "not_found", "未知路由")

    # -----------------------------------------------------------------
    def _case_command(self, actor, cid, tail, b):
        svc = self.svc
        idem = b.get("idempotency_key", "")
        try:
            if tail == ["screening", "start"]:
                _require_role(actor, STAFF, "启动筛查")
                ev = svc.start_screening(actor, cid,
                                         arrangements=b.get("arrangements", ""),
                                         idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["screening", "pass"]:
                _require_role(actor, STAFF, "筛查通过")
                ev = svc.pass_screening(actor, cid, idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["consent", "grant"]:
                _require_role(actor, {Role.COORDINATOR, Role.DONOR_CENTER,
                                      Role.DONOR}, "签署同意")
                ev, version = svc.grant_consent(
                    actor, cid,
                    document_version=b["document_version"],
                    document_hash=b["document_hash"],
                    signed_local_iso=b["signed_local_iso"],
                    witness_party=b.get("witness_party", ""),
                    statement=b.get("statement", ""), idem_key=idem)
                return 200, {"event_seq": ev.seq, "consent_version": version}

            if tail == ["consent", "withdraw"]:
                _require_role(actor, {Role.COORDINATOR, Role.DONOR_CENTER,
                                      Role.DONOR}, "撤回同意")
                ev, version = svc.withdraw_consent(
                    actor, cid,
                    document_version=b["document_version"],
                    document_hash=b["document_hash"],
                    reason=b.get("reason", ""),
                    signed_local_iso=b.get("signed_local_iso"),
                    idem_key=idem)
                return 200, {"event_seq": ev.seq, "consent_version": version,
                             "consent_state": "withdrawn"}

            if tail == ["schedule"]:
                _require_role(actor, STAFF, "排期/改期")
                ev, version = svc.schedule_collection(
                    actor, cid,
                    start_local_iso=b["start_local_iso"],
                    end_local_iso=b["end_local_iso"],
                    tz=b.get("tz"), reason=b.get("reason", ""),
                    idem_key=idem)
                return 200, {"event_seq": ev.seq, "schedule_version": version}

            if tail == ["schedule", "confirm"]:
                result = svc.confirm_schedule(
                    actor, cid, party_id=b["party_id"],
                    external_source=b.get("external_source", ""),
                    external_event_id=b.get("external_event_id", ""),
                    idem_key=idem)
                return 200, {"result": result[0]}

            if tail == ["collection"]:
                _require_role(actor, {Role.COORDINATOR, Role.DONOR_CENTER},
                              "采集完成")
                ev = svc.complete_collection(
                    actor, cid, product_id=b["product_id"],
                    volume_ml=b.get("volume_ml"), idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["handovers"]:
                _require_role(actor, {Role.COORDINATOR, Role.DONOR_CENTER,
                                      Role.COURIER, Role.RECIPIENT_HOSPITAL},
                              "样本交接")
                ev = svc.handover(
                    actor, cid, kind=b["kind"],
                    from_person=b["from_person"], to_person=b["to_person"],
                    product_temp_c=b["product_temp_c"],
                    container_id=b["container_id"],
                    evidence_ref=b["evidence_ref"], idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["excursion"]:
                _require_role(actor, {Role.COORDINATOR, Role.COURIER},
                              "温控异常上报")
                result = svc.report_excursion(
                    actor, cid, temp_c=b["temp_c"],
                    limit_low_c=b["limit_low_c"], limit_high_c=b["limit_high_c"],
                    reading_local_iso=b["reading_local_iso"],
                    external_source=b["external_source"],
                    external_event_id=b["external_event_id"])
                return 200, {"result": result[0], "event_seq": result[1].seq}

            if tail == ["excursion", "resolve"]:
                _require_role(actor, {Role.COORDINATOR, Role.DONOR_CENTER,
                                      Role.RECIPIENT_HOSPITAL}, "异常处置")
                ev = svc.resolve_excursion(
                    actor, cid, result=b["result"],
                    note=b.get("note", ""), idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["deliver"]:
                _require_role(actor, {Role.COORDINATOR, Role.COURIER}, "送达")
                ev = svc.deliver(actor, cid, idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["accept"]:
                _require_role(actor, {Role.COORDINATOR,
                                      Role.RECIPIENT_HOSPITAL}, "签收")
                ev = svc.accept_product(
                    actor, cid, by_person=b["by_person"],
                    note=b.get("note", ""), idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["reject"]:
                _require_role(actor, {Role.COORDINATOR,
                                      Role.RECIPIENT_HOSPITAL}, "拒收")
                ev = svc.reject_product(
                    actor, cid, by_person=b["by_person"],
                    reason=b["reason"], idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["infusion"]:
                _require_role(actor, {Role.COORDINATOR,
                                      Role.RECIPIENT_HOSPITAL}, "回输")
                ev = svc.complete_infusion(
                    actor, cid, operator=b["operator"], idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["cancel"]:
                _require_role(actor, {Role.COORDINATOR}, "取消")
                ev = svc.cancel_case(
                    actor, cid, reason=b["reason"],
                    cause_role=b.get("cause_role", ""),
                    stage=b.get("stage", ""), idem_key=idem)
                return 200, {"event_seq": ev.seq}

            if tail == ["swap-donor"]:
                _require_role(actor, {Role.COORDINATOR}, "替代供者")
                ev = svc.swap_donor(
                    actor, cid, reason=b["reason"],
                    replacement_case_id=b["replacement_case_id"],
                    idem_key=idem)
                return 200, {"event_seq": ev.seq}

        except KeyError as exc:
            raise ApiError(400, "missing_field", f"缺少字段 {exc}")
        except DuplicateExternalEvent as dup:
            return 200, {"result": "duplicate_external",
                         "first_event_seq": dup.first.seq}

        raise ApiError(404, "not_found", f"未知病例命令: /{'/'.join(tail)}")
