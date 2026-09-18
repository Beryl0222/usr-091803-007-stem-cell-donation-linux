"""状态机门禁与异常处置规则。"""

import unittest

from coord import states as S
from coord.errors import RuleViolation, ValidationError
from coord.testsupport import Driver, build_app


class WorkflowRuleTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)

    def test_cannot_confirm_without_proposal(self):
        self.d.to_consented()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_SLOT_CONFIRMED, {})

    def test_confirmed_slot_change_must_use_reschedule(self):
        self.d.to_scheduled()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_SLOT_PROPOSED,
                      {"start_local": "2026-09-28T09:00",
                       "end_local": "2026-09-28T14:00"}, actor="u_coll")

    def test_reschedule_keeps_consent_and_waits_new_confirmation(self):
        self.d.to_scheduled()
        self.d.go(S.EV_SLOT_RESCHEDULE, {
            "reason": "受者病情变化", "requested_by": "recipient",
            "new_start_local": "2026-09-27T09:00",
            "new_end_local": "2026-09-27T14:00"}, actor="u_doc")
        case = self.d.case
        self.assertEqual(case.phase, S.CONSENTED)
        self.assertEqual(case.effective_consent_version, 1)  # 同意不失效
        self.assertEqual(case.active_slot().status, S.SLOT_PROPOSED)
        old = case.slots[0]
        self.assertEqual(old.status, S.SLOT_CANCELLED)  # 旧窗口留痕

    def test_window_end_must_be_after_start(self):
        self.d.to_consented()
        with self.assertRaises(ValidationError):
            self.d.go(S.EV_SLOT_PROPOSED,
                      {"start_local": "2026-09-25T14:00",
                       "end_local": "2026-09-25T09:00"}, actor="u_coll")

    def test_handover_chain_holder_mismatch_rejected(self):
        self.d.to_collected()
        # 当前持有人是采集医院 u_coll；由押运员“交出”不成立
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_HANDOVER, {
                "product_code": self.d.product_code, "from_person_id": "u_car",
                "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
                "temp_c": 5.0}, actor="u_car")

    def test_broken_seal_requires_ack(self):
        self.d.to_collected()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_HANDOVER, {
                "product_code": self.d.product_code, "from_person_id": "u_coll",
                "to_org_id": "COLD-CHAIN", "to_person_id": "u_car",
                "temp_c": 5.0, "sealed": False}, actor="u_car")

    def test_open_excursion_blocks_delivery_and_infusion(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        self.d.go(S.EV_HANDOVER, {
            "product_code": self.d.product_code, "from_person_id": "u_car",
            "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
            "temp_c": 7.0, "at_utc": "2026-09-25T08:00:00Z"}, actor="u_recv")
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_DELIVERY, {"product_code": self.d.product_code}, actor="u_recv")

    def test_waiver_requires_reference(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        with self.assertRaises(ValidationError):
            self.d.go(S.EV_TEMP_RESOLVED,
                      {"excursion_id": self.d.excursion_id,
                       "disposition": S.DISP_RELEASE_WAIVER}, actor="u_doc")

    def test_resolved_excursion_cannot_be_rewritten(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        self.d.resolve_excursion(S.DISP_CONTINUE)
        with self.assertRaises(RuleViolation):
            self.d.resolve_excursion(S.DISP_RECOLLECT)

    def test_recollect_disposition_rejects_product(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        self.d.resolve_excursion(S.DISP_RECOLLECT)
        self.assertEqual(self.d.product.status, S.PROD_REJECTED)
        self.assertEqual(self.d.case.phase, S.RECOLLECT)

    def test_rejection_on_delivery_moves_to_recollect(self):
        self.d.to_in_transit()
        self.d.go(S.EV_HANDOVER, {
            "product_code": self.d.product_code, "from_person_id": "u_car",
            "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
            "temp_c": 9.5, "at_utc": "2026-09-25T08:00:00Z"}, actor="u_recv")
        self.d.go(S.EV_DELIVERY, {
            "product_code": self.d.product_code, "accepted": False,
            "reason": "到院复检温度记录异常"}, actor="u_recv")
        self.assertEqual(self.d.case.phase, S.RECOLLECT)
        self.assertEqual(self.d.product.status, S.PROD_REJECTED)

    def test_full_happy_path_phases(self):
        self.d.to_infused()
        self.assertEqual(self.d.case.phase, S.CLOSED)
        self.assertEqual(self.d.product.status, S.PROD_INFUSED)
        self.assertEqual(self.d.product.infusing_physician_id, "u_doc")

    def test_recollect_then_second_product_completes_case(self):
        self.d.to_in_transit()
        self.d.report_excursion()
        self.d.resolve_excursion(S.DISP_RECOLLECT)
        case = self.d.case
        self.assertEqual(case.phase, S.RECOLLECT)
        # 同意仍有效，重新排期
        self.d.go(S.EV_SLOT_PROPOSED,
                  {"start_local": "2026-09-30T09:00",
                   "end_local": "2026-09-30T14:00"}, actor="u_coll")
        self.d.go(S.EV_SLOT_CONFIRMED, {"slot_version": 2}, actor="u_coord")
        self.assertEqual(case.phase, S.SCHEDULED)
        self.d.go(S.EV_COLLECTION_STARTED, {}, actor="u_coll")
        self.d.product_code = "HSC-TEST-002"
        self.d.go(S.EV_COLLECTION_COMPLETED,
                  {"product_code": self.d.product_code, "collected_by": "u_coll"},
                  actor="u_coll")
        self.d.to_infused()
        self.assertEqual(case.phase, S.CLOSED)
        # 第一支报废产品仍保留在时间线中
        codes = sorted(self.app.store.products[pid].code for pid in case.product_ids)
        self.assertEqual(codes, ["HSC-TEST-001", "HSC-TEST-002"])
        first = next(p for p in self.app.store.products.values()
                     if p.code == "HSC-TEST-001")
        self.assertEqual(first.status, S.PROD_REJECTED)

    def test_alternative_donor_starts_fresh_case_and_preserves_old(self):
        self.d.to_scheduled()
        self.d.go(S.EV_CONSENT_WITHDRAWN,
                  {"document_ref": "W-1", "signed_at": "2026-09-22T09:00:00+08:00"},
                  actor="u_daff")
        # 需要第二位供者
        from coord.identity import Channel, Person
        self.app.directory.add_person(Person(
            "donor-88", "毕（替代供者）", "志愿捐献者", (), "REG-XJ", [], ""))
        r = self.d.go(S.EV_ALT_DONOR,
                      {"donor_person_id": "donor-88", "case_code": "CASE-ALT-1"},
                      actor="u_coord")
        self.assertTrue(r["applied"])
        old = self.d.case
        self.assertTrue(old.superseded)
        self.assertEqual(old.replaced_by_case_id, r["case_id"])
        new = self.app.store.cases[r["case_id"]]
        self.assertEqual(new.phase, S.MATCHED)
        self.assertEqual(new.donor_person_id, "donor-88")
        # 新病例沿用同一检索与医院链路，但同意/窗口全部重来
        self.assertEqual(new.consent_versions, [])
        self.assertEqual(new.slots, [])


if __name__ == "__main__":
    unittest.main()
