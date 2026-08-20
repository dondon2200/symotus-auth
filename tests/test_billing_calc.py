"""計價純函式測試。不碰 DB，所以不需要任何 fixture。"""
from datetime import datetime

from services.billing_calc import (
    invoice_total, quota_pct, quota_state, period_of, next_period, period_bounds_utc,
)


def test_發票總額為各訂閱月費加總():
    assert invoice_total([1200, 800, 500]) == 2500


def test_沒有訂閱時總額為零():
    assert invoice_total([]) == 0


def test_配額百分比():
    assert quota_pct(50, 100) == 50
    assert quota_pct(0, 100) == 0


def test_配額上限為零或缺值時回零而非除以零():
    assert quota_pct(50, 0) == 0
    assert quota_pct(50, None) == 0


def test_配額百分比夾在零到一百之間():
    assert quota_pct(150, 100) == 100
    assert quota_pct(-5, 100) == 0


def test_配額狀態的三段門檻():
    assert quota_state(89, 100) == "ok"
    assert quota_state(90, 100) == "warned"     # 剛好 90% 進入警告
    assert quota_state(99, 100) == "warned"
    assert quota_state(100, 100) == "suspended"  # 剛好 100% 進入停用標記
    assert quota_state(120, 100) == "suspended"


def test_配額上限為零時狀態為ok():
    # 沒有設上限的方案不該被誤判為超量
    assert quota_state(999, 0) == "ok"


def test_期別以台北時區判定():
    # naive UTC 2026-07-31 17:00 = 台北 2026-08-01 01:00 → 屬於 2026-08
    assert period_of(datetime(2026, 7, 31, 17, 0)) == "2026-08"
    # naive UTC 2026-07-31 15:59 = 台北 2026-07-31 23:59 → 仍屬 2026-07
    assert period_of(datetime(2026, 7, 31, 15, 59)) == "2026-07"


def test_下一期別():
    assert next_period("2026-08") == "2026-09"
    assert next_period("2026-12") == "2027-01"  # 跨年


def test_期別邊界為台北月初到下月初的naiveUTC():
    # 台北 2026-08-01 00:00 = naive UTC 2026-07-31 16:00
    # 台北 2026-09-01 00:00 = naive UTC 2026-08-31 16:00
    start, end = period_bounds_utc("2026-08")
    assert start == datetime(2026, 7, 31, 16, 0)
    assert end == datetime(2026, 8, 31, 16, 0)


def test_期別邊界跨年():
    # 台北 2026-12-01 00:00 = naive UTC 2026-11-30 16:00
    # 台北 2027-01-01 00:00 = naive UTC 2026-12-31 16:00
    start, end = period_bounds_utc("2026-12")
    assert start == datetime(2026, 11, 30, 16, 0)
    assert end == datetime(2026, 12, 31, 16, 0)


def test_期別邊界起訖恰為台北月初零時():
    start, end = period_bounds_utc("2026-08")
    # 邊界轉回台北時間應恰為月初 00:00
    assert period_of(start) == "2026-08"
    assert period_of(end) == "2026-09"
