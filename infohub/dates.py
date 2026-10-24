"""Deterministic date/time extraction for Chinese and English chat text (no AI).

find_datetime(text, ref) -> (datetime | None, has_time: bool)
Understands: 今天/明天/后天/大后天/今晚, 周五/星期五/礼拜五, 下周一/本周三, 10月30日/30号,
2026-10-30, 10/30, 10-30, 14:00, 下午3点(半), 3pm, today/tomorrow/tonight, (next) Friday, Oct 30.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

WD_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WD_EN = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MON_EN = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

R_REL_CN = re.compile(r"(大后天|后天|明天|明早|明晚|今天|今晚|今早|今日|明日)")
R_WEEK_CN = re.compile(r"(下下|下|本|这)?\s*(?:周|星期|礼拜)([一二三四五六日天])")
R_YMD = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
R_MD_CN = re.compile(r"(\d{1,2})月(\d{1,2})[日号]?")
R_MD_NUM = re.compile(r"(?<![\d.:/-])(\d{1,2})[/-](\d{1,2})(?![\d/-])")
R_D_CN = re.compile(r"(?<![\d月])(\d{1,2})[号日](?!期)")
R_REL_EN = re.compile(r"\b(today|tonight|tomorrow)\b", re.I)
R_WEEK_EN = re.compile(r"\b(next\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
R_MD_EN = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b|\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
R_TIME = re.compile(r"(上午|早上|中午|下午|晚上|今晚|明晚)?\s*(\d{1,2})[:：](\d{2})")
R_TIME_CN = re.compile(r"(上午|早上|中午|下午|晚上|今晚|明晚)?\s*(\d{1,2})点(半|(\d{1,2})分?)?")
R_TIME_EN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)


def _future(ref: datetime, month: int, day: int) -> datetime | None:
    try:
        d = ref.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None
    if d.date() < (ref - timedelta(days=60)).date():   # "1月5日" said in December means next year
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d


def _date(text: str, ref: datetime) -> datetime | None:
    base = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    if m := R_YMD.search(text):
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=ref.tzinfo)
        except ValueError:
            pass
    if m := R_MD_CN.search(text):
        if d := _future(ref, int(m[1]), int(m[2])):
            return d
    if m := R_REL_CN.search(text):
        w = m[1]
        off = 3 if w == "大后天" else 2 if w == "后天" else 1 if w.startswith("明") else 0
        return base + timedelta(days=off)
    if m := R_WEEK_CN.search(text):
        target, pre = WD_CN[m[2]], m[1] or ""
        if pre in ("下", "下下"):
            days = (7 - ref.weekday()) + target + (7 if pre == "下下" else 0)
        else:
            days = (target - ref.weekday()) % 7
        return base + timedelta(days=days)
    if m := R_REL_EN.search(text):
        return base + timedelta(days=1 if m[1].lower() == "tomorrow" else 0)
    if m := R_MD_EN.search(text):
        mon, day = (m[1], m[2]) if m[1] else (m[4], m[3])
        if d := _future(ref, MON_EN[mon[:3].lower()], int(day)):
            return d
    if m := R_WEEK_EN.search(text):
        target = WD_EN[m[2][:3].lower()]
        days = (target - ref.weekday()) % 7 + (7 if m[1] else 0)
        return base + timedelta(days=days)
    if m := R_MD_NUM.search(text):
        mo, dy = int(m[1]), int(m[2])
        if 1 <= mo <= 12 and 1 <= dy <= 31 and (d := _future(ref, mo, dy)):
            return d
    if m := R_D_CN.search(text):
        dy = int(m[1])
        if 1 <= dy <= 31:
            try:
                d = base.replace(day=dy)
            except ValueError:
                return None
            if d < base:
                nm = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
                try:
                    d = nm.replace(day=dy)
                except ValueError:
                    return None
            return d
    return None


def _time(text: str) -> tuple[int, int] | None:
    for rx in (R_TIME, R_TIME_CN):
        if m := rx.search(text):
            h = int(m[2])
            mi = int(m[3]) if rx is R_TIME else (30 if m[3] == "半" else int(m[4] or 0))
            if m[1] in ("下午", "晚上", "今晚", "明晚") and h < 12:
                h += 12
            if m[1] == "中午" and h < 6:
                h += 12
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return h, mi
    if m := R_TIME_EN.search(text):
        h, mi = int(m[1]) % 12, int(m[2] or 0)
        if m[3].lower() == "pm":
            h += 12
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    return None


def find_datetime(text: str, ref: datetime) -> tuple[datetime | None, bool]:
    if not text:
        return None, False
    d, t = _date(text, ref), _time(text)
    if d is None:
        return None, False
    if t:
        return d.replace(hour=t[0], minute=t[1]), True
    return d.replace(hour=23, minute=59), False
