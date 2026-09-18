"""通知重发与送达记录。"""

import unittest

from coord import states as S
from coord.errors import RuleViolation
from coord.testsupport import Driver, build_app


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)

    def test_resend_appends_attempt_and_keeps_history(self):
        self.d.to_matched()
        note = next(n for n in self.app.store.notifications.values()
                    if n.recipient_person_id == "u_coord" and not n.suppressed)
        self.assertEqual(len(note.sends), 1)
        self.app.workflow.notifier.resend(note.id)
        self.app.workflow.notifier.resend(note.id)
        self.assertEqual(len(note.sends), 3)
        # 每次尝试都留痕
        self.assertEqual([s["attempt"] for s in note.sends], [1, 2, 3])

    def test_suppressed_notification_cannot_be_sent(self):
        self.d.open_search()
        self.d.to_matched()
        suppressed = next(n for n in self.app.store.notifications.values()
                          if n.suppressed)
        with self.assertRaises(RuleViolation):
            self.app.workflow.notifier.resend(suppressed.id)

    def test_delivery_receipt_marks_delivered_once(self):
        self.d.to_in_transit()
        note = next(n for n in self.app.store.notifications.values()
                    if n.template == "handover_pickup")
        self.assertIsNone(note.delivered_at_utc)
        self.d.go(S.EV_DELIVERY_RECEIPT,
                  {"notification_id": note.id, "channel": "sms"}, ext="R1")
        first = note.delivered_at_utc
        self.assertIsNotNone(first)
        # 重发后再回执：送达时间不被覆盖
        self.app.workflow.notifier.resend(note.id)
        self.d.go(S.EV_DELIVERY_RECEIPT,
                  {"notification_id": note.id, "channel": "sms"}, ext="R2")
        self.assertEqual(note.delivered_at_utc, first)


if __name__ == "__main__":
    unittest.main()
