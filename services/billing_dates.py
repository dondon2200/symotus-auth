"""計費 v2 的日期規則（純函式，不碰 DB）。

規格：symotus-frontend/docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md §5

- 全部以台北日期計算。
- 一律「從起始日加 n 個月、日 = min(錨定日, 當月天數)」，不從上一期往後疊加，
  否則 1/31 → 2/28 → 3/28 會一路往前漂。
- 截止日 = 帳單日 + 1 個月（同一錨定日）。注意是從錨定日推，不是從被夾過的帳單日推：
  錨定 29 的 2029-02-28 帳單，截止日是 03-29。
"""
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Optional

TAIPEI = timezone(timedelta(hours=8))

TERMS = ("monthly", "annual")
CYCLES = ("monthly", "yearly")


def taipei_today(now: Optional[datetime] = None) -> date:
    """台北今天。now 為 naive 時視為 UTC（本專案時間戳慣例）。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TAIPEI).date()


def taipei_date_to_utc_naive(d: date) -> datetime:
    """台北某日 00:00 對應的 naive UTC 時間戳（ended_at 這類「某日起」的時間點用）。"""
    return datetime(d.year, d.month, d.day, tzinfo=TAIPEI).astimezone(timezone.utc).replace(tzinfo=None)


def add_months(start: date, n: int, anchor: int) -> date:
    y, m = divmod(start.month - 1 + n, 12)
    y += start.year
    m += 1
    return date(y, m, min(anchor, calendar.monthrange(y, m)[1]))


def cycle_months(cycle: str) -> int:
    if cycle == "monthly":
        return 1
    if cycle == "yearly":
        return 12
    raise ValueError(f"unknown cycle {cycle!r}")


def valid_combo(term: str, cycle: str) -> bool:
    """月約年繳不允許（spec D2）：月約可隨時不續約，預繳一年中途不續約就得退款。"""
    return term in TERMS and cycle in CYCLES and not (term == "monthly" and cycle == "yearly")


def bill_date(start: date, k: int, cycle: str, anchor: int) -> date:
    """第 k 期（0 起算）的帳單日。"""
    return add_months(start, k * cycle_months(cycle), anchor)


def due_date_of(bill_day: date, anchor: int) -> date:
    return add_months(bill_day, 1, anchor)


def term_end_of(term_start: date, anchor: int) -> date:
    """年約合約到期日（= 下一個合約期的起點）。"""
    return add_months(term_start, 12, anchor)


def is_overdue(status: str, due: date, today: date) -> bool:
    """截止日當天整天都不算逾期。"""
    return status == "unpaid" and today > due


def overdue_days(due: date, today: date) -> int:
    return max(0, (today - due).days)


def next_bill_date_after(start: date, cycle: str, anchor: int, today: date) -> date:
    """嚴格晚於 today 的第一個帳單日（「下次帳單日」顯示、月約不續約的生效日用）。"""
    k = 0
    while True:
        d = bill_date(start, k, cycle, anchor)
        if d > today:
            return d
        k += 1
