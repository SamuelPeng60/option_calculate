# -*- coding: utf-8 -*-
"""
mock_live_quotes.py

模擬即時報價產生器
------------------------------------
目的：在還沒確認mis.taifex真實API之前，先產生「格式跟真實資料一樣」的假資料，
用來測試整條 pipeline (抓資料 → 算IV → 比對baseline → 判斷貴賤) 有沒有串好。

介面刻意設計成跟 taifex_fetch.py 的 fetch_underlying_futures_price() /
fetch_live_quotes() 回傳一樣的型別，之後你把真實API接上時，直接替換掉
呼叫這兩個function的地方就好，pipeline其他部分完全不用改。

模擬邏輯：
1. 先假設一個「真實IV」，用 Black-76 算出每個履約價的「理論上合理」的價格
2. 故意在其中幾檔動手腳，讓價格偏離理論價一段百分比(模擬真實市場的貴/便宜)
3. 這樣demo pipeline時，可以親眼看到「貴/合理/便宜」三種狀態都被正確標記出來
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Literal
import random

from black76_iv import black76_price
from taifex_fetch import RawLiveQuote

OptionType = Literal["C", "P"]


def mock_fetch_underlying_futures_price(base: float = 17490.0, noise: float = 15.0) -> float:
    """模擬台指期即時點數，帶一點隨機跳動"""
    return round(base + random.uniform(-noise, noise), 1)


def mock_fetch_live_quotes(
    expiry_type: Literal["week", "month"],
    expiry_date: date,
    right_type: OptionType,
    underlying_price: float,
    true_iv: float = 0.15,
    r: float = 0.015,
    strike_range: int = 5,       # 價平上下各幾檔
    strike_step: int = 100,      # 履約價間距(台指選通常100點一檔)
    force_mispriced_strikes: dict[float, float] | None = None,
    # 手動指定要故意做偏差的履約價 → 偏差比例，例如 {17600: 0.09} 代表17600這檔故意貴9%
) -> list[RawLiveQuote]:
    """
    產生一組模擬的選擇權報價鏈(單一到期日、單一買賣權)。
    """
    force_mispriced_strikes = force_mispriced_strikes or {}

    T = max((expiry_date - date.today()).days, 1) / 365
    atm_strike = round(underlying_price / strike_step) * strike_step

    quotes = []
    for i in range(-strike_range, strike_range + 1):
        strike = atm_strike + i * strike_step

        fair = black76_price(underlying_price, strike, T, r, true_iv, right_type)
        if fair <= 0:
            continue

        # 預設加一點小雜訊(模擬正常的buy/sell spread誤差, ±1.5%)，
        # 如果這個履約價有被指定要故意做偏差，就蓋掉雜訊改用指定的偏差幅度
        if strike in force_mispriced_strikes:
            bias = force_mispriced_strikes[strike]
        else:
            bias = random.uniform(-0.015, 0.015)

        market_price = round(fair * (1 + bias), 1)
        spread = max(market_price * 0.02, 0.5)

        quotes.append(RawLiveQuote(
            strike_price=strike,
            right_type=right_type,
            expiry_type=expiry_type,
            expiry_date=expiry_date,
            bid=round(market_price - spread / 2, 1),
            ask=round(market_price + spread / 2, 1),
            last_price=market_price,
        ))

    return quotes


def mock_iv_history(strike_price: float, days: int = 5, center_iv: float = 0.15, noise: float = 0.01):
    """模擬過去N天的IV歷史(給月選5日移動平均測試用)"""
    return [round(center_iv + random.uniform(-noise, noise), 4) for _ in range(days)]
