"""HTTP 端到端契约：鉴权、角色裁剪、命令推进、外部回调幂等、时间线。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, build_application
from coordination.events import Ledger
from coordination.service import CoordinationService, StubSmsChannel


def _req(method, url, token, body=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return Request(url, data=data, headers=headers, method=method)


def call(method, url, token, body=None):
    try:
        with urlopen(_req(method, url, token, body), timeout=3) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


COORD = {"X-Actor-Id": "party-coord", "X-Actor-Role": "coordinator"}
DC = {"X-Actor-Id": "party-dc", "X-Actor-Role": "donor_center"}
COURIER = {"X-Actor-Id": "party-courier", "X-Actor-Role": "courier"}
RH = {"X-Actor-Id": "party-rh", "X-Actor-Role": "recipient_hospital"}
DONOR = {"X-Actor-Id": "party-donor", "X-Actor-Role": "donor"}

PARTIES = {"coordinator": "party-coord", "donor_center": "party-dc",
           "courier": "party-courier", "recipient_hospital": "party-rh",
           "donor": "party-donor"}


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注入一个进程内应用（空台账）
        from coordination.api import Api
        ledger = Ledger("")
        cls.ledger = ledger
        cls.channel = StubSmsChannel()
        cls.app = Api(CoordinationService(ledger, cls.channel))
        Handler.api = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        Handler.api = None

    def test_01_requires_actor(self):
        status, body = call("POST", f"{self.base}/api/cases", {}, {})
        self.assertEqual(status, 401)

    def test_02_role_forbidden_for_courier_open_case(self):
        body = {"donor_identity": {"full_name": "张"},
                "recipient_identity": {"full_name": "李"},
                "parties": PARTIES}
        status, _ = call("POST", f"{self.base}/api/cases", COURIER, body)
        self.assertEqual(status, 403)

    def test_03_full_journey_over_http(self):
        # 建档（跨时区：供者侧乌鲁木齐，受者侧上海）
        status, body = call("POST", f"{self.base}/api/cases", COORD, {
            "donor_identity": {"full_name": "张伟", "contact_phone": "13812345678",
                               "national_id_masked": "110101********0011"},
            "recipient_identity": {"full_name": "李明",
                                   "medical_record_no": "MR-9"},
            "parties": PARTIES,
            "timezones": {"donor": "Asia/Urumqi",
                          "donor_center": "Asia/Urumqi",
                          "courier": "Asia/Shanghai",
                          "recipient_hospital": "Asia/Shanghai"}})
        self.assertEqual(status, 201)
        cid = body["case_id"]

        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/screening/start",
                              DC, {"arrangements": "体检"})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/screening/pass",
                              DC, {})[0], 200)
        status, body = call("POST", f"{self.base}/api/cases/{cid}/consent/grant",
                            COORD, {"document_version": "C-v1",
                                    "document_hash": "a" * 64,
                                    "signed_local_iso": "2026-09-10T10:00"})
        self.assertEqual(status, 200)
        self.assertEqual(body["consent_version"], 1)

        # 排期
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/schedule",
                              COORD, {"start_local_iso": "2026-09-25T08:00",
                                      "end_local_iso": "2026-09-25T14:00",
                                      "tz": "Asia/Urumqi"})[0], 200)
        # 四方确认；受者医院经 HIS 回调，带外部事件 ID
        for tok, pid in ((DONOR, "party-donor"), (DC, "party-dc"),
                         (COURIER, "party-courier")):
            self.assertEqual(call(
                "POST", f"{self.base}/api/cases/{cid}/schedule/confirm",
                tok, {"party_id": pid})[1]["result"], "confirmed")
        status, body = call(
            "POST", f"{self.base}/api/cases/{cid}/schedule/confirm", RH,
            {"party_id": "party-rh", "external_source": "his-rh",
             "external_event_id": "CB-9"})
        self.assertEqual(body["result"], "confirmed")
        # HIS 回调重试：不得二次推进
        status, body = call(
            "POST", f"{self.base}/api/cases/{cid}/schedule/confirm", RH,
            {"party_id": "party-rh", "external_source": "his-rh",
             "external_event_id": "CB-9"})
        self.assertIn(body["result"], ("duplicate_external", "already_confirmed"))

        # 采集 -> 交接 -> 温控异常 -> 放行 -> 送达 -> 签收 -> 回输
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/collection",
                              DC, {"product_id": "PROD-H1",
                                   "volume_ml": 200})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/handovers",
                              DC, {"kind": "collection_to_courier",
                                   "from_person": "王医生", "to_person": "赵押运",
                                   "product_temp_c": 4.0, "container_id": "B1",
                                   "evidence_ref": "H1"})[0], 200)
        excursion_body = {"temp_c": 12.0, "limit_low_c": 2.0,
                          "limit_high_c": 8.0,
                          "reading_local_iso": "2026-09-25T20:00",
                          "external_source": "iot", "external_event_id": "I-9"}
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/excursion",
                              COURIER, excursion_body)[1]["result"], "recorded")
        # IoT 重发同一读数
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/excursion",
                              COURIER, excursion_body)[1]["result"],
                         "duplicate_external")
        self.assertEqual(call("POST",
                              f"{self.base}/api/cases/{cid}/excursion/resolve",
                              COORD, {"result": "released",
                                      "note": "短暂偏离"})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/handovers",
                              COURIER, {"kind": "courier_to_recipient",
                                        "from_person": "赵押运",
                                        "to_person": "孙主任",
                                        "product_temp_c": 5.0,
                                        "container_id": "B1",
                                        "evidence_ref": "H2"})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/deliver",
                              COURIER, {})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/accept",
                              RH, {"by_person": "孙主任"})[0], 200)
        self.assertEqual(call("POST", f"{self.base}/api/cases/{cid}/infusion",
                              RH, {"operator": "孙主任"})[0], 200)

        status, case = call("GET", f"{self.base}/api/cases/{cid}", RH)
        self.assertEqual(case["status"], "infused")

    def test_04_privacy_over_the_wire(self):
        # 取上一用例病例；受者医院看不到供者姓名
        cid = self._find_case()
        status, rh_view = call("GET", f"{self.base}/api/cases/{cid}", RH)
        self.assertNotIn("full_name", rh_view["identities"]["donor"])
        self.assertIn("pseudonym", rh_view["identities"]["donor"])
        # 协调员可见双方
        status, coord_view = call("GET", f"{self.base}/api/cases/{cid}", COORD)
        self.assertEqual(coord_view["identities"]["donor"]["full_name"], "张伟")

    def test_05_timeline_endpoint_integrity_and_consent(self):
        cid = self._find_case()
        status, tl = call("GET",
                          f"{self.base}/api/cases/{cid}/timeline"
                          "?tz=Asia/Shanghai&viewer_role=regulator", COORD)
        self.assertEqual(status, 200)
        self.assertTrue(tl["integrity"]["ok"])
        collection = next(e for e in tl["entries"]
                          if e["node"] == "collection_completed")
        self.assertEqual(collection["detail"]["adopted_consent_version"], 1)
        # 温控异常的处置结果已闭合
        exc = next(e for e in tl["entries"]
                   if e["node"] == "shipment_excursion")
        self.assertEqual(exc["detail"]["resolution"]["result"], "released")

    def test_06_inbox_and_resend_and_delivery_receipt(self):
        cid = self._find_case()
        status, inbox = call("GET",
                             f"{self.base}/api/parties/party-courier/inbox",
                             COORD)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(inbox["total"], 1)
        # 找一条承运方通知重发
        nid = inbox["actions"][0]["notification_id"]
        status, body = call("POST",
                            f"{self.base}/api/notifications/{nid}/resend",
                            COORD, {})
        self.assertEqual(status, 200)
        self.assertTrue(body["sent"])

    def _find_case(self):
        status, listing = call("GET", f"{self.base}/api/cases", COORD)
        self.assertEqual(status, 200)
        self.assertTrue(listing["cases"])
        return listing["cases"][0]["case_id"]

    def test_07_illegal_command_returns_conflict_not_500(self):
        # 建档后未经筛查/同意直接排期：状态机拒绝 -> 409
        status, body = call("POST", f"{self.base}/api/cases", COORD, {
            "donor_identity": {"full_name": "王"},
            "recipient_identity": {"full_name": "赵"},
            "parties": PARTIES, "case_id": "CASE-409"})
        self.assertEqual(status, 201)
        status, body = call("POST",
                            f"{self.base}/api/cases/CASE-409/schedule", COORD,
                            {"start_local_iso": "2026-09-25T08:00",
                             "end_local_iso": "2026-09-25T14:00"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "illegal_command")

    def test_08_open_case_idempotent_on_retry(self):
        payload = {"donor_identity": {"full_name": "钱"},
                   "recipient_identity": {"full_name": "孙"},
                   "parties": PARTIES, "case_id": "CASE-IDEM",
                   "idempotency_key": "open-77"}
        s1, b1 = call("POST", f"{self.base}/api/cases", COORD, payload)
        s2, b2 = call("POST", f"{self.base}/api/cases", COORD, payload)
        self.assertEqual(s1, 201)
        # 重试不二次建档、不二次通知
        self.assertEqual(b2["result"], "duplicate_external")


if __name__ == "__main__":
    unittest.main()
