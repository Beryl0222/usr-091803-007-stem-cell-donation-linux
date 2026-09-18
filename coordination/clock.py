"""时间与窗口：各中心按所在地时间解释窗口。

系统内部一律以 UTC 存储绝对时刻；展示与排期时按参与方所在地时区换算。
窗口（采集窗口、运输时限、同意有效区间等）以"当地挂钟时间 + 时区"表达，
换算为明确的 UTC 绝对区间，避免跨中心因夏令时/经度差产生歧义。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

# 常见参与方中心时区。IANA 标识，避免"北京时间"这类非标准缩写。
KNOWN_TIMEZONES = {
    "Asia/Shanghai",   # 东部中心（中华骨髓库总部及多数分库）
    "Asia/Urumqi",     # 新疆中心（官方用北京时间，但当地作息晚约 2 小时）
    "Asia/Kashgar",
}


def now_utc():
    """当前 UTC 时刻。测试可通过 Clock 注入。"""
    return datetime.now(tz=UTC)


def parse_tz(tz_name):
    """解析 IANA 时区名，非法时抛出 ValueError。"""
    if not isinstance(tz_name, str) or not tz_name:
        raise ValueError("缺少时区标识")
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError
        raise ValueError(f"不支持的时区: {tz_name}") from exc


def to_instant(local_iso, tz_name):
    """把"当地挂钟时间"解释为 UTC 绝对时刻。

    local_iso 可带偏移（此时以其自身为准，tz_name 仅用于校验归属），
    也可为 naive（按 tz_name 定位）。
    """
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=parse_tz(tz_name))
    return dt.astimezone(UTC)


def format_local(instant, tz_name):
    """把绝对时刻格式化为某中心的当地时间。"""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(parse_tz(tz_name)).isoformat()


@dataclass(frozen=True)
class Window:
    """一个半开时间区间 [start, end)，内部以 UTC 绝对时刻表达。"""

    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("窗口边界必须带时区")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        if end <= start:
            raise ValueError("窗口结束必须晚于开始")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def from_local(cls, start_local_iso, end_local_iso, tz_name):
        """各中心用自己的当地挂钟时间声明窗口。"""
        return cls(to_instant(start_local_iso, tz_name),
                   to_instant(end_local_iso, tz_name))

    def contains(self, instant):
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        instant = instant.astimezone(UTC)
        return self.start <= instant < self.end

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end

    def view(self, tz_name):
        """以某中心所在地时间呈现窗口（含 UTC 对照，便于跨中心核对）。"""
        return {
            "start_local": format_local(self.start, tz_name),
            "end_local": format_local(self.end, tz_name),
            "timezone": tz_name,
            "start_utc": self.start.astimezone(UTC).isoformat(),
            "end_utc": self.end.astimezone(UTC).isoformat(),
        }

    def duration_minutes(self):
        return int((self.end - self.start) / timedelta(minutes=1))
