"""多场景集成：受者床位改期后取消、替代供者、并发多病例隔离。"""

import unittest

from coordination.models import CaseStatus
from coordination.notifications import build_notification_projection
from testsupport import (
    make_service, open_ready_case, schedule_and_confirm, actor, PARTIES,
)

P = PARTIES


def kinds_for(svc, party):
    proj = build_notification_projection(svc.ledger.all_events())
    return [n.kind for n in proj.for_party(party)]


class RescheduleThenCancelTest(unittest.TestCase):
    def test_cancel_excludes_cause_party_but_releases_capacity(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-SC1")
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00")
        for r in ("donor", "donor_center", "courier", "recipient_hospital"):
            svc.confirm_schedule(actor(r), cid, party_id=P[r])
        # 受者病情变化先改期，后仍无法承接 -> 取消（受者医院是取消原因方）
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-29T08:00",
                                end_local_iso="2026-09-29T14:00",
                                reason="受者感染，床位后移")
        svc.cancel_case(actor("coordinator"), cid,
                        reason="受者病情恶化，本周期不适合移植",
                        cause_role="recipient_hospital", stage="scheduled")
        kinds = kinds_for(svc, P["recipient_hospital"])
        # 取消原因方不再收到取消通知（其自身发起/已知情）
        self.assertNotIn("case_cancelled", kinds)
        # 承运方因已排期需要释放运力，被通知一次
        cancel_notes = [k for k in kinds_for(svc, P["courier"])
                        if k == "case_cancelled"]
        self.assertEqual(len(cancel_notes), 1)
        self.assertEqual(svc.get_case(cid).status, CaseStatus.CANCELLED)

    def test_cancel_before_schedule_does_not_touch_courier(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-SC2")
        svc.cancel_case(actor("coordinator"), cid,
                        reason="供者高分辨复查不合", cause_role="donor_center",
                        stage="consent_pending")
        self.assertNotIn("case_cancelled", kinds_for(svc, P["courier"]))
        # 原因方（采集医院）也不收到
        self.assertNotIn("case_cancelled", kinds_for(svc, P["donor_center"]))
        # 受者医院需要被知会调整治疗
        self.assertIn("case_cancelled", kinds_for(svc, P["recipient_hospital"]))


class AlternateDonorTest(unittest.TestCase):
    def test_swap_notifies_handover_chain_with_replacement_ref(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-SWAP1")
        svc.swap_donor(actor("coordinator"), cid,
                       reason="原供者复查肝功能异常",
                       replacement_case_id="CASE-SWAP2")
        proj = build_notification_projection(svc.ledger.all_events())
        courier = [n for n in proj.for_party(P["courier"])
                   if n.kind == "donor_swapped"]
        self.assertEqual(len(courier), 1)
        self.assertEqual(courier[0].vars["replacement_case_id"], "CASE-SWAP2")
        self.assertEqual(svc.get_case(cid).status, CaseStatus.SUPERSEDED)
        # 归档病例不能继续推进
        from coordination.service import CommandError
        with self.assertRaises(CommandError):
            svc.schedule_collection(actor("coordinator"), cid,
                                    start_local_iso="2026-09-30T08:00",
                                    end_local_iso="2026-09-30T14:00")

    def test_replacement_case_is_independent_full_flow(self):
        svc = make_service()
        old = open_ready_case(svc, "CASE-OLD")
        svc.swap_donor(actor("coordinator"), old, reason="原供者退出",
                       replacement_case_id="CASE-NEW")
        # 替代供者是独立病例，独立同意与排期，互不影响
        new = open_ready_case(svc, "CASE-NEW")
        schedule_and_confirm(svc, new)
        svc.complete_collection(actor("donor_center"), new, product_id="P-NEW")
        svc.handover(actor("donor_center"), new, kind="collection_to_courier",
                     from_person="王医生", to_person="钱押运",
                     product_temp_c=4.0, container_id="B-N", evidence_ref="HN1")
        svc.handover(actor("courier"), new, kind="courier_to_recipient",
                     from_person="钱押运", to_person="孙主任",
                     product_temp_c=4.5, container_id="B-N", evidence_ref="HN2")
        svc.deliver(actor("courier"), new)
        svc.accept_product(actor("recipient_hospital"), new, by_person="孙主任")
        svc.complete_infusion(actor("recipient_hospital"), new,
                              operator="孙主任")
        self.assertEqual(svc.get_case(new).status, CaseStatus.INFUSED)
        self.assertEqual(svc.get_case(old).status, CaseStatus.SUPERSEDED)


class MultiCaseIsolationTest(unittest.TestCase):
    def test_idempotency_and_notifications_scoped_per_case(self):
        svc = make_service()
        c1 = open_ready_case(svc, "CASE-M1")
        c2 = open_ready_case(svc, "CASE-M2")
        # 两个病例可以使用相同的业务幂等键而互不冲突（作用域为病例）
        svc.schedule_collection(actor("coordinator"), c1,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00",
                                idem_key="window-1")
        svc.schedule_collection(actor("coordinator"), c2,
                                start_local_iso="2026-09-26T08:00",
                                end_local_iso="2026-09-26T14:00",
                                idem_key="window-1")
        self.assertEqual(svc.get_case(c1).current_schedule.schedule_version, 1)
        self.assertEqual(svc.get_case(c2).current_schedule.schedule_version, 1)
        # 通知按病例隔离：c2 的确认通知不混入 c1
        proj = build_notification_projection(svc.ledger.all_events())
        self.assertTrue(all(n.case_id == c1
                            for n in proj.for_case(c1)))
        self.assertTrue(all(n.case_id == c2
                            for n in proj.for_case(c2)))


if __name__ == "__main__":
    unittest.main()
