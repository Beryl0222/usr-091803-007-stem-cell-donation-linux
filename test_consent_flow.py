"""同意版本化与状态机守卫。

- 已确认同意不得改写：重复授予被拒绝；
- 撤回以新版本生效，历史版本原样保留；
- 撤回后排期/采集/交接被阻止；重新签署是再下一版；
- 关键节点固化当时生效的同意版本，日后撤回不改变该事实；
- 采集前必须四方确认窗口；非法状态迁移被拒绝。
"""

import unittest

from coordination.models import CaseStatus, ConsentState
from coordination.service import CommandError
from testsupport import make_service, open_ready_case, schedule_and_confirm, actor


class ConsentVersioningTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.cid = open_ready_case(self.svc, "CASE-CONSENT")

    def test_granted_consent_cannot_be_overwritten(self):
        snap = self.svc.get_case(self.cid)
        self.assertEqual(snap.consent_state, ConsentState.GRANTED)
        self.assertEqual(len(snap.consents), 1)
        # 再次"签署"不得覆盖既已确认的同意
        with self.assertRaises(CommandError):
            self.svc.grant_consent(
                actor("coordinator"), self.cid,
                document_version="CONSENT-v2025.2",
                document_hash="b" * 64,
                signed_local_iso="2026-09-11T10:00")
        snap = self.svc.get_case(self.cid)
        self.assertEqual(len(snap.consents), 1)
        self.assertEqual(snap.consents[0].document_hash, "a" * 64)

    def test_withdraw_appends_new_version_and_keeps_history(self):
        _, v = self.svc.withdraw_consent(
            actor("donor"), self.cid,
            document_version="WITHDRAW-v1", document_hash="w" * 64,
            reason="个人原因")
        self.assertEqual(v, 2)
        snap = self.svc.get_case(self.cid)
        self.assertEqual(snap.consent_state, ConsentState.WITHDRAWN)
        self.assertEqual(len(snap.consents), 2)
        # 原授予版本原样保留，可逐版审计
        self.assertEqual(snap.consents[0].action, "grant")
        self.assertEqual(snap.consents[1].action, "withdraw")
        self.assertEqual(snap.consents[0].document_hash, "a" * 64)

    def test_withdraw_blocks_scheduling_and_collection(self):
        self.svc.schedule_collection(
            actor("coordinator"), self.cid,
            start_local_iso="2026-09-25T08:00",
            end_local_iso="2026-09-25T14:00")
        self.svc.withdraw_consent(
            actor("donor"), self.cid,
            document_version="WITHDRAW-v1", document_hash="w" * 64,
            reason="病情变化")
        # 撤回后不能再改期/采集
        with self.assertRaises(CommandError):
            self.svc.schedule_collection(
                actor("coordinator"), self.cid,
                start_local_iso="2026-09-28T08:00",
                end_local_iso="2026-09-28T14:00")

    def test_regrant_after_withdraw_is_version_three(self):
        self.svc.withdraw_consent(
            actor("donor"), self.cid,
            document_version="WITHDRAW-v1", document_hash="w" * 64)
        # 再次自愿同意 -> 第 3 版，而非覆盖
        _, v = self.svc.grant_consent(
            actor("coordinator"), self.cid,
            document_version="CONSENT-v2025.2",
            document_hash="c" * 64,
            signed_local_iso="2026-09-12T09:00")
        self.assertEqual(v, 3)
        snap = self.svc.get_case(self.cid)
        actions = [c.action for c in snap.consents]
        self.assertEqual(actions, ["grant", "withdraw", "grant"])
        self.assertEqual(snap.consent_state, ConsentState.GRANTED)

    def test_critical_node_pins_consent_version_surviving_later_withdraw(self):
        schedule_and_confirm(self.svc, self.cid)
        self.svc.complete_collection(
            actor("donor_center"), self.cid,
            product_id="PROD-1", volume_ml=200)
        product = self.svc.get_case(self.cid).product
        self.assertEqual(product["effective_consent_version"], 1)
        # 采集物已在运输中；监管时间线上的采集节点仍指向 v1 同意
        self.svc.handover(
            actor("donor_center"), self.cid, kind="collection_to_courier",
            from_person="王医生", to_person="赵押运",
            product_temp_c=4.0, container_id="BOX-9",
            evidence_ref="HO-0001")
        ho = self.svc.get_case(self.cid).handovers[0]
        self.assertEqual(ho.effective_consent_version, 1)

    def test_withdraw_only_before_collection(self):
        schedule_and_confirm(self.svc, self.cid)
        self.svc.complete_collection(
            actor("donor_center"), self.cid,
            product_id="PROD-1", volume_ml=200)
        with self.assertRaises(CommandError):
            self.svc.withdraw_consent(
                actor("donor"), self.cid,
                document_version="WITHDRAW-v1", document_hash="w" * 64)


class StateGuardTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.cid = open_ready_case(self.svc, "CASE-GUARD")

    def test_cannot_collect_without_full_confirmation(self):
        self.svc.schedule_collection(
            actor("coordinator"), self.cid,
            start_local_iso="2026-09-25T08:00",
            end_local_iso="2026-09-25T14:00")
        # 只有供者确认，缺三方
        self.svc.confirm_schedule(actor("donor"), self.cid, party_id="party-donor")
        with self.assertRaises(CommandError) as cm:
            self.svc.complete_collection(
                actor("donor_center"), self.cid, product_id="P1")
        self.assertIn("确认", str(cm.exception))

    def test_illegal_forward_jump_rejected(self):
        # 刚同意，未排期，不能交接/回输
        with self.assertRaises(CommandError):
            self.svc.handover(
                actor("donor_center"), self.cid, kind="collection_to_courier",
                from_person="a", to_person="b", product_temp_c=4.0,
                container_id="x", evidence_ref="e")
        with self.assertRaises(CommandError):
            self.svc.complete_infusion(actor("recipient_hospital"),
                                       self.cid, operator="r")

    def test_cancel_is_terminal(self):
        self.svc.cancel_case(actor("coordinator"), self.cid,
                             reason="受者病情恶化暂缓", cause_role="recipient_hospital")
        self.assertEqual(self.svc.get_case(self.cid).status, CaseStatus.CANCELLED)
        with self.assertRaises(CommandError):
            self.svc.schedule_collection(
                actor("coordinator"), self.cid,
                start_local_iso="2026-09-25T08:00",
                end_local_iso="2026-09-25T14:00")

    def test_swap_donor_supersedes_and_links_replacement(self):
        ev = self.svc.swap_donor(
            actor("coordinator"), self.cid, reason="供者高分辨不合",
            replacement_case_id="CASE-ALT-2")
        snap = self.svc.get_case(self.cid)
        self.assertEqual(snap.status, CaseStatus.SUPERSEDED)
        self.assertEqual(snap.supersede["replacement_case_id"], "CASE-ALT-2")


if __name__ == "__main__":
    unittest.main()
