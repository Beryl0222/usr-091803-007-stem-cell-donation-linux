"""监管时间线：同意版本、交接人、异常处置可复核；哈希链防篡改。"""

import unittest

from coord import states as S
from coord.errors import PermissionError
from coord.projection import can_access_case
from coord.testsupport import Driver, build_app
from coord.timeline import build_product_timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)
        self.d.to_in_transit()
        self.d.report_excursion(temp=12.0, minutes=45)
        self.d.resolve_excursion(S.DISP_RELEASE_WAIVER, waiver_ref="WV-7",
                                 note="医学评估细胞活性可接受")
        self.d.go(S.EV_HANDOVER, {
            "product_code": self.d.product_code, "from_person_id": "u_car",
            "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
            "temp_c": 6.5, "at_utc": "2026-09-25T08:00:00Z"}, actor="u_recv")
        self.d.go(S.EV_DELIVERY, {"product_code": self.d.product_code}, actor="u_recv")
        self.d.go(S.EV_INFUSION, {
            "product_code": self.d.product_code, "physician_id": "u_doc",
            "infused_at": "2026-09-25T10:00:00Z"}, actor="u_doc")

    def _timeline(self, viewer_id="u_reg"):
        viewer = self.app.directory.people[viewer_id]
        return build_product_timeline(
            store=self.app.store, directory=self.app.directory,
            audit=self.app.audit, clock=self.app.clock,
            product_code=self.d.product_code, viewer=viewer)

    def test_timeline_has_consent_handovers_excursion_outcome(self):
        tl = self._timeline()
        self.assertEqual(tl["adopted_consent_version"], 1)
        self.assertEqual(tl["adopted_consent"]["document_ref"], "CONS-T-001")
        # 两次交接，交接人姓名落档
        self.assertEqual(len(tl["handovers"]), 2)
        first = tl["handovers"][0]
        self.assertEqual(first["from_person"]["person_id"], "u_coll")
        self.assertEqual(first["to_person"]["person_id"], "u_car")
        self.assertTrue(first["temp_ok"])
        # 异常处置闭环
        self.assertEqual(tl["excursions"][0]["status"], S.TEMP_RESOLVED)
        self.assertEqual(tl["excursions"][0]["disposition"], S.DISP_RELEASE_WAIVER)
        self.assertEqual(tl["excursions"][0]["waiver_ref"], "WV-7")
        self.assertEqual(tl["excursions"][0]["decider"]["person_id"], "u_doc")
        self.assertIn("特许放行", tl["excursions"][0]["outcome"])

    def test_every_node_carries_hash_and_local_times(self):
        tl = self._timeline()
        for node in tl["nodes"]:
            self.assertEqual(len(node["hash_chain"]["hash"]), 64)
            self.assertEqual(len(node["hash_chain"]["prev_hash"]), 64)
            self.assertIn("HOSP-BJ", node["local_times"])
            self.assertIn("HOSP-SH", node["local_times"])
        self.assertTrue(tl["integrity"]["ok"])

    def test_local_time_interpretation_per_center(self):
        tl = self._timeline()
        pickup = tl["handovers"][0]
        # 2026-09-25T03:00Z → 北京 11:00
        self.assertEqual(pickup["local_times"]["HOSP-BJ"]["time"], "11:00")

    def test_tampering_breaks_chain(self):
        self._timeline()
        # 模拟内部人员篡改历史审计内容（存储的 hash 不会随之改变，
        # 因此重算内容哈希即可发现 seq=6 被改）
        entry = self.app.audit.entries[5]
        entry.payload = {"tampered": True}
        verdict = self.app.audit.verify()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["broken_at_seq"], 6)
        # 时间线的完整性结论同步失败
        tl = self._timeline()
        self.assertFalse(tl["integrity"]["ok"])

    def test_product_timeline_pins_consent_even_after_later_events(self):
        tl = self._timeline()
        self.assertEqual(tl["product"]["status"], S.PROD_INFUSED)
        self.assertIn("同意 v1", tl["consent_note"])

    def test_unrelated_role_cannot_view(self):
        # 仅有承运角色且不在本病例承运机构的人不可见（这里用目录隔离验证）
        from coord.identity import CARRIER_HANDLER, Channel, Org, Person
        d = self.app.directory
        d.add_org(Org("COLD-OTHER", "别家冷链", "carrier", "Asia/Shanghai"))
        d.add_person(Person("u_car_other", "钱押运", "押运员",
                            (CARRIER_HANDLER,), "COLD-OTHER",
                            [Channel("sms", "13700009999")], "tk_car_other"))
        intruder = d.people["u_car_other"]
        self.assertFalse(can_access_case(self.d.case, intruder))
        with self.assertRaises(PermissionError):
            build_product_timeline(
                store=self.app.store, directory=d, audit=self.app.audit,
                clock=self.app.clock, product_code=self.d.product_code,
                viewer=intruder)

    def test_timeline_view_is_audited(self):
        before = len(self.app.audit.entries)
        self._timeline(viewer_id="u_reg")
        after = self.app.audit.entries
        self.assertEqual(after[-1].action, "timeline.viewed")
        self.assertEqual(after[-1].actor_id, "u_reg")
        self.assertGreater(len(after), before)


if __name__ == "__main__":
    unittest.main()
