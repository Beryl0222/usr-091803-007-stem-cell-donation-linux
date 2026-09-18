"""时区解释：内部绝对时刻唯一，窗口按各中心所在地时间解释。"""

import unittest
from datetime import timezone

from coord.clock import (
    Window, as_utc, interpret_local, iso, local_view, parse_instant,
)


class ClockTests(unittest.TestCase):
    def test_naive_local_interpreted_by_org_zone(self):
        # 北京 09:00 与乌鲁木齐 09:00 是不同的绝对时刻（尽管行政同属 UTC+8，
        # 库按 IANA 数据解释；这里用喀什/乌鲁木齐偏移验证机制）
        utc = interpret_local("2026-09-25T09:00", "Asia/Shanghai")
        self.assertEqual(utc.hour, 1)
        view = local_view(utc, "Asia/Shanghai")
        self.assertEqual(view["time"], "09:00")
        self.assertEqual(view["utc_offset_minutes"], 480)

    def test_same_instant_different_local_walls(self):
        instant = parse_instant("2026-09-25T02:30:00Z")
        bj = local_view(instant, "Asia/Shanghai")
        sh = local_view(instant, "Asia/Shanghai")
        self.assertEqual(bj["time"], sh["time"])  # 采集与受者均在上海时区
        self.assertEqual(bj["time"], "10:30")

    def test_window_carries_interpreting_zone(self):
        w = Window(parse_instant("2026-09-25T01:00:00Z"),
                   parse_instant("2026-09-25T06:00:00Z"), "Asia/Shanghai")
        view = w.to_view()
        self.assertEqual(view["start_local"]["time"], "09:00")
        self.assertEqual(view["end_local"]["time"], "14:00")
        self.assertEqual(iso(w.start_utc), "2026-09-25T01:00:00Z")

    def test_naive_utc_iso(self):
        dt = parse_instant("2026-09-25T01:00:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertTrue(iso(dt).endswith("Z"))

    def test_offset_preserves_instant(self):
        a = parse_instant("2026-09-25T09:00:00+08:00")
        b = parse_instant("2026-09-25T01:00:00Z")
        self.assertEqual(as_utc(a), as_utc(b))


if __name__ == "__main__":
    unittest.main()
