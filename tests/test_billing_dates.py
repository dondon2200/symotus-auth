"""計費 v2 日期規則（spec §5）。§5.2 的範例表原封不動列在這裡，前端 billingDates 共用同一組。"""
from datetime import date, datetime

import pytest

from services.billing_dates import (
    add_months, bill_date, due_date_of, is_overdue, next_bill_date_after, overdue_days,
    taipei_date_to_utc_naive, taipei_today, term_end_of, valid_combo,
)

D = date


@pytest.mark.parametrize("start,cycle,bills,dues", [
    (D(2026, 10, 7), "monthly",
     [D(2026, 10, 7), D(2026, 11, 7), D(2026, 12, 7)],
     [D(2026, 11, 7), D(2026, 12, 7), D(2027, 1, 7)]),
    (D(2026, 1, 31), "monthly",
     [D(2026, 1, 31), D(2026, 2, 28), D(2026, 3, 31), D(2026, 4, 30)],
     [D(2026, 2, 28), D(2026, 3, 31), D(2026, 4, 30), D(2026, 5, 31)]),
    (D(2026, 10, 7), "yearly",
     [D(2026, 10, 7), D(2027, 10, 7)],
     [D(2026, 11, 7), D(2027, 11, 7)]),
    (D(2028, 2, 29), "yearly",
     [D(2028, 2, 29), D(2029, 2, 28), D(2030, 2, 28), D(2031, 2, 28), D(2032, 2, 29)],
     [D(2028, 3, 29), D(2029, 3, 29), D(2030, 3, 29), D(2031, 3, 29), D(2032, 3, 29)]),
])
def test_spec_範例表(start, cycle, bills, dues):
    anchor = start.day
    got = [bill_date(start, k, cycle, anchor) for k in range(len(bills))]
    assert got == bills
    assert [due_date_of(b, anchor) for b in got] == dues


def test_不從上一期疊加_月底不漂移():
    # 疊加會變成 1/31 → 2/28 → 3/28；從起始日算要回到 3/31
    assert bill_date(D(2026, 1, 31), 2, "monthly", 31) == D(2026, 3, 31)


def test_跨年():
    assert add_months(D(2026, 12, 15), 1, 15) == D(2027, 1, 15)
    assert add_months(D(2026, 11, 30), 3, 30) == D(2027, 2, 28)


def test_錨定29到31():
    assert add_months(D(2027, 1, 29), 1, 29) == D(2027, 2, 28)
    assert add_months(D(2028, 1, 29), 1, 29) == D(2028, 2, 29)
    assert add_months(D(2026, 3, 30), 1, 30) == D(2026, 4, 30)
    assert add_months(D(2026, 5, 31), 1, 31) == D(2026, 6, 30)


def test_年約到期日():
    assert term_end_of(D(2026, 10, 7), 7) == D(2027, 10, 7)
    assert term_end_of(D(2029, 2, 28), 29) == D(2030, 2, 28)


def test_月約年繳不允許():
    assert valid_combo("monthly", "monthly")
    assert valid_combo("annual", "monthly")
    assert valid_combo("annual", "yearly")
    assert not valid_combo("monthly", "yearly")
    assert not valid_combo("weekly", "monthly")


def test_截止日當天不算逾期_隔天才算():
    due = D(2026, 11, 7)
    assert not is_overdue("unpaid", due, D(2026, 11, 7))
    assert is_overdue("unpaid", due, D(2026, 11, 8))
    assert not is_overdue("paid", due, D(2026, 12, 1))
    assert overdue_days(due, D(2026, 11, 19)) == 12
    assert overdue_days(due, D(2026, 11, 1)) == 0


def test_台北日界_UTC16點():
    # 台北 00:00 = UTC 前一天 16:00
    assert taipei_today(datetime(2026, 11, 7, 15, 59)) == D(2026, 11, 7)
    assert taipei_today(datetime(2026, 11, 7, 16, 0)) == D(2026, 11, 8)
    assert taipei_date_to_utc_naive(D(2026, 11, 8)) == datetime(2026, 11, 7, 16, 0)


def test_下次帳單日():
    assert next_bill_date_after(D(2026, 10, 7), "monthly", 7, D(2026, 10, 7)) == D(2026, 11, 7)
    assert next_bill_date_after(D(2026, 10, 7), "monthly", 7, D(2026, 11, 6)) == D(2026, 11, 7)
    assert next_bill_date_after(D(2026, 10, 7), "yearly", 7, D(2026, 11, 6)) == D(2027, 10, 7)
