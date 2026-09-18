"""同一外部事件只推进一次状态：短信与中心回调重复到达安全。"""

import unittest

from coord import states as S
from coord.testsupport import Driver, build_app


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)
        self.d.to_scheduled()

    def test_duplicate_match_via_two_channels_applied_once(self):
        # to_scheduled 已建过案；对同一检索+供者再投递一次配型成功
        r = self.d.go(S.EV_MATCH_SUCCESS, {
            "search_id": self.d.search_id, "donor_person_id": "donor-77",
            "donor_org_id": "REG-XJ"}, ext="SMS-DUP", source="callback")
        self.assertFalse(r["applied"])
        self.assertTrue(r["duplicate_of"])
        cases = [c for c in self.app.store.cases.values()]
        self.assertEqual(len(cases), 1)
        # 重复事件留痕但不产生新通知
        self.assertEqual(r["notifications"], [])

    def test_duplicate_screening_does_not_double_advance(self):
        before = self.app.store.cases[self.d.case_id].phase
        r = self.d.go(S.EV_SCREENING_RESULT,
                      {"pass": True, "exam_ref": "EX-1"}, ext="CB-DUP")
        self.assertFalse(r["applied"])
        self.assertEqual(self.app.store.cases[self.d.case_id].phase, before)

    def test_distinct_exam_refs_are_distinct_events(self):
        # 不同 exam_ref 是不同事项（如补充复查），允许再次录入
        r = self.d.go(S.EV_SCREENING_RESULT,
                      {"pass": True, "summary": "复查合格", "exam_ref": "EX-2"},
                      ext="CB-EX2")
        self.assertTrue(r["applied"])

    def test_duplicate_temp_alert_from_iot_and_sms(self):
        self.d.to_in_transit()
        payload = {"product_code": self.d.product_code, "temp_c": 11.0,
                   "duration_minutes": 30, "detected_at_utc": "2026-09-25T04:00:00Z"}
        r1 = self.d.go(S.EV_TEMP_ALERT, payload, ext="IOT-1", source="iot")
        r2 = self.d.go(S.EV_TEMP_ALERT, payload, ext="SMS-1", source="sms")
        self.assertTrue(r1["applied"])
        self.assertFalse(r2["applied"])
        self.assertEqual(len(self.d.product.excursions), 1)

    def test_explicit_idem_dedupes_same_key(self):
        r1 = self.d.go(S.EV_SLOT_CANCEL, {"reason": "设备故障"},
                       ext="X1", idem="cancel-fixed-key")
        r2 = self.d.go(S.EV_SLOT_CANCEL, {"reason": "设备故障"},
                       ext="X2", idem="cancel-fixed-key")
        self.assertTrue(r1["applied"])
        self.assertFalse(r2["applied"])

    def test_duplicate_delivery_receipt_ignored(self):
        self.d.to_in_transit()
        # 触发一次交接通知后，对同一条通知回执两次
        note = next(n for n in self.app.store.notifications.values()
                    if n.template == "handover_pickup")
        p1 = {"notification_id": note.id, "channel": "sms"}
        r1 = self.d.go(S.EV_DELIVERY_RECEIPT, p1, ext="RCP-1")
        r2 = self.d.go(S.EV_DELIVERY_RECEIPT, p1, ext="RCP-2")
        self.assertTrue(r1["applied"])
        self.assertFalse(r2["applied"])
        # 只记录一条 delivered
        delivered = [s for s in note.sends if s["status"] == "delivered"]
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
