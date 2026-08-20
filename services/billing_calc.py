"""計價純函式。

刻意不碰 DB 也不 import models：計價規則是最需要被測試覆蓋的部分，
把它與資料存取隔開才能用最便宜的測試涵蓋所有邊界。
"""
from datetime import datetime, timedelta
from typing import Optional

# 配額門檻（spec §5）：達 90% 警告，達 100% 標記停用。
# 注意：只做標記與顯示，不做實際阻擋——真要停掉拍照/上傳需 Camera Backend 配合。
WARN_THRESHOLD = 0.9
SUSPEND_THRESHOLD = 1.0

# DB 存 naive UTC，但帳務的「哪一天/哪一月」語意是台北時間。
TAIPEI_OFFSET = timedelta(hours=8)


def invoice_total(monthly_fees: list[int]) -> int:
    """發票總額 = 各 active 訂閱的月費加總。"""
    return sum(monthly_fees)


def quota_pct(used: Optional[float], total: Optional[float]) -> int:
    """用量百分比，夾在 0-100。上限為 0 或缺值時回 0（不是除以零）。"""
    if not total or total <= 0:
        return 0
    pct = round((used or 0) / total * 100)
    return max(0, min(100, pct))


def quota_state(used: Optional[float], total: Optional[float]) -> str:
    """配額狀態：ok / warned / suspended。上限為 0 或缺值代表不限量，一律 ok。"""
    if not total or total <= 0:
        return "ok"
    ratio = (used or 0) / total
    if ratio >= SUSPEND_THRESHOLD:
        return "suspended"
    if ratio >= WARN_THRESHOLD:
        return "warned"
    return "ok"


def period_of(dt: datetime) -> str:
    """naive UTC 時間點屬於哪個帳務期別（台北時區的 YYYY-MM）。"""
    return (dt + TAIPEI_OFFSET).strftime("%Y-%m")


def next_period(period: str) -> str:
    """下一個期別，處理跨年。"""
    year, month = (int(x) for x in period.split("-"))
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def period_bounds_utc(period: str) -> tuple[datetime, datetime]:
    """期別（YYYY-MM）對應的 naive UTC 左閉右開區間 [start, end)。

    語意是台北時間該月 1 日 00:00 到下月 1 日 00:00；轉成 naive UTC 存取，
    須扣掉 TAIPEI_OFFSET（台北時間 - 8 小時 = UTC）。
    """
    year, month = (int(x) for x in period.split("-"))
    start_taipei = datetime(year, month, 1)
    end_taipei = datetime(*(int(x) for x in next_period(period).split("-")), 1)
    return start_taipei - TAIPEI_OFFSET, end_taipei - TAIPEI_OFFSET
