"""用量計算純函式。DB 與檔案系統都不碰，所以最便宜也最該完整覆蓋邊界。"""
from datetime import datetime

from services.billing_usage import (
    taipei_day_bounds_utc, yesterday_taipei, job_duration_secs, bytes_to_gb,
)


def test_台北某日換算成UTC區間():
    # 台北 2026-08-20 00:00 = UTC 2026-08-19 16:00
    start, end = taipei_day_bounds_utc("2026-08-20")
    assert start == datetime(2026, 8, 19, 16, 0)
    assert end == datetime(2026, 8, 20, 16, 0)


def test_台北某日區間跨月():
    start, end = taipei_day_bounds_utc("2026-09-01")
    assert start == datetime(2026, 8, 31, 16, 0)


def test_前一天以台北時區判定():
    # naive UTC 2026-08-19 19:00 = 台北 2026-08-20 03:00 → 前一天是 08-19
    assert yesterday_taipei(datetime(2026, 8, 19, 19, 0)) == "2026-08-19"
    # naive UTC 2026-08-19 15:00 = 台北 2026-08-19 23:00 → 前一天是 08-18
    assert yesterday_taipei(datetime(2026, 8, 19, 15, 0)) == "2026-08-18"


def test_影片秒數為張數除以fps():
    assert job_duration_secs(900, 30) == 30
    assert job_duration_secs(901, 30) == 30  # 無條件捨去，不四捨五入到多收費


def test_缺值的舊資料計為零而非猜測():
    assert job_duration_secs(None, 30) == 0
    assert job_duration_secs(900, None) == 0
    assert job_duration_secs(900, 0) == 0    # fps=0 不能當除數


def test_位元組換算GB():
    assert bytes_to_gb(1024 ** 3) == 1.0
    assert bytes_to_gb(0) == 0.0
    assert bytes_to_gb(int(1.5 * 1024 ** 3)) == 1.5


def test_GB保留三位小數():
    # 一天幾百 MB 的相機，四捨五入到整數會全部變 0
    assert bytes_to_gb(1024 ** 2 * 512) == 0.5
    assert bytes_to_gb(1024 ** 2) == 0.001
