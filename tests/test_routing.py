"""精准受众：改期、撤回、温控异常等只通知真正需要处理的人。"""

import unittest

from coord import states as S
from coord.testsupport import Driver, build_app


def roles(result, suppressed=None):
    items = result["notifications"]
    if suppressed is not None:
        items = [n for n in items if n["suppressed"] is suppressed]
    return {n["audience_role"] for n in items}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)

    def test_match_does_not_disturb_carrier(self):
        self.d.open_search()
        r = self.d.to_matched()
        sent = roles(r, suppressed=False)
        suppressed = roles(r, suppressed=True)
        self.assertIn("carrier_handler", suppressed)
        self.assertNotIn("carrier_handler", sent)

    def test_withdrawal_before_scheduling_suppresses_hospital_and_carrier(self):
        self.d.to_consented()  # 尚未排期
        r = self.d.go(S.EV_CONSENT_WITHDRAWN, {
            "document_ref": "W-1", "signed_at": "2026-09-22T09:00:00+08:00"},
            actor="u_daff")
        sent = roles(r, suppressed=False)
        suppressed = roles(r, suppressed=True)
        # 协调员、联络员、受者医生必须立即动作
        self.assertIn("coordinator", sent)
        self.assertIn("donor_affairs", sent)
        self.assertIn("transplant_physician", sent)
        # 采集医院与承运方尚未卷入：留抑制痕迹，不打扰
        self.assertIn("collection_staff", suppressed)
        self.assertIn("carrier_handler", suppressed)
        self.assertNotIn("collection_staff", sent)
        self.assertNotIn("carrier_handler", sent)

    def test_withdrawal_after_scheduling_alerts_engaged_parties(self):
        self.d.to_scheduled()
        r = self.d.go(S.EV_CONSENT_WITHDRAWN, {
            "document_ref": "W-1", "signed_at": "2026-09-24T09:00:00+08:00"},
            actor="u_daff")
        sent = roles(r, suppressed=False)
        # 已排期：采集医院要释放手术间、承运方要取消取件
        self.assertIn("collection_staff", sent)
        self.assertIn("carrier_handler", sent)

    def test_reschedule_by_recipient_re_asks_donor_side(self):
        self.d.to_scheduled()
        r = self.d.go(S.EV_SLOT_RESCHEDULE, {
            "reason": "受者床位紧张", "requested_by": "recipient",
            "new_start_local": "2026-09-27T09:00",
            "new_end_local": "2026-09-27T14:00"}, actor="u_doc")
        sent = roles(r, suppressed=False)
        self.assertIn("donor_affairs", sent)       # 非供者发起，需供者重新确认
        self.assertIn("carrier_handler", sent)
        self.assertIn("collection_staff", sent)
        self.assertIn("transplant_physician", sent)

    def test_reschedule_by_donor_defers_donor_side_notice(self):
        self.d.to_scheduled()
        r = self.d.go(S.EV_SLOT_RESCHEDULE, {
            "reason": "供者临时工作安排", "requested_by": "donor",
            "new_start_local": "2026-09-27T09:00",
            "new_end_local": "2026-09-27T14:00"}, actor="u_daff")
        suppressed = roles(r, suppressed=True)
        sent = roles(r, suppressed=False)
        self.assertIn("donor_affairs", suppressed)   # 发起方已知情
        self.assertNotIn("donor_affairs", sent)
        # 其它三方仍需动作
        self.assertIn("carrier_handler", sent)
        self.assertIn("collection_staff", sent)

    def test_temp_alert_skips_collection_hospital(self):
        self.d.to_in_transit()
        r = self.d.report_excursion()
        sent = roles(r, suppressed=False)
        suppressed = roles(r, suppressed=True)
        self.assertIn("carrier_handler", sent)
        self.assertIn("transplant_physician", sent)
        self.assertIn("receiving_staff", sent)
        self.assertIn("collection_staff", suppressed)
        self.assertNotIn("collection_staff", sent)

    def test_recollect_disposition_recalls_donor_and_collection(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        r = self.d.resolve_excursion(S.DISP_RECOLLECT)
        sent = roles(r, suppressed=False)
        # 报废：采集医院与供者侧重新卷入，准备重采
        self.assertIn("collection_staff", sent)
        self.assertIn("donor_affairs", sent)
        self.assertIn("transplant_physician", sent)

    def test_infusion_releases_carrier_and_collection_silently(self):
        r = self.d.to_infused()
        suppressed = roles(r, suppressed=True)
        sent = roles(r, suppressed=False)
        self.assertIn("carrier_handler", suppressed)
        self.assertIn("collection_staff", suppressed)
        self.assertIn("donor_affairs", sent)  # 随访仍需供者联络员

    def test_every_notification_carries_actionable_reason(self):
        self.d.open_search()
        r = self.d.to_matched()
        for n in r["notifications"]:
            if not n["suppressed"]:
                self.assertTrue(n["body"].strip())
                self.assertTrue(n["context"]["reason"].strip())


if __name__ == "__main__":
    unittest.main()
