"""监管时间线：关键节点的同意版本、交接人、异常处置、防篡改结论。"""

import unittest

from coordination.audit import build_timeline, render_timeline_text
from coordination.models import Role
from testsupport import make_service, open_ready_case, schedule_and_confirm, actor


def _full_case(cid="CASE-AUDIT"):
    svc = make_service()
    open_ready_case(svc, cid)
    schedule_and_confirm(svc, cid)
    svc.complete_collection(actor("donor_center"), cid,
                            product_id="PROD-A", volume_ml=190)
    svc.handover(actor("donor_center"), cid, kind="collection_to_courier",
                 from_person="王医生", to_person="赵押运",
                 product_temp_c=4.0, container_id="BOX-A",
                 evidence_ref="HO-A1")
    # 途中短暂偏离后放行
    svc.report_excursion(actor("courier"), cid, temp_c=9.5,
                         limit_low_c=2.0, limit_high_c=8.0,
                         reading_local_iso="2026-09-25T20:30",
                         external_source="iot", external_event_id="audit-1")
    svc.resolve_excursion(actor("coordinator"), cid, result="released_with_note",
                          note="偏离 9 分钟，活力达标")
    svc.handover(actor("courier"), cid, kind="courier_to_recipient",
                 from_person="赵押运", to_person="孙主任",
                 product_temp_c=5.0, container_id="BOX-A",
                 evidence_ref="HO-A2")
    svc.deliver(actor("courier"), cid)
    svc.accept_product(actor("recipient_hospital"), cid, by_person="孙主任")
    svc.complete_infusion(actor("recipient_hospital"), cid, operator="孙主任")
    return svc


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.svc = _full_case()
        self.tl = build_timeline(self.svc.ledger, "CASE-AUDIT",
                                 viewer_tz="Asia/Shanghai",
                                 viewer_role=Role.REGULATOR)

    def test_timeline_covers_every_stage_in_order(self):
        nodes = [e["node"] for e in self.tl["entries"]]
        required = ["case_opened", "screening_started", "screening_passed",
                    "consent_granted", "collection_scheduled",
                    "schedule_confirmed", "collection_completed",
                    "handover", "shipment_excursion", "excursion_resolved",
                    "shipment_delivered", "product_accepted",
                    "infusion_completed"]
        for r in required:
            self.assertIn(r, nodes)
        # 条目严格按事件序号升序
        seqs = [e["seq"] for e in self.tl["entries"]]
        self.assertEqual(seqs, sorted(seqs))
        # 各阶段首次出现的相对次序正确（允许 handover 出现两次）
        first_pos = [nodes.index(r) for r in required]
        self.assertEqual(first_pos, sorted(first_pos))

    def test_critical_nodes_state_adopted_consent_version(self):
        critical = [e for e in self.tl["entries"] if e["critical"]]
        self.assertTrue(critical)
        for e in critical:
            self.assertTrue(e["consent_in_force"]["consent_present"])
            self.assertEqual(e["consent_in_force"]["version"], 1)
        collection = next(e for e in self.tl["entries"]
                          if e["node"] == "collection_completed")
        self.assertEqual(collection["detail"]["adopted_consent_version"], 1)
        self.assertEqual(collection["detail"]["adopted_consent_document_hash"],
                         "a" * 64)

    def test_handovers_name_both_persons_and_evidence(self):
        hos = [e for e in self.tl["entries"] if e["node"] == "handover"]
        self.assertEqual(len(hos), 2)
        first = hos[0]["detail"]
        self.assertEqual(first["handover_from_person"], "王医生")
        self.assertEqual(first["handover_to_person"], "赵押运")
        self.assertEqual(first["evidence_ref"], "HO-A1")
        self.assertTrue(first["dual_signature"])
        second = hos[1]["detail"]
        self.assertEqual(second["handover_from_person"], "赵押运")
        self.assertEqual(second["handover_to_person"], "孙主任")

    def test_excursion_carries_final_resolution(self):
        exc = next(e for e in self.tl["entries"]
                   if e["node"] == "shipment_excursion")
        self.assertIsNotNone(exc["detail"]["resolution"])
        self.assertEqual(exc["detail"]["resolution"]["result"],
                         "released_with_note")
        self.assertEqual(exc["detail"]["resolution"]["decided_by"],
                         "party-coord")
        self.assertEqual(self.tl["open_excursions"], [])

    def test_integrity_verifies_and_regulator_sees_pseudonyms(self):
        self.assertTrue(self.tl["integrity"]["ok"])
        opening = next(e for e in self.tl["entries"]
                       if e["node"] == "case_opened")
        self.assertIn("pseudonym", opening["detail"]["donor"])
        self.assertNotIn("full_name", opening["detail"]["donor"])

    def test_text_render_includes_consent_and_integrity(self):
        text = render_timeline_text(self.tl)
        self.assertIn("监管复核时间线", text)
        self.assertIn("采用同意: v1", text)
        self.assertIn("哈希链校验: 通过", text)
        self.assertIn("王医生", text)

    def test_consent_history_remains_after_later_withdraw_version(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-AUDIT-W")
        svc.withdraw_consent(actor("donor"), cid,
                             document_version="W-v1", document_hash="w" * 64,
                             reason="个人原因")
        tl = build_timeline(svc.ledger, cid, viewer_role=Role.REGULATOR)
        versions = tl["consent_ledger"]
        self.assertEqual([v["action"] for v in versions],
                         ["grant", "withdraw"])
        # 旧授予版本的文书哈希原样保留，未被撤回覆盖
        self.assertEqual(versions[0]["document_hash"], "a" * 64)
        self.assertEqual(tl["current_consent_state"], "withdrawn")

    def test_open_excursion_flagged_when_unresolved(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-AUDIT-O")
        schedule_and_confirm(svc, cid)
        svc.complete_collection(actor("donor_center"), cid, product_id="P")
        svc.handover(actor("donor_center"), cid, kind="collection_to_courier",
                     from_person="王医生", to_person="赵押运",
                     product_temp_c=4.0, container_id="B", evidence_ref="H")
        svc.report_excursion(actor("courier"), cid, temp_c=14.0,
                             limit_low_c=2.0, limit_high_c=8.0,
                             reading_local_iso="2026-09-25T21:00",
                             external_source="iot", external_event_id="open-1")
        tl = build_timeline(svc.ledger, cid, viewer_role=Role.REGULATOR)
        self.assertEqual(len(tl["open_excursions"]), 1)


if __name__ == "__main__":
    unittest.main()
