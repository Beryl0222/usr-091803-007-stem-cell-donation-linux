"""通知路由：只通知有动作项的人；重发留痕；送达回执幂等。"""

import unittest

from coordination.models import Role
from coordination.notifications import build_notification_projection
from coordination.service import StubSmsChannel
from testsupport import (
    make_service, open_ready_case, schedule_and_confirm, actor, PARTIES,
    DONOR_IDENTITY, RECIPIENT_IDENTITY,
)

P = PARTIES


def notes_by_party(svc):
    proj = build_notification_projection(svc.ledger.all_events())
    out = {}
    for n in proj.all():
        out.setdefault(n.party_id, []).append(n)
    return out


def kinds_for(svc, party):
    return [n.kind for n in notes_by_party(svc).get(party, [])]


def parties_notified(svc):
    return set(notes_by_party(svc).keys())


class NotificationRoutingTest(unittest.TestCase):
    def test_match_only_alerts_two_centers_not_courier_or_donor(self):
        svc = make_service()
        # 仅建档（非血缘配型成功通知到达）这一个时刻
        svc.open_case(
            actor("coordinator"),
            donor_identity=dict(DONOR_IDENTITY),
            recipient_identity=dict(RECIPIENT_IDENTITY),
            parties=dict(PARTIES), case_id="CASE-N1")
        notified = parties_notified(svc)
        # 建档那一刻只有供者侧医院与受者医院需要启动准备
        self.assertIn(P["donor_center"], notified)
        self.assertIn(P["recipient_hospital"], notified)
        self.assertNotIn(P["courier"], notified)
        self.assertNotIn(P["donor"], notified)

    def test_screening_pass_prompts_donor_to_sign(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-N2")
        self.assertIn("sign_consent", kinds_for(svc, P["donor"]))

    def test_schedule_notifies_four_parties_each_in_own_local_time(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-N3", tzs={
            "donor": "Asia/Urumqi",
            "donor_center": "Asia/Urumqi",
            "courier": "Asia/Shanghai",
            "recipient_hospital": "Asia/Shanghai",
        })
        svc.schedule_collection(
            actor("coordinator"), cid,
            start_local_iso="2026-09-25T08:00",
            end_local_iso="2026-09-25T14:00", tz="Asia/Urumqi",
            reason="受者床位落实")
        nb = notes_by_party(svc)
        # 四方都被要求确认
        for party in (P["donor"], P["donor_center"], P["courier"],
                      P["recipient_hospital"]):
            kinds = [n.kind for n in nb[party]]
            self.assertIn("confirm_window", kinds)

        # 承运方（上海）看到的窗口是当地时间 10:00–16:00，而非乌鲁木齐 08:00
        courier_note = next(n for n in nb[P["courier"]]
                            if n.kind == "confirm_window")
        self.assertIn("2026-09-25T10:00:00+08:00",
                      courier_note.vars["start_local"])
        donor_note = next(n for n in nb[P["donor"]]
                          if n.kind == "confirm_window")
        self.assertIn("2026-09-25T08:00:00+06:00",
                      donor_note.vars["start_local"])

    def test_reschedule_only_asks_for_reconfirmation(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-N4")
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00")
        for role in ("donor", "donor_center", "courier", "recipient_hospital"):
            svc.confirm_schedule(actor(role), cid, party_id=P[role])
        # 受者床位变化迫使改期
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-27T08:00",
                                end_local_iso="2026-09-27T14:00",
                                reason="受者床位调整")
        nb = notes_by_party(svc)
        # 新窗口下四方各收到再次确认（v2）
        for party in (P["donor"], P["donor_center"], P["courier"],
                      P["recipient_hospital"]):
            confirm_notes = [n for n in nb[party] if n.kind == "confirm_window"]
            self.assertEqual(len(confirm_notes), 2)
            self.assertEqual(confirm_notes[1].vars["schedule_version"], 2)
            self.assertEqual(confirm_notes[1].vars["reason"], "受者床位调整")

    def test_withdraw_after_schedule_alerts_only_parties_with_actions(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-N5")
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00")
        notified_before = set(notes_by_party(svc).keys())
        svc.withdraw_consent(actor("donor"), cid,
                             document_version="W-v1", document_hash="w" * 64,
                             reason="临时发热")
        nb = notes_by_party(svc)
        # 撤回动作的发出者（捐献者本人）不收到撤回通知
        withdraw_kinds = [n.kind for n in nb.get(P["donor"], [])]
        self.assertNotIn("consent_withdrawn_disposition", withdraw_kinds)
        # 已排期：协调员、采集医院、承运方（释放运力）、受者医院（调整床位）
        for party, hint in [
            (P["coordinator"], None), (P["donor_center"], None),
            (P["courier"], "释放"), (P["recipient_hospital"], "预处理")]:
            notes = [n for n in nb.get(party, [])
                     if n.kind == "consent_withdrawn_disposition"]
            self.assertEqual(len(notes), 1, f"{party} 应收到恰好一条善后通知")

    def test_withdraw_before_schedule_does_not_disturb_courier(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-N6")
        svc.withdraw_consent(actor("donor"), cid,
                             document_version="W-v1", document_hash="w" * 64)
        # 尚未排期，承运方无运力可释放，不应被打扰
        self.assertNotIn("consent_withdrawn_disposition",
                         kinds_for(svc, P["courier"]))


class ExcursionNotificationTest(unittest.TestCase):
    def _in_transit(self, cid):
        svc = make_service()
        open_ready_case(svc, cid)
        schedule_and_confirm(svc, cid)
        svc.complete_collection(actor("donor_center"), cid,
                                product_id="PROD-X", volume_ml=180)
        svc.handover(actor("donor_center"), cid, kind="collection_to_courier",
                     from_person="王医生", to_person="赵押运",
                     product_temp_c=4.0, container_id="BOX-1",
                     evidence_ref="HO-1")
        return svc

    def test_excursion_alerts_only_three_responsible_parties(self):
        svc = self._in_transit("CASE-E1")
        svc.report_excursion(
            actor("courier"), "CASE-E1", temp_c=11.5,
            limit_low_c=2.0, limit_high_c=8.0,
            reading_local_iso="2026-09-25T20:10",
            external_source="coldchain-iot", external_event_id="reading-77")
        nb = notes_by_party(svc)
        kinds = {party: [n.kind for n in notes]
                 for party, notes in nb.items()}
        self.assertIn("excursion_handle", kinds.get(P["courier"], []))
        self.assertIn("excursion_decide", kinds.get(P["coordinator"], []))
        self.assertIn("hold_infusion", kinds.get(P["recipient_hospital"], []))
        # 供者与采集医院没有可执行动作，不通知
        self.assertNotIn("excursion_handle", kinds.get(P["donor"], []))
        self.assertFalse(any(k.startswith("excursion") or k == "hold_infusion"
                             for k in kinds.get(P["donor_center"], [])))

    def test_duplicate_excursion_reading_notifies_once(self):
        svc = self._in_transit("CASE-E2")
        args = dict(temp_c=11.5, limit_low_c=2.0, limit_high_c=8.0,
                    reading_local_iso="2026-09-25T20:10",
                    external_source="coldchain-iot",
                    external_event_id="reading-77")
        r1 = svc.report_excursion(actor("courier"), "CASE-E2", **args)
        n1 = len(svc.ledger.all_events())
        r2 = svc.report_excursion(actor("courier"), "CASE-E2", **args)
        self.assertEqual(r1[0], "recorded")
        self.assertEqual(r2[0], "duplicate_external")
        # 重复读数未产生任何新事件、未重复通知
        self.assertEqual(len(svc.ledger.all_events()), n1)
        nb = notes_by_party(svc)
        self.assertEqual(
            sum(1 for n in nb.get(P["courier"], [])
                if n.kind == "excursion_handle"), 1)

    def test_released_notifies_hospital_and_courier_to_continue(self):
        svc = self._in_transit("CASE-E3")
        svc.report_excursion(
            actor("courier"), "CASE-E3", temp_c=9.2,
            limit_low_c=2.0, limit_high_c=8.0,
            reading_local_iso="2026-09-25T20:10",
            external_source="coldchain-iot", external_event_id="r-1")
        svc.resolve_excursion(actor("coordinator"), "CASE-E3",
                              result="released_with_note",
                              note="偏离 12 分钟，细胞活力达标，附条件放行")
        kinds = kinds_for(svc, P["recipient_hospital"])
        self.assertIn("excursion_released", kinds)
        self.assertIn("excursion_released", kinds_for(svc, P["courier"]))

    def test_discarded_alerts_replenishment_chain(self):
        svc = self._in_transit("CASE-E4")
        svc.report_excursion(
            actor("courier"), "CASE-E4", temp_c=18.0,
            limit_low_c=2.0, limit_high_c=8.0,
            reading_local_iso="2026-09-25T21:00",
            external_source="coldchain-iot", external_event_id="r-2")
        svc.resolve_excursion(actor("coordinator"), "CASE-E4",
                              result="discarded", note="超限过久，报废")
        nb = notes_by_party(svc)
        for party in (P["coordinator"], P["recipient_hospital"],
                      P["donor_center"], P["courier"]):
            self.assertIn("excursion_discarded",
                          [n.kind for n in nb.get(party, [])])


class ResendAndReceiptTest(unittest.TestCase):
    def test_resend_records_every_attempt_and_receipt_is_idempotent(self):
        channel = StubSmsChannel()
        svc = make_service(channel)
        cid = open_ready_case(svc, "CASE-R1")
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00")
        proj = build_notification_projection(svc.ledger.all_events())
        target = next(n for n in proj.for_party(P["courier"])
                      if n.kind == "confirm_window")
        ext_msg = next(s["external_message_id"] for s in channel.sent
                       if s["party_id"] == P["courier"]
                       and s["kind"] == "confirm_window")

        # 手动重发，每次尝试都留痕
        svc.resend_notification(actor("coordinator"),
                                target.notification_id)
        proj = build_notification_projection(svc.ledger.all_events())
        n = proj.get(target.notification_id)
        self.assertGreaterEqual(len(n.attempts), 2)

        # 首次送达回执推进状态
        r1 = svc.record_delivery_receipt(
            actor("coordinator"), external_message_id=ext_msg,
            external_source="sms-gateway", external_event_id="rcpt-1")
        self.assertEqual(r1[0], "delivered")
        # 网关重复回调：只推进一次
        r2 = svc.record_delivery_receipt(
            actor("coordinator"), external_message_id=ext_msg,
            external_source="sms-gateway", external_event_id="rcpt-1")
        self.assertEqual(r2[0], "duplicate_external")
        proj = build_notification_projection(svc.ledger.all_events())
        delivered = [x for x in proj.all()
                     if x.status == "delivered"]
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
