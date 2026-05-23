from __future__ import annotations

import calendar
from datetime import date, timedelta


def add_months(value: date, months: int = 1) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def initial_next_reset_date(created_date: date, first_day: int) -> date:
    # Example: Jan 22 + 1 month - (3 - 1 days) = Feb 19.
    return add_months(created_date, 1) - timedelta(days=max(first_day - 1, 0))


def apply_monthly_reset(today: date, current_day: int, next_reset: date) -> tuple[int, date, bool]:
    reset_happened = False
    if today >= next_reset:
        current_day = 1
        reset_happened = True
        while today >= next_reset:
            next_reset = add_months(next_reset, 1)
    return current_day, next_reset, reset_happened
