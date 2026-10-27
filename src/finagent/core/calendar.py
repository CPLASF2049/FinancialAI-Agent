"""交易日历工具。

中国交易所的休市日用一个显式的小表近似。真实日历时，provider 可以通过
``trade_calendar`` 覆盖 :func:`is_trading_day` 的结果；本模块只需要足够好地服务
日期默认值、指标预热窗口和预警 worker 的交易时段判断。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

#: A 股固定休市的公历假日（月, 日）：元旦、劳动节、国庆，
#: 另加近几年的春节休市窗口。
_FIXED_CLOSURES: frozenset[tuple[int, int]] = frozenset(
    {
        (1, 1),
        (1, 2),
        (1, 3),
        (5, 1),
        (5, 2),
        (5, 3),
        (10, 1),
        (10, 2),
        (10, 3),
        (10, 4),
        (10, 5),
        (10, 6),
        (10, 7),
    }
)

#: 每年春节的完整休市日（年, 月, 日）。
_SPRING_FESTIVAL: frozenset[tuple[int, int, int]] = frozenset(
    {
        (2023, 1, 23),
        (2023, 1, 24),
        (2023, 1, 25),
        (2023, 1, 26),
        (2023, 1, 27),
        (2024, 2, 12),
        (2024, 2, 13),
        (2024, 2, 14),
        (2024, 2, 15),
        (2024, 2, 16),
        (2025, 1, 29),
        (2025, 1, 30),
        (2025, 1, 31),
        (2025, 2, 3),
        (2025, 2, 4),
        (2026, 2, 17),
        (2026, 2, 18),
        (2026, 2, 19),
        (2026, 2, 20),
    }
)

_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def today() -> date:
    return datetime.now().date()


def format_date(value: date | datetime | str) -> str:
    """把各种日期样式统一渲染成 ``YYYYMMDD``。"""

    if isinstance(value, str):
        cleaned = value.strip().replace("-", "").replace("/", "")
        if len(cleaned) == 8 and cleaned.isdigit():
            return cleaned
        raise ValueError(f"无法识别的日期字符串：{value!r}")
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    return value.strftime("%Y%m%d")


def parse_date(value: str | date | datetime | None, default: date | None = None) -> date:
    if value is None:
        return default or today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(format_date(value), "%Y%m%d").date()


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in _FIXED_CLOSURES:
        return False
    if (day.year, day.month, day.day) in _SPRING_FESTIVAL:
        return False
    return True


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def latest_trading_day(reference: date | None = None) -> date:
    """``reference`` 当天或之前最近的交易日。"""

    cursor = reference or today()
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def is_trading_now(now: datetime | None = None) -> bool:
    """A 股连续竞价时段（09:30-11:30、13:00-15:00，北京时间）。"""

    moment = now or datetime.now()
    if not is_trading_day(moment.date()):
        return False
    minutes = moment.hour * 60 + moment.minute
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def default_window(days: int = 365, end: date | None = None) -> tuple[str, str]:
    """返回 ``(start, end)``（``YYYYMMDD``），覆盖约 ``days`` 个自然日。"""

    end_date = end or latest_trading_day()
    start_date = end_date - timedelta(days=days)
    return format_date(start_date), format_date(end_date)


def warmup_start(start_date: str, days: int = 400) -> str:
    """把窗口向前延伸，让滚动指标有足够历史。"""

    anchor = parse_date(start_date)
    return format_date(anchor - timedelta(days=days))


def quarter_end_before(day: date) -> date:
    """``day`` 当天或之前最近的季度末（用于财报期）。"""

    candidates: list[date] = []
    for year in (day.year, day.year - 1):
        for month, dom in _QUARTER_ENDS:
            candidate = date(year, month, dom)
            if candidate <= day:
                candidates.append(candidate)
    return max(candidates) if candidates else date(day.year - 1, 12, 31)
