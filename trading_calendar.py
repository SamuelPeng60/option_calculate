# -*- coding: utf-8 -*-
"""
trading_calendar.py

交易日曆 —— 回答「下一個交易日是哪一天」
------------------------------------------------
給 pricing_service 算「下一個交易日同一時間，權利金會衰減多少」用的。
只跳過週六日的話，連假前會少算(國慶連假前一天看到的「隔日」其實是4天後)。

資料來源：證交所「市場開休市日期」
    https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&date=YYYY0101
台指選擇權的交易日跟集中市場一致(標的就是加權指數)，所以用證交所的表。
期交所自己沒有對應的公開API(實測 2026-10-12：openapi.taifex.com.tw 沒有這張表)。

抓回來的結果存在 trading_holidays.json(跟著版控走，離線也能用)。
用到檔案裡沒有的年度時會自動去抓一次；抓不到(沒網路、證交所還沒公告明年的表)
就退回「只跳過週六日」，並且**讓呼叫端知道這次的答案不完整**(known=False) ——
不要靜靜地當成沒有假日。

注意：
  - 證交所的表裡混著「不是休市」的列，例如「農曆春節前最後交易日」「國曆新年開始交易日」，
    那幾天是有開盤的，要濾掉(_TRADING_DAY_MARKERS)。
  - 「市場無交易，僅辦理結算交割作業」是休市。
  - 颱風假這種臨時休市不在表裡，沒辦法事先知道。

用法：
    python trading_calendar.py          # 重抓今年+明年、印出來、跑自我檢查(會連網)
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

CALENDAR_FILE = Path(__file__).resolve().parent / "trading_holidays.json"
TWSE_URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
FETCH_TIMEOUT = 8          # 秒。這是在算報價的路上順便抓的，不能卡太久
RETRY_INTERVAL = 6 * 3600  # 某年度抓不到時，隔多久才再試(秒)

# 表裡名稱帶這些字的是「有開盤」的日子，不是假日
_TRADING_DAY_MARKERS = ("開始交易", "最後交易")

_holidays: Optional[dict[int, dict[date, str]]] = None   # 年度 → {休市日: 名稱}
_last_try: dict[int, float] = {}


def fetch_year(year: int) -> dict[date, str]:
    """
    向證交所抓某一年的休市日。回傳 {日期: 名稱}。
    那一年還沒公告時證交所回空表，這裡照樣回 {}(呼叫端要當成「不知道」，不是「沒有假日」)。
    """
    resp = requests.get(
        TWSE_URL, params={"response": "json", "date": f"{year}0101"},
        headers=HEADERS, timeout=FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("stat") != "ok":
        raise RuntimeError(f"證交所回傳 stat={body.get('stat')!r}")
    out: dict[date, str] = {}
    for day_str, name, *_ in body.get("data", []):
        if any(marker in name for marker in _TRADING_DAY_MARKERS):
            continue
        out[date.fromisoformat(day_str)] = name
    return out


def _load() -> dict[int, dict[date, str]]:
    global _holidays
    if _holidays is None:
        _holidays = {}
        try:
            raw = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
            for year, entry in raw.get("years", {}).items():
                _holidays[int(year)] = {
                    date.fromisoformat(d): name for d, name in entry["holidays"].items()
                }
        except (OSError, ValueError, KeyError):
            _holidays = {}   # 檔案不在或壞掉就當沒有，用到時再重抓
    return _holidays


def _save() -> None:
    years = {
        str(year): {"holidays": {d.isoformat(): name for d, name in sorted(days.items())}}
        for year, days in sorted(_load().items())
    }
    CALENDAR_FILE.write_text(
        json.dumps({"source": TWSE_URL, "years": years}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def refresh_year(year: int) -> bool:
    """重抓某一年並寫回檔案。有抓到資料才回 True；失敗時例外會往外丟"""
    days = fetch_year(year)
    if not days:
        return False
    _load()[year] = days
    _save()
    return True


def holidays_of(year: int) -> Optional[dict[date, str]]:
    """那一年的休市日；不知道(沒資料也抓不到)回 None"""
    known = _load()
    if year not in known and time.monotonic() - _last_try.get(year, -RETRY_INTERVAL) >= RETRY_INTERVAL:
        _last_try[year] = time.monotonic()
        try:
            refresh_year(year)
        except Exception:
            pass   # 沒網路/證交所擋：退回只跳週末，由回傳的 None 讓呼叫端知道
    return known.get(year)


def next_trading_day(d: date) -> tuple[date, bool]:
    """
    d 之後(不含 d)的第一個交易日。

    第二個回傳值 known：途中經過的每一天是不是都查得到行事曆。
    False 代表有某一年沒資料、那段只跳過了週六日，答案可能偏早。
    """
    known = True
    for i in range(1, 60):
        cand = d + timedelta(days=i)
        if cand.weekday() >= 5:
            continue
        year_days = holidays_of(cand.year)
        if year_days is None:
            known = False
        elif cand in year_days:
            continue
        return cand, known
    raise RuntimeError(f"{d} 之後 60 天內找不到交易日，行事曆資料有問題")


if __name__ == "__main__":
    import sys

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    this_year = date.today().year
    for y in (this_year, this_year + 1):
        try:
            got = refresh_year(y)
        except Exception as exc:
            print(f"{y}: 抓取失敗 {type(exc).__name__}: {exc}")
            continue
        if not got:
            print(f"{y}: 證交所還沒公告")
            continue
        days = _load()[y]
        weekdays = {d: n for d, n in days.items() if d.weekday() < 5}
        print(f"{y}: {len(days)} 個休市日(其中 {len(weekdays)} 個落在平日)")
        for d, n in sorted(weekdays.items()):
            print(f"   {d} 週{'一二三四五'[d.weekday()]}  {n}")
    print(f"已寫入 {CALENDAR_FILE.name}")

    # 自我檢查：用 2026 年的表(抓不到這一年就跳過)
    cal = _load().get(2026)
    if cal:
        assert next_trading_day(date(2026, 10, 12)) == (date(2026, 10, 13), True)   # 一般平日
        assert next_trading_day(date(2026, 10, 16)) == (date(2026, 10, 19), True)   # 週五→週一
        assert next_trading_day(date(2026, 10, 8)) == (date(2026, 10, 12), True)    # 國慶補假(五)
        assert next_trading_day(date(2026, 10, 23)) == (date(2026, 10, 27), True)   # 光復節補假(一)
        assert next_trading_day(date(2026, 2, 11)) == (date(2026, 2, 23), True)     # 春節(含兩天只結算不交易)
        assert next_trading_day(date(2026, 2, 10)) == (date(2026, 2, 11), True)     # 「春節前最後交易日」有開盤
        assert next_trading_day(date(2025, 12, 31))[0] == date(2026, 1, 2)          # 元旦休、1/2「開始交易日」有開盤
        print("自我檢查通過")
