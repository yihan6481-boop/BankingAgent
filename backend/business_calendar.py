"""Business date arithmetic, independent from language parsing."""
from datetime import date

def month_before(month):
    d = date.fromisoformat(month + '-01')
    if d.year == 1 and d.month == 1:
        raise ValueError('no previous supported month')
    return f'{d.year - (d.month == 1):04d}-{12 if d.month == 1 else d.month-1:02d}'


