"""JSON HTTP 接口（标准库实现，无第三方依赖）。

鉴权：请求头 X-Staff-Token。所有业务接口均需登录；数据可见性由 projection 按角色裁剪。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import states as S
from .errors import CoordError, NotFound, PermissionError, ValidationError
from .identity import (
    CARRIER_HANDLER, COLLECTION_STAFF, COORDINATOR, DONOR_AFFAIRS,
    RECEIVING_STAFF, REGULATOR, TRANSPLANT_PHYSICIAN,
)
from .projection import (
    can_access_case, project_case, project_product, rules_for, visible_notifications,
)
from .timeline import build_case_timeline, build_product_timeline

EVENT_TYPES = {
    S.EV_MATCH_SUCCESS, S.EV_SCREENING_RESULT, S.EV_CONSENT_GRANTED,
    S.EV_CONSENT_WITHDRAWN, S.EV_SLOT_PROPOSED, S.EV_SLOT_CONFIRMED,
    S.EV_SLOT_RESCHEDULE, S.EV_SLOT_CANCEL, S.EV_ALT_DONOR,
    S.EV_COLLECTION_STARTED, S.EV_COLLECTION_COMPLETED, S.EV_HANDOVER,
    S.EV_TEMP_ALERT, S.EV_TEMP_RESOLVED, S.EV_DELIVERY, S.EV_INFUSION,
    S.EV_CASE_CANCEL, S.EV_DELIVERY_RECEIPT,
}


def make_handler(app):
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "CoordAPI/1.0"

        # ---- 基础收发 ----
        def _send(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, exc: CoordError):
            self._send(exc.status, {"error": exc.to_dict()})

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _viewer(self):
            return app.directory.authenticate(self.headers.get("X-Staff-Token", ""))

        def _require_role(self, viewer, roles):
            if not any(viewer.has(r) for r in roles):
                raise PermissionError(
                    f"需要角色之一: {', '.join(roles)}",
                    details={"have": list(viewer.roles)},
                )

        def log_message(self, *_args):
            return

        # ---- 路由 ----
        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                q = parse_qs(parsed.query)
                if path == "/health":
                    from service import health_payload
                    return self._send(200, health_payload())
                known = (
                    path == "/api/cases" or path == "/api/notifications"
                    or path == "/api/events" or path == "/api/audit/verify"
                    or path == "/api/directory"
                    or path.startswith("/api/cases/")
                    or path.startswith("/api/products/")
                )
                if not known:
                    self._send(404, {"error": {"code": "not_found", "message": f"无此路由: {path}"}})
                    return
                viewer = self._viewer()
                if path == "/api/cases":
                    return self._list_cases(viewer)
                if path.startswith("/api/cases/") and path.endswith("/timeline"):
                    case_id = path.split("/")[3]
                    return self._case_timeline(viewer, case_id)
                if path.startswith("/api/cases/"):
                    return self._get_case(viewer, path.split("/")[3])
                if path.startswith("/api/products/") and path.endswith("/timeline"):
                    code = path.split("/")[3]
                    return self._product_timeline(viewer, code)
                if path.startswith("/api/products/"):
                    return self._get_product(viewer, path.split("/")[3])
                if path == "/api/notifications":
                    return self._list_notifications(viewer, q)
                if path == "/api/events":
                    return self._list_events(viewer)
                if path == "/api/audit/verify":
                    return self._audit_verify(viewer)
                if path == "/api/directory":
                    return self._directory(viewer)
            except CoordError as exc:
                self._error(exc)

        def do_POST(self):
            try:
                path = urlparse(self.path).path.rstrip("/")
                known = (
                    path == "/api/searches" or path == "/api/events"
                    or (path.startswith("/api/notifications/") and path.endswith("/resend"))
                )
                if not known:
                    self._send(404, {"error": {"code": "not_found", "message": f"无此路由: {path}"}})
                    return
                viewer = self._viewer()
                if path == "/api/searches":
                    return self._open_search(viewer)
                if path == "/api/events":
                    return self._ingest(viewer)
                if path.startswith("/api/notifications/") and path.endswith("/resend"):
                    nid = path.split("/")[3]
                    return self._resend(viewer, nid)
            except CoordError as exc:
                self._error(exc)

        # ---- 业务端点 ----
        def _open_search(self, viewer):
            self._require_role(viewer, [TRANSPLANT_PHYSICIAN, COORDINATOR, REGULATOR])
            body = self._read_json()
            for f in ("transplant_org_id", "hla_summary"):
                if not body.get(f):
                    raise ValidationError(f"缺少必填字段: {f}")
            org_id = body["transplant_org_id"]
            if viewer.has(TRANSPLANT_PHYSICIAN) and viewer.org_id != org_id:
                raise PermissionError("只能为本机构发起检索")
            search = app.workflow.open_search(
                actor_id=viewer.id, transplant_org_id=org_id,
                hla_summary=body["hla_summary"],
                urgency=body.get("urgency", "routine"),
            )
            self._send(201, {"search": search.to_dict()})

        def _ingest(self, viewer):
            body = self._read_json()
            etype = body.get("type")
            if etype not in EVENT_TYPES:
                raise ValidationError(f"未知事件类型: {etype}",
                                      details={"allowed": sorted(EVENT_TYPES)})
            payload = body.get("payload") or {}
            external_id = body.get("external_id")
            if not external_id:
                raise ValidationError("缺少 external_id（外部事件唯一编号）")
            source = body.get("source") or "api"
            # 角色闸门：不同事件限定可上报角色
            self._event_role_gate(viewer, etype)
            result = app.workflow.ingest(
                etype, payload, external_id=external_id, source=source,
                actor_id=viewer.id, idem=body.get("idem"),
                case_id=body.get("case_id"),
            )
            # 重复事件幂等返回 200：applied=False 时不推进任何状态
            self._send(200, {"result": result})

        def _event_role_gate(self, viewer, etype):
            staff_any = [COORDINATOR, REGULATOR]
            gates = {
                S.EV_MATCH_SUCCESS: staff_any,
                S.EV_SCREENING_RESULT: staff_any + [COLLECTION_STAFF],
                S.EV_CONSENT_GRANTED: staff_any + [DONOR_AFFAIRS],
                S.EV_CONSENT_WITHDRAWN: staff_any + [DONOR_AFFAIRS],
                S.EV_SLOT_PROPOSED: staff_any + [COLLECTION_STAFF],
                S.EV_SLOT_CONFIRMED: staff_any + [COLLECTION_STAFF],
                S.EV_SLOT_RESCHEDULE: staff_any + [COLLECTION_STAFF, TRANSPLANT_PHYSICIAN],
                S.EV_SLOT_CANCEL: staff_any + [COLLECTION_STAFF],
                S.EV_ALT_DONOR: staff_any,
                S.EV_COLLECTION_STARTED: staff_any + [COLLECTION_STAFF],
                S.EV_COLLECTION_COMPLETED: staff_any + [COLLECTION_STAFF],
                S.EV_HANDOVER: staff_any + [COLLECTION_STAFF, CARRIER_HANDLER, RECEIVING_STAFF],
                S.EV_TEMP_ALERT: staff_any + [CARRIER_HANDLER, RECEIVING_STAFF],
                S.EV_TEMP_RESOLVED: staff_any + [TRANSPLANT_PHYSICIAN, COORDINATOR],
                S.EV_DELIVERY: staff_any + [RECEIVING_STAFF],
                S.EV_INFUSION: staff_any + [TRANSPLANT_PHYSICIAN],
                S.EV_CASE_CANCEL: staff_any,
                S.EV_DELIVERY_RECEIPT: staff_any + [CARRIER_HANDLER, RECEIVING_STAFF],
            }
            self._require_role(viewer, gates.get(etype, staff_any))

        def _list_cases(self, viewer):
            cases = []
            for case in app.store.cases.values():
                if can_access_case(case, viewer):
                    cases.append(project_case(case, viewer, app.directory, app.store,
                                              include_products=False))
            self._send(200, {"cases": cases, "count": len(cases)})

        def _get_case(self, viewer, case_id):
            case = app.store.require_case(case_id)
            if not can_access_case(case, viewer):
                raise PermissionError("无权查看该病例")
            data = project_case(case, viewer, app.directory, app.store)
            data["notifications"] = [
                n.to_dict() for n in visible_notifications(case.id, viewer, app.store)
            ]
            self._send(200, {"case": data})

        def _get_product(self, viewer, code):
            product = next((p for p in app.store.products.values() if p.code == code), None)
            if not product:
                raise NotFound(f"采集物不存在: {code}")
            case = app.store.require_case(product.case_id)
            if not can_access_case(case, viewer):
                raise PermissionError("无权查看该采集物")
            self._send(200, {"product": project_product(product, viewer, app.directory)})

        def _product_timeline(self, viewer, code):
            tl = build_product_timeline(
                store=app.store, directory=app.directory, audit=app.audit,
                clock=app.clock, product_code=code, viewer=viewer,
            )
            self._send(200, {"timeline": tl})

        def _case_timeline(self, viewer, case_id):
            tl = build_case_timeline(
                store=app.store, directory=app.directory, audit=app.audit,
                clock=app.clock, case_id=case_id, viewer=viewer,
            )
            self._send(200, {"timeline": tl})

        def _list_notifications(self, viewer, q):
            case_id = (q.get("case_id") or [None])[0]
            rules = rules_for(viewer)
            out = []
            for n in app.store.notifications.values():
                if case_id and n.case_id != case_id:
                    continue
                case = app.store.cases.get(n.case_id)
                if case and not can_access_case(case, viewer):
                    continue
                if rules["all_notifications"] or n.recipient_person_id == viewer.id:
                    out.append(n.to_dict())
            out.sort(key=lambda x: (x["created_utc"], x["id"]))
            self._send(200, {"notifications": out, "count": len(out)})

        def _list_events(self, viewer):
            self._require_role(viewer, [COORDINATOR, REGULATOR])
            events = [e.to_dict() for e in app.store.events]
            self._send(200, {"events": events, "count": len(events)})

        def _audit_verify(self, viewer):
            self._require_role(viewer, [REGULATOR])
            self._send(200, {"verification": app.audit.verify()})

        def _resend(self, viewer, nid):
            n = app.store.notifications.get(nid)
            if not n:
                raise NotFound(f"通知不存在: {nid}")
            case = app.store.cases.get(n.case_id)
            if case and not can_access_case(case, viewer):
                raise PermissionError("无权操作该通知")
            if n.recipient_person_id != viewer.id and not rules_for(viewer)["all_notifications"]:
                raise PermissionError("只能重发发给自己的通知")
            updated = app.workflow.notifier.resend(nid)
            self._send(200, {"notification": updated.to_dict()})

        def _directory(self, viewer):
            """供前端展示的机构/人员名录（联系方式按最小化原则不回传）。"""
            self._require_role(viewer, [COORDINATOR, REGULATOR])
            self._send(200, {
                "orgs": [o.ref() for o in app.directory.orgs.values()],
                "people": [{k: v for k, v in p.to_dict().items()
                            if k in ("id", "name", "title", "roles", "org_id")}
                           for p in app.directory.people.values() if p.roles],
            })

    return ApiHandler
