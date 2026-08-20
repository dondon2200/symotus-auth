"""計價純函式。

刻意不碰 DB 也不 import models：計價規則是最需要被測試覆蓋的部分，
把它與資料存取隔開才能用最便宜的測試涵蓋所有邊界。
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
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


# 客戶的付款方式與分潤類型；值存 DB，顯示文字由前端負責。
PAYMENT_METHODS = ("monthly_transfer", "credit_card")
COMMISSION_TYPES = ("percent", "fixed")


def effective_monthly_fee(plan_fee: int, custom_fee: Optional[int]) -> int:
    """客戶的實際月費。自訂月費設了就覆蓋方案月費。

    注意用 `is None` 判斷而非 falsy：0 是有效的自訂月費（談成免費的客戶），
    用 `custom_fee or plan_fee` 會讓 0 被當成未設定而錯收全額。
    """
    return plan_fee if custom_fee is None else custom_fee


# 分潤的百分比以「萬分比（basis points）」儲存：1250 bps = 12.5%，精度 0.01%。
# 為什麼不用浮點數存百分比：分潤金額是要真的付出去的錢，二進位浮點數會有分位誤差。
# 整數 bps 讓輸入→儲存→計算→顯示全程沒有浮點數參與。
BPS_PER_PERCENT = 100
MAX_COMMISSION_BPS = 10000  # 100%


def commission_amount(base: int, commission_type: Optional[str], commission_value: Optional[int]) -> int:
    """分潤金額（TWD 整數）。

    分潤不進發票（設計決定：另列應付），這裡只負責算出「要付給經銷商多少」。
    percent 型別的 commission_value 是 bps（萬分比）；用 Decimal ROUND_HALF_UP
    四捨五入到元——Python 內建 round() 是 banker's rounding，.5 會一半進一半捨，
    帳務上不可預期。
    """
    if not commission_type or commission_value is None:
        return 0
    if commission_type == "fixed":
        return int(commission_value)
    if commission_type == "percent":
        return int(
            (Decimal(base) * Decimal(commission_value) / Decimal(10000))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    return 0  # 未知類型不猜


def commission_display(commission_type: Optional[str], commission_value: Optional[int]) -> str:
    """人看的分潤條件字串。

    bps 對人類不直觀（1250 容易被誤讀成 1250%），報表與稽核日誌一律帶這個字串，
    避免有人照著 commission_value 的數字做決策。
    """
    if not commission_type or commission_value is None:
        return ""
    if commission_type == "fixed":
        return f"NT$ {commission_value:,}"
    if commission_type == "percent":
        pct = Decimal(commission_value) / Decimal(BPS_PER_PERCENT)
        # normalize() 去掉尾端多餘的零（15.00 → 15），但要避免用到科學記號
        text = format(pct.normalize(), "f")
        return f"{text}%"
    return ""
