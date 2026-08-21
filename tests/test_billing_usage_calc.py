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


def test_優先用實際影片長度():
    # Spark 的 video_duration_secs 是產出影片的真實長度。
    # image_count/fps 是「來源照片張數/fps」，因為 Spark 會抽樣，
    # 實測會比真實長度大 2～6 倍（見計畫文件的查證表）。
    assert job_duration_secs(125.43, 7194, 30) == 125


def test_無條件捨去不四捨五入():
    assert job_duration_secs(125.99, None, None) == 125
    assert job_duration_secs(0.9, None, None) == 0


def test_舊資料沒有影片長度時退回張數除以fps():
    # video_duration_secs 是後加欄位，既有資料為 NULL。
    # 這個 fallback 是高估值，但總比整批用量消失好。
    assert job_duration_secs(None, 900, 30) == 30


def test_影片長度為零是有效值不是缺值():
    # 0 秒的影片（例如來源只有 1 張照片）是合法結果，
    # 不可被當成「沒有值」而退回 image_count/fps。
    assert job_duration_secs(0, 900, 30) == 0


def test_三個都缺時為零而不猜():
    assert job_duration_secs(None, None, 30) == 0
    assert job_duration_secs(None, 900, None) == 0
    assert job_duration_secs(None, 900, 0) == 0


def test_位元組換算GB():
    assert bytes_to_gb(1024 ** 3) == 1.0
    assert bytes_to_gb(0) == 0.0
    assert bytes_to_gb(int(1.5 * 1024 ** 3)) == 1.5


def test_GB保留三位小數():
    # 一天幾百 MB 的相機，四捨五入到整數會全部變 0
    assert bytes_to_gb(1024 ** 2 * 512) == 0.5
    assert bytes_to_gb(1024 ** 2) == 0.001
