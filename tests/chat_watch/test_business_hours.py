"""営業時間内経過時間の積算(設計 §7.1)。

単純な経過時間ではなく、月〜金 09:00-18:00 JST の中だけを積算する。祝日は
考慮しない(PoC)。金曜夕方の質問が月曜に発火する例を正本とする。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from abist_kb.application.chat_watch.business_hours import elapsed_business_hours

JST = ZoneInfo("Asia/Tokyo")


def _jst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=JST)


def test_within_one_business_day() -> None:
    # 木 10:00 -> 木 14:00
    assert elapsed_business_hours(_jst(8, 20, 10), _jst(8, 20, 14)) == pytest.approx(4.0)


def test_excludes_time_before_opening() -> None:
    # 木 07:00 -> 木 11:00 のうち算入は 09:00-11:00 の2時間
    assert elapsed_business_hours(_jst(8, 20, 7), _jst(8, 20, 11)) == pytest.approx(2.0)


def test_excludes_time_after_closing() -> None:
    # 木 17:00 -> 木 20:00 のうち算入は 17:00-18:00 の1時間
    assert elapsed_business_hours(_jst(8, 20, 17), _jst(8, 20, 20)) == pytest.approx(1.0)


def test_friday_evening_question_reaches_four_hours_on_monday_noon() -> None:
    """設計 §7.1 の正本の例。

    金 17:00 -> 18:00 で1時間、月 09:00 -> 12:00 で3時間、合計4時間。
    2026-08-21 が金曜、2026-08-24 が月曜。
    """
    assert elapsed_business_hours(_jst(8, 21, 17), _jst(8, 24, 12)) == pytest.approx(4.0)
    # 月曜 11:59 ではまだ4時間に満たない
    assert elapsed_business_hours(_jst(8, 21, 17), _jst(8, 24, 11, 59)) < 4.0


def test_weekend_contributes_nothing() -> None:
    # 土 09:00 -> 日 18:00
    assert elapsed_business_hours(_jst(8, 22, 9), _jst(8, 23, 18)) == pytest.approx(0.0)


def test_now_before_start_is_zero() -> None:
    assert elapsed_business_hours(_jst(8, 20, 14), _jst(8, 20, 10)) == pytest.approx(0.0)
