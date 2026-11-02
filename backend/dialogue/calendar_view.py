"""Exact server calendar DTO; no free-text or arbitrary-field exemption."""
from datetime import date, datetime
from typing import Literal
from agent.schemas import StrictModel
from pydantic import model_validator


class CalendarView(StrictModel):
    kind:Literal['server_calendar']
    today:str
    today_weekday:str
    date:str
    weekday:str
    timezone:Literal['Asia/Shanghai']
    as_of:str
    reminder_created:Literal[False]
    notification_deliverable:Literal[False]

    @model_validator(mode='after')
    def dates(self):
        from zoneinfo import ZoneInfo
        stamp=datetime.fromisoformat(self.as_of)
        if stamp.tzinfo is None or stamp.astimezone(ZoneInfo(self.timezone)).date().isoformat()!=self.today:raise ValueError('calendar_date_mismatch')
        for value,label in [(self.today,self.today_weekday),(self.date,self.weekday)]:
            if label!='星期'+'一二三四五六日'[date.fromisoformat(value).weekday()]:raise ValueError('calendar_weekday_mismatch')
        return self
