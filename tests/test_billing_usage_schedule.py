"""排程純函式：算到下一個台北 03:00 還有多久。

排程迴圈本身（無限迴圈 + 長時間 sleep）不好測，所以把「算下一個觸發時間」
抽成 seconds_until_next_collect 純函式獨立驗證，重點在跨日、跨時區邊界。
"""
from datetime import datetime

from services.billing_usage import seconds_until_next_collect, COLLECT_HOUR_TAIPEI


def test_台北凌晨三點前_今天就是目標():
    # 台北 2026-08-21 02:00 = UTC 2026-08-20 18:00，距台北 03:00 還有 1 小時
    now_utc = datetime(2026, 8, 20, 18, 0, 0)
    assert seconds_until_next_collect(now_utc) == 3600.0


def test_台北凌晨三點整_視為已過_算到明天():
    # 台北 2026-08-21 03:00 整 = UTC 2026-08-20 19:00，target<=taipei 觸發跨日
    now_utc = datetime(2026, 8, 20, 19, 0, 0)
    assert seconds_until_next_collect(now_utc) == 24 * 3600.0


def test_台北三點後_算到明天三點():
    # 台北 2026-08-21 10:00 = UTC 2026-08-21 02:00，距明天台北 03:00 還有 17 小時
    now_utc = datetime(2026, 8, 21, 2, 0, 0)
    assert seconds_until_next_collect(now_utc) == 17 * 3600.0


def test_跨月邊界():
    # 台北 2026-08-31 23:00 = UTC 2026-08-31 15:00，距 9/1 台北 03:00 還有 4 小時
    now_utc = datetime(2026, 8, 31, 15, 0, 0)
    assert seconds_until_next_collect(now_utc) == 4 * 3600.0


def test_目標小時是台北三點():
    assert COLLECT_HOUR_TAIPEI == 3
