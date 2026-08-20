"""営業時間内経過時間の積算(設計 §7.1)。

リマインドは「単純な経過4時間」ではなく「営業時間を4時間消費した時点」で発火
する。金曜17:00の質問は月曜12:00に4時間へ到達する。

祝日は考慮しない(PoC)。考慮する場合はここへ休日判定を足すだけで済むよう、
日付単位の判定を `_is_business_day` に閉じてある。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
OPENING = time(9, 0)
CLOSING = time(18, 0)


def _is_business_day(day: date) -> bool:
    """月〜金を営業日とみなす。祝日は考慮しない(PoC)。"""
    return day.weekday() < 5


def elapsed_business_hours(start: datetime, now: datetime) -> float:
    """`start` から `now` までのうち、営業時間に入る時間数を返す。

    引数は timezone-aware であること。内部で JST へ変換して判定する。
    `now` が `start` より前なら 0.0 を返す。
    """
    if start.tzinfo is None or now.tzinfo is None:
        raise ValueError("start and now must be timezone-aware")

    start_jst = start.astimezone(JST)
    now_jst = now.astimezone(JST)
    if now_jst <= start_jst:
        return 0.0

    total = timedelta()
    day = start_jst.date()
    while day <= now_jst.date():
        if _is_business_day(day):
            window_open = datetime.combine(day, OPENING, tzinfo=JST)
            window_close = datetime.combine(day, CLOSING, tzinfo=JST)
            overlap_start = max(window_open, start_jst)
            overlap_end = min(window_close, now_jst)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        day += timedelta(days=1)

    return total.total_seconds() / 3600.0
