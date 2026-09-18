"""同意规则：已确认同意不可改写，撤回只能以更高版本追加。"""

import unittest

from coord import states as S
from coord.errors import RuleViolation
from coord.testsupport import Driver, build_app


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)

    def test_grant_requires_passed_screening(self):
        self.d.to_matched()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_CONSENT_GRANTED,
                      {"document_ref": "C-1", "signed_at": "2026-09-15T10:00:00+08:00"},
                      actor="u_daff")

    def test_grant_is_versioned_and_immutable(self):
        self.d.to_screened()
        self.d.go(S.EV_CONSENT_GRANTED,
                  {"document_ref": "C-1", "signed_at": "2026-09-15T10:00:00+08:00"},
                  actor="u_daff")
        # 同编号同意书重复登记被拒（不能靠重发改写）
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_CONSENT_GRANTED,
                      {"document_ref": "C-1", "signed_at": "2026-09-16T10:00:00+08:00"},
                      actor="u_daff")
        case = self.d.case
        self.assertEqual(len(case.consent_versions), 1)
        self.assertEqual(case.effective_consent_version, 1)
        self.assertEqual(case.consent_versions[0].kind, S.CONSENT_GRANT)

    def test_renewed_grant_appends_new_version(self):
        self.d.to_scheduled()
        # 例如补充随访范围，重新签署：新版本追加，旧版本保留
        self.d.go(S.EV_CONSENT_GRANTED,
                  {"document_ref": "C-2", "signed_at": "2026-09-20T10:00:00+08:00",
                   "scopes": [S.SCOPE_HR_TYPING, S.SCOPE_MEDICAL_EXAM,
                              S.SCOPE_COLLECTION, S.SCOPE_FOLLOWUP]},
                  actor="u_daff")
        case = self.d.case
        self.assertEqual(len(case.consent_versions), 2)
        self.assertEqual(case.effective_consent_version, 2)
        self.assertEqual(case.consent_versions[0].version, 1)  # 历史版本原样保留

    def test_withdrawal_is_new_version_referencing_grant(self):
        self.d.to_scheduled()
        r = self.d.go(S.EV_CONSENT_WITHDRAWN, {
            "document_ref": "W-1", "signed_at": "2026-09-22T09:00:00+08:00",
            "note": "供者个人原因"}, actor="u_daff")
        self.assertTrue(r["applied"])
        case = self.d.case
        self.assertEqual(case.phase, S.DONOR_WITHDRAWN)
        self.assertIsNone(case.effective_consent_version)
        versions = case.consent_versions
        self.assertEqual(len(versions), 2)
        grant, withdrawal = versions
        self.assertEqual(grant.kind, S.CONSENT_GRANT)
        self.assertEqual(withdrawal.kind, S.CONSENT_WITHDRAWAL)
        self.assertEqual(withdrawal.supersedes_version, 1)  # 引用而非改写
        self.assertEqual(grant.document_ref, "CONS-T-001")   # 原件未被修改
        # 窗口随之释放
        self.assertTrue(all(s.status == S.SLOT_CANCELLED for s in case.slots))

    def test_double_withdrawal_rejected(self):
        self.d.to_scheduled()
        wd = {"document_ref": "W-1", "signed_at": "2026-09-22T09:00:00+08:00"}
        self.d.go(S.EV_CONSENT_WITHDRAWN, wd, actor="u_daff")
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_CONSENT_WITHDRAWN,
                      {"document_ref": "W-2", "signed_at": "2026-09-23T09:00:00+08:00"},
                      actor="u_daff")

    def test_withdrawal_blocks_without_effective_grant(self):
        self.d.to_screened()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_CONSENT_WITHDRAWN,
                      {"document_ref": "W-x", "signed_at": "2026-09-22T09:00:00+08:00"},
                      actor="u_daff")

    def test_withdrawal_after_transit_refused_direct_rewrite(self):
        self.d.to_in_transit()
        with self.assertRaises(RuleViolation):
            self.d.go(S.EV_CONSENT_WITHDRAWN,
                      {"document_ref": "W-9", "signed_at": "2026-09-25T05:00:00+08:00"},
                      actor="u_daff")

    def test_collected_product_pins_consent_version(self):
        self.d.to_collected()
        self.assertEqual(self.d.product.consent_version, 1)

    def test_withdrawal_after_collection_pre_transit_rejects_product(self):
        self.d.to_collected()
        self.d.go(S.EV_CONSENT_WITHDRAWN,
                  {"document_ref": "W-2", "signed_at": "2026-09-25T05:00:00+08:00"},
                  actor="u_daff")
        self.assertEqual(self.d.case.phase, S.DONOR_WITHDRAWN)
        # 已采集但未启运的产品不得再用于移植
        self.assertEqual(self.d.product.status, S.PROD_REJECTED)
        self.assertEqual(self.d.product.rejection_reason, "供者撤回同意")


if __name__ == "__main__":
    unittest.main()
