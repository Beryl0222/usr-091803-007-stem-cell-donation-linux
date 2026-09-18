"""时间服务：内部一律存 UTC，窗口按各中心所在地时区解释。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


class Clock:
    """可注入固定时间，便于测试与演示复现。"""

    def __init__(self, fixed: datetime | None = None):
        self.fixed = fixed.astimezone(UTC) if fixed else None

    def now(self) -> datetime:
        if self.fixed is not None:
            return self.fixed
        return datetime.now(UTC)

    def advance(self, **delta) -> None:
        if self.fixed is None:
            self.fixed = datetime.now(UTC)
        self.fixed = self.fixed + timedelta(**delta)


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # zoneinfo 对未知时区抛 ZoneInfoNotFoundError
        raise ValueError(f"未知时区: {tz_name}") from exc


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("时间戳必须带时区信息")
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """UTC ISO8601，尾缀 Z。"""
    out = as_utc(dt).isoformat()
    return out.replace("+00:00", "Z")


def parse_instant(value: str, default_tz: str | None = None) -> datetime:
    """解析带偏移的时间串；裸时间用 default_tz 解释。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_zone(default_tz or "UTC"))
    return as_utc(dt)


def interpret_local(local_iso: str, tz_name: str) -> datetime:
    """把“当地墙上时间”按该机构时区解释为 UTC 绝对时刻。"""
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is not None:
        raise ValueError("interpret_local 只接受不带偏移的当地时间")
    return dt.replace(tzinfo=get_zone(tz_name)).astimezone(UTC)


def local_view(dt: datetime, tz_name: str) -> dict:
    """供界面展示：同一绝对时刻在某中心所在地的本地解释。"""
    zoned = as_utc(dt).astimezone(get_zone(tz_name))
    offset = zoned.utcoffset()
    return {
        "iso": zoned.isoformat(),
        "date": zoned.date().isoformat(),
        "time": zoned.strftime("%H:%M"),
        "tz": tz_name,
        "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        "abbrev": zoned.strftime("%Z"),
    }


@dataclass(frozen=True)
class Window:
    """采集/送达温度或作业窗口：UTC 绝对起止 + 解释它的时区。"""

    start_utc: datetime
    end_utc: datetime
    tz: str

    def to_view(self, tz_name: str | None = None) -> dict:
        tz_name = tz_name or self.tz
        return {
            "start_utc": iso(self.start_utc),
            "end_utc": iso(self.end_utc),
            "start_local": local_view(self.start_utc, tz_name),
            "end_local": local_view(self.end_utc, tz_name),
        }
