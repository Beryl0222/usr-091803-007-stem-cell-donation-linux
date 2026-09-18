"""台账：外部事件去重、命令幂等、哈希链防篡改、持久化重放。"""

import os
import tempfile
import unittest

from coordination.events import Ledger, DuplicateExternalEvent, GENESIS_HASH
from coordination.models import Actor, Role
from coordination.service import CommandError
from testsupport import make_service, open_ready_case, actor, PARTIES


COORD = actor(Role.COORDINATOR)


class LedgerTest(unittest.TestCase):
    def test_duplicate_external_event_applied_once(self):
        svc = make_service()
        cid = open_ready_case(svc)
        svc.schedule_collection(COORD, cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00",
                                tz="Asia/Urumqi")
        n_before = len(svc.ledger.all_events())

        # 医院系统回调确认窗口
        r1 = svc.confirm_schedule(
            actor(Role.RECIPIENT_HOSPITAL), cid, party_id=PARTIES["recipient_hospital"],
            external_source="his-rh", external_event_id="cb-1001")
        n_after_first = len(svc.ledger.all_events())
        # 同一回调因网络重试重复到达
        r2 = svc.confirm_schedule(
            actor(Role.RECIPIENT_HOSPITAL), cid, party_id=PARTIES["recipient_hospital"],
            external_source="his-rh", external_event_id="cb-1001")

        self.assertEqual(r1[0], "confirmed")
        # 重复回调无论走外部幂等去重还是快照短路，都不得二次推进
        self.assertIn(r2[0], ("duplicate_external", "already_confirmed"))
        if r2[0] == "duplicate_external":
            self.assertEqual(r1[1].seq, r2[1].seq)
        # 重复到达后没有任何新增事件（状态与通知都只推进一次）
        self.assertEqual(len(svc.ledger.all_events()), n_after_first)
        self.assertGreater(n_after_first, n_before)
        confirms = [e for e in svc.ledger.all_events()
                    if e.event_type == "schedule_confirmed"]
        self.assertEqual(len(confirms), 1)

    def test_same_external_id_same_source_dedup_globally(self):
        ledger = Ledger("")
        ledger.append(case_id="C1", event_type="x", payload={"v": 1},
                      actor=COORD, external_source="gw", external_event_id="E1")
        with self.assertRaises(DuplicateExternalEvent):
            ledger.append(case_id="C2", event_type="x", payload={"v": 2},
                          actor=COORD, external_source="gw", external_event_id="E1")

    def test_business_idempotency_key_scoped_per_case(self):
        svc = make_service()
        cid = open_ready_case(svc)
        svc.schedule_collection(COORD, cid,
                                start_local_iso="2026-09-25T08:00",
                                end_local_iso="2026-09-25T14:00",
                                idem_key="sched-1")
        with self.assertRaises(DuplicateExternalEvent):
            svc.schedule_collection(COORD, cid,
                                    start_local_iso="2026-09-26T08:00",
                                    end_local_iso="2026-09-26T14:00",
                                    idem_key="sched-1")
        # 不同键才是真正的改期（新版本）
        _, v2 = svc.schedule_collection(
            COORD, cid, start_local_iso="2026-09-26T08:00",
            end_local_iso="2026-09-26T14:00", idem_key="sched-2")
        self.assertEqual(v2, 2)

    def test_hash_chain_links_and_genesis(self):
        svc = make_service()
        open_ready_case(svc)
        events = svc.ledger.all_events()
        self.assertEqual(events[0].prev_hash, GENESIS_HASH)
        for prev, cur in zip(events, events[1:]):
            self.assertEqual(cur.prev_hash, prev.hash)
        self.assertTrue(svc.ledger.verify_chain()["ok"])

    def test_tampering_is_detected_after_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            svc = make_service(path=path)
            open_ready_case(svc)
            # 直接篡改台账文件中的一处内容（模拟绕过系统改历史）：
            # 把该事件时间戳年份 2026 改成 3026，JSON 仍合法但内容哈希必变。
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertIn("2026", lines[2])
            lines[2] = lines[2].replace("2026", "3026", 1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)

            reloaded = Ledger(path)
            verdict = reloaded.verify_chain()
            self.assertFalse(verdict["ok"])
            self.assertEqual(verdict["broken_at_seq"], 3)

    def test_persistence_roundtrip_preserves_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            svc1 = make_service(path=path)
            cid = open_ready_case(svc1, "CASE-PERSIST")
            svc1.schedule_collection(COORD, cid,
                                     start_local_iso="2026-09-25T08:00",
                                     end_local_iso="2026-09-25T14:00")
            svc2 = make_service(path=path)
            snap = svc2.get_case(cid)
            self.assertEqual(snap.status.value, "scheduled")
            self.assertEqual(snap.consent_state.value, "granted")
            self.assertEqual(len(snap.schedules), 1)
            self.assertTrue(svc2.ledger.verify_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
