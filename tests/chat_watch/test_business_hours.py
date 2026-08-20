"""営業時間内経過時間の積算(設計 §7.1)。

単純な経過時間ではなく、月〜金 09:00-18:00 JST の中だけを積算する。祝日は
考慮しない(PoC)。金曜夕方の質問が月曜に発火する例を正本とする。
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from abist_kb.application.chat_watch.business_hours import (
    CLOSING,
    OPENING,
    elapsed_business_hours,
    is_within_business_hours,
)

JST = ZoneInfo("Asia/Tokyo")


def _jst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=JST)


def test_within_one_business_day() -> None:
    # 木 10:00 -> 木 14:00
    assert elapsed_business_hours(_jst(8, 20, 10), _jst(8, 20, 14)) == pytest.approx(4.0)


def test_excludes_time_before_opening() -> None:
    # 木 07:00 -> 木 11:00 のうち算入は 08:30-11:00 の2.5時間
    assert elapsed_business_hours(_jst(8, 20, 7), _jst(8, 20, 11)) == pytest.approx(2.5)


def test_excludes_time_after_closing() -> None:
    # 木 17:00 -> 木 20:00 のうち算入は 17:00-17:30 の0.5時間
    assert elapsed_business_hours(_jst(8, 20, 17), _jst(8, 20, 20)) == pytest.approx(0.5)


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


def test_default_window_is_0830_to_1730() -> None:
    """運用時間は営業日の 8:30〜17:30。"""
    assert time(8, 30) == OPENING
    assert time(17, 30) == CLOSING


def test_friday_evening_example_still_lands_on_monday_noon() -> None:
    """設計 §7.1 の正本の例は 8:30〜17:30 でも結論が変わらない。

    金 17:00→17:30 で 0.5h、月 8:30→12:00 で 3.5h、合計 4.0h。
    """
    assert elapsed_business_hours(_jst(8, 21, 17), _jst(8, 24, 12)) == pytest.approx(4.0)
    assert elapsed_business_hours(_jst(8, 21, 17), _jst(8, 24, 11, 59)) < 4.0


def test_window_can_be_overridden() -> None:
    """窓は設定で差し替えられる(祝日運用や時短日のため)。"""
    got = elapsed_business_hours(
        _jst(8, 20, 8), _jst(8, 20, 18), opening=time(10, 0), closing=time(15, 0)
    )

    assert got == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_jst(8, 20, 8, 29), False),  # 木 開始前
        (_jst(8, 20, 8, 30), True),  # 木 開始ちょうど
        (_jst(8, 20, 12, 0), True),  # 木 日中
        (_jst(8, 20, 17, 29), True),  # 木 終了直前
        (_jst(8, 20, 17, 30), False),  # 木 終了ちょうどは対象外
        (_jst(8, 22, 12, 0), False),  # 土
        (_jst(8, 23, 12, 0), False),  # 日
    ],
)
def test_is_within_business_hours(moment: datetime, expected: bool) -> None:
    assert is_within_business_hours(moment) is expected
