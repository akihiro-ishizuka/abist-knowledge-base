"""営業時間の窓と、その中での経過時間の積算(設計 §7.1)。

この窓は2つの用途で共有する。ひとつはリマインドの積算 — 「単純な経過4時間」では
なく「営業時間を4時間消費した時点」で発火させる。もうひとつは AI が応答してよい
時間帯そのもので、窓の外ではティックが何もしない。

同じ「営業時間」を2箇所で別々に定義すると必ず食い違うため、定義はここ1箇所に置く。

祝日は考慮しない(PoC)。考慮する場合はここへ休日判定を足すだけで済むよう、
日付単位の判定を `_is_business_day` に閉じてある。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
#: 既定の運用時間。営業日の 8:30〜17:30。
OPENING = time(8, 30)
CLOSING = time(17, 30)


def _is_business_day(day: date) -> bool:
    """月〜金を営業日とみなす。祝日は考慮しない(PoC)。"""
    return day.weekday() < 5


def is_within_business_hours(
    moment: datetime, *, opening: time = OPENING, closing: time = CLOSING
) -> bool:
    """`moment` が営業日の運用時間内なら True。

    終了時刻ちょうどは含めない(17:30 は時間外)。窓の外では応答も催促もしない。
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    local = moment.astimezone(JST)
    if not _is_business_day(local.date()):
        return False
    return opening <= local.time() < closing


def elapsed_business_hours(
    start: datetime, now: datetime, *, opening: time = OPENING, closing: time = CLOSING
) -> float:
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
            window_open = datetime.combine(day, opening, tzinfo=JST)
            window_close = datetime.combine(day, closing, tzinfo=JST)
            overlap_start = max(window_open, start_jst)
            overlap_end = min(window_close, now_jst)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        day += timedelta(days=1)

    return total.total_seconds() / 3600.0
