"""HTTP 接口契约：鉴权、事件投递、精准通知、时间线、重发、脱敏。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from coord.api import make_handler
from coord.testsupport import Driver, build_app


class ApiServer:
    def __init__(self, app):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, token=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Staff-Token", token)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)


class ApiTests(unittest.TestCase):
    def setUp(self):
        # 每个用例独立的应用与端口，避免病例累积干扰计数断言
        self.app = build_app()
        self.http = ApiServer(self.app)
        self.d = Driver(self.app)

    def tearDown(self):
        self.http.stop()

    def test_health_anonymous(self):
        status, body = self.http.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "stem-cell-donation")

    def test_unknown_route_404_without_auth(self):
        status, _ = self.http.call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_business_route_requires_token(self):
        status, body = self.http.call("GET", "/api/cases")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")

    def test_role_gate_on_event_ingest(self):
        self.d.to_scheduled()
        # 押运员无权上报改期
        status, body = self.http.call("POST", "/api/events", token="tk_car", body={
            "type": "slot.reschedule_requested",
            "external_id": "HTTP-1",
            "payload": {"reason": "无权操作"}})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_ingest_duplicate_event_is_idempotent_over_http(self):
        self.d.open_search()
        payload = {"search_id": self.d.search_id, "donor_person_id": "donor-77",
                   "donor_org_id": "REG-XJ", "collection_org_id": "HOSP-BJ",
                   "carrier_org_id": "COLD-CHAIN"}
        s1, b1 = self.http.call("POST", "/api/events", token="tk_coord", body={
            "type": "match.success", "external_id": "HTTP-M1",
            "source": "sms", "payload": payload})
        s2, b2 = self.http.call("POST", "/api/events", token="tk_coord", body={
            "type": "match.success", "external_id": "HTTP-M2",
            "source": "callback", "payload": payload})
        self.assertEqual(s1, 200)
        self.assertTrue(b1["result"]["applied"])
        self.assertEqual(s2, 200)
        self.assertFalse(b2["result"]["applied"])
        self.assertEqual(b2["result"]["duplicate_of"], b1["result"]["event"]["id"])

    def test_org_scoped_case_list_and_masking(self):
        self.d.to_scheduled()
        # 承运方只能看到与自己机构相关的病例
        status, body = self.http.call("GET", "/api/cases", token="tk_car")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["cases"][0]["donor"]["masked"])
        # 受者医生视角：可见 HLA 但不见供者
        _, bdoc = self.http.call("GET", "/api/cases", token="tk_doc")
        self.assertEqual(bdoc["cases"][0]["hla_summary"], "HLA 10/10")
        self.assertTrue(bdoc["cases"][0]["donor"]["masked"])

    def test_notification_list_and_resend_flow(self):
        self.d.to_matched()
        _, body = self.http.call("GET", "/api/notifications", token="tk_coord")
        note = next(n for n in body["notifications"] if not n["suppressed"])
        sends_before = len(note["sends"])
        status, updated = self.http.call(
            "POST", f"/api/notifications/{note['id']}/resend", token="tk_coord")
        self.assertEqual(status, 200)
        self.assertEqual(len(updated["notification"]["sends"]), sends_before + 1)

    def test_cannot_resend_others_notification(self):
        self.d.to_matched()
        _, body = self.http.call("GET", "/api/notifications?case_id=" + self.d.case_id,
                                 token="tk_coord")
        note = next(n for n in body["notifications"]
                    if n["recipient_person_id"] != "u_coord" and not n["suppressed"])
        status, body = self.http.call(
            "POST", f"/api/notifications/{note['id']}/resend", token="tk_car")
        # 押运员重发别人的通知 → 403
        self.assertEqual(status, 403)

    def test_product_timeline_requires_related_party(self):
        self.d.to_delivered()
        code = self.d.product_code
        # 无关机构人员不可见
        from coord.identity import Channel, Org, Person
        from coord.identity import CARRIER
        d = self.app.directory
        d.add_org(Org("COLD-OTHER", "别家冷链", CARRIER, "Asia/Shanghai"))
        d.add_person(Person("u_car_other", "钱押运", "押运员", ("carrier_handler",),
                            "COLD-OTHER", [Channel("sms", "13700009999")], "tk_other"))
        status, _ = self.http.call("GET", f"/api/products/{code}/timeline", token="tk_other")
        self.assertEqual(status, 403)
        # 监管可查
        status, body = self.http.call("GET", f"/api/products/{code}/timeline", token="tk_reg")
        self.assertEqual(status, 200)
        self.assertTrue(body["timeline"]["integrity"]["ok"])
        self.assertEqual(body["timeline"]["adopted_consent_version"], 1)


if __name__ == "__main__":
    unittest.main()
