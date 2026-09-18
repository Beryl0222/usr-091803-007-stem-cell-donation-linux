"""隐私最小化、时区窗口解释、冷链处置状态机。"""

import unittest
from datetime import datetime

from coordination.clock import Window, to_instant, format_local, UTC
from coordination.models import Role, ShipmentStatus
from coordination.privacy import (
    project_identity, DONOR_FIELD_VISIBILITY, RECIPIENT_FIELD_VISIBILITY,
)
from coordination.views import case_for_party
from coordination.service import CommandError
from testsupport import (
    make_service, open_ready_case, schedule_and_confirm, actor,
    DONOR_IDENTITY, RECIPIENT_IDENTITY,
)


class PrivacyTest(unittest.TestCase):
    def test_field_level_visibility_matrix(self):
        # 协调员可见供者全名；承运方不可见
        self.assertIn("full_name",
                      project_identity(DONOR_IDENTITY, Role.COORDINATOR,
                                       DONOR_FIELD_VISIBILITY))
        donor_to_courier = project_identity(DONOR_IDENTITY, Role.COURIER,
                                            DONOR_FIELD_VISIBILITY)
        self.assertEqual(donor_to_courier, {})
        # 受者医院看得到受者，看不到供者
        self.assertIn("full_name",
                      project_identity(RECIPIENT_IDENTITY, Role.RECIPIENT_HOSPITAL,
                                       RECIPIENT_FIELD_VISIBILITY))

    def test_double_blind_case_view(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-PRIV")
        # 受者医院视角：供者仅为代号
        rh = case_for_party(svc.ledger, cid, Role.RECIPIENT_HOSPITAL)
        self.assertTrue(rh["identities"]["donor"].get("pseudonym", "").startswith("DONOR-"))
        self.assertNotIn("full_name", rh["identities"]["donor"])
        self.assertEqual(rh["identities"]["recipient"]["full_name"],
                         RECIPIENT_IDENTITY["full_name"])
        # 承运方双盲：双方都只是代号
        co = case_for_party(svc.ledger, cid, Role.COURIER)
        self.assertIn("pseudonym", co["identities"]["donor"])
        self.assertIn("pseudonym", co["identities"]["recipient"])
        self.assertNotIn("contact_phone", co["identities"]["donor"])
        # 供者侧医院可见供者医疗信息，但仍不见受者身份
        dc = case_for_party(svc.ledger, cid, Role.DONOR_CENTER)
        self.assertEqual(dc["identities"]["donor"]["full_name"],
                         DONOR_IDENTITY["full_name"])
        self.assertNotIn("full_name", dc["identities"]["recipient"])

    def test_regulator_gets_no_plaintext_identity(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-PRIV2")
        reg = case_for_party(svc.ledger, cid, Role.REGULATOR)
        self.assertNotIn("full_name", reg["identities"]["donor"])
        self.assertNotIn("full_name", reg["identities"]["recipient"])


class TimezoneWindowTest(unittest.TestCase):
    def test_same_instant_different_local_walls(self):
        # 乌鲁木齐 08:00 (+06:00) == 上海 10:00 (+08:00)
        instant = to_instant("2026-09-25T08:00", "Asia/Urumqi")
        self.assertEqual(format_local(instant, "Asia/Shanghai"),
                         "2026-09-25T10:00:00+08:00")
        self.assertEqual(instant.astimezone(UTC).isoformat(),
                         "2026-09-25T02:00:00+00:00")

    def test_window_declared_by_center_is_unambiguous_utc(self):
        w = Window.from_local("2026-09-25T08:00", "2026-09-25T14:00",
                              "Asia/Urumqi")
        self.assertEqual(w.start.isoformat(), "2026-09-25T02:00:00+00:00")
        self.assertEqual(w.duration_minutes(), 360)
        sh = w.view("Asia/Shanghai")
        self.assertEqual(sh["start_local"], "2026-09-25T10:00:00+08:00")
        self.assertEqual(sh["end_local"], "2026-09-25T16:00:00+08:00")

    def test_each_party_confirms_against_own_local_time(self):
        svc = make_service()
        cid = open_ready_case(svc, "CASE-TZ", tzs={
            "donor": "Asia/Urumqi", "donor_center": "Asia/Urumqi",
            "courier": "Asia/Shanghai", "recipient_hospital": "Asia/Shanghai"})
        svc.schedule_collection(actor("coordinator"), cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00",
                                tz="Asia/Urumqi")
        # 承运方工作台按上海时间展示同一窗口
        co = case_for_party(svc.ledger, cid, Role.COURIER)
        self.assertEqual(co["schedule"]["window"]["start_local"],
                         "2026-09-25T10:00:00+08:00")
        # 供者工作台按乌鲁木齐时间
        dn = case_for_party(svc.ledger, cid, Role.DONOR)
        self.assertEqual(dn["schedule"]["window"]["start_local"],
                         "2026-09-25T08:00:00+06:00")

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):
            Window.from_local("2026-09-25T08:00", "2026-09-25T14:00",
                              "Mars/Olympus")


class ColdChainTest(unittest.TestCase):
    def _in_transit(self, cid):
        svc = make_service()
        open_ready_case(svc, cid)
        schedule_and_confirm(svc, cid)
        svc.complete_collection(actor("donor_center"), cid,
                                product_id="PROD-C", volume_ml=170)
        svc.handover(actor("donor_center"), cid, kind="collection_to_courier",
                     from_person="王医生", to_person="赵押运",
                     product_temp_c=4.0, container_id="BOX-C",
                     evidence_ref="HO-C")
        return svc

    def test_in_range_reading_is_not_an_excursion(self):
        svc = self._in_transit("CASE-C1")
        with self.assertRaises(CommandError):
            svc.report_excursion(
                actor("courier"), "CASE-C1", temp_c=5.0,
                limit_low_c=2.0, limit_high_c=8.0,
                reading_local_iso="2026-09-25T20:00",
                external_source="iot", external_event_id="ok-1")

    def test_quarantine_blocks_handover_until_resolution(self):
        svc = self._in_transit("CASE-C2")
        svc.report_excursion(
            actor("courier"), "CASE-C2", temp_c=12.0,
            limit_low_c=2.0, limit_high_c=8.0,
            reading_local_iso="2026-09-25T20:00",
            external_source="iot", external_event_id="hot-1")
        self.assertEqual(svc.get_case("CASE-C2").shipment_status,
                         ShipmentStatus.QUARANTINED)
        # 隔离中不能向受者医院交接
        with self.assertRaises(CommandError):
            svc.handover(actor("courier"), "CASE-C2",
                         kind="courier_to_recipient",
                         from_person="赵押运", to_person="孙主任",
                         product_temp_c=6.0, container_id="BOX-C",
                         evidence_ref="HO-C2")

    def test_release_resumes_transit_and_full_chain_completes(self):
        svc = self._in_transit("CASE-C3")
        svc.report_excursion(
            actor("courier"), "CASE-C3", temp_c=9.0,
            limit_low_c=2.0, limit_high_c=8.0,
            reading_local_iso="2026-09-25T20:00",
            external_source="iot", external_event_id="hot-2")
        svc.resolve_excursion(actor("coordinator"), "CASE-C3",
                              result="released", note="短暂偏离，评估放行")
        snap = svc.get_case("CASE-C3")
        self.assertEqual(snap.shipment_status, ShipmentStatus.IN_TRANSIT)
        self.assertEqual(snap.excursions[-1].resolution["result"], "released")
        # 放行后完成送达-签收-回输
        svc.handover(actor("courier"), "CASE-C3", kind="courier_to_recipient",
                     from_person="赵押运", to_person="孙主任",
                     product_temp_c=5.5, container_id="BOX-C",
                     evidence_ref="HO-C3")
        svc.deliver(actor("courier"), "CASE-C3")
        svc.accept_product(actor("recipient_hospital"), "CASE-C3",
                           by_person="孙主任", note="核验合格")
        svc.complete_infusion(actor("recipient_hospital"), "CASE-C3",
                              operator="孙主任")
        self.assertEqual(svc.get_case("CASE-C3").status.value, "infused")

    def test_cannot_resolve_without_open_excursion(self):
        svc = self._in_transit("CASE-C4")
        with self.assertRaises(CommandError):
            svc.resolve_excursion(actor("coordinator"), "CASE-C4",
                                  result="released")


if __name__ == "__main__":
    unittest.main()
