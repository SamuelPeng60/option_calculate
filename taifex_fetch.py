# -*- coding: utf-8 -*-
"""
taifex_fetch.py

資料抓取模組 — 分成兩塊，可信度不同，先講清楚：

【A. 歷史行情回補】(backfill_monthly_iv_history)
    用期交所官方「選擇權每日交易行情查詢」頁面 (www.taifex.com.tw/cht/3/optDailyMarketReport)
    這是正式公開的查詢/下載功能，用POST帶查詢日期跟MarketCode(0=日盤,1=夜盤)取得歷史收盤行情。
    這個模組的 HTTP 請求邏輯是照這個頁面公開的查詢方式寫的，但因為我這邊的環境連不到
    taifex.com.tw 網域(網路白名單沒開放)，我沒辦法在這裡實際發送請求驗證回傳格式，
    你在自己的伺服器上跑第一次時，務必先手動印出 df.head() 檢查欄位對不對，
    期交所偶爾會微調表格欄位(例如新增「契約到期日」欄位)，要抓對欄位名稱。

【B. 即時報價】(fetch_live_quotes)
    這塊我「沒有」確認過實際的API網址跟回傳格式，只是先把介面(function signature)
    定義好，方便接下來接進 iv_baseline / black76_iv 那兩個已經測過的模組。
    你需要自己做一件事：在盤中打開瀏覽器開發者工具(F12) → Network分頁 →
    篩選XHR → 到 mis.taifex.com.tw 的即時報價頁面 → 觀察它實際打了哪個API網址、
    帶了什麼參數、回傳什麼JSON格式，然後把下面 fetch_live_quotes() 裡的 TODO 補完。
    我不想給你一個「看起來能動但其實網址是我猜的」的函式，那樣風險更高。
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Literal, Optional
import io

import requests
import pandas as pd

Session = Literal["day", "night"]
OptionType = Literal["C", "P"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------------------
# 剩餘時間 T：算到「結算價決定的那一刻」，不是到期日整天
# ---------------------------------------------------------------------------
#
# TXO 的最後結算價 = 到期日「開盤後30分鐘內」台股加權指數的簡單算術平均，
# 也就是 09:00–09:30 那段。09:30 一到，這口合約的損益就已經確定了 ——
# 它還會繼續掛牌交易到 13:45，但那之後的價格裡不含任何時間價值。
#
# 為什麼這件事非算對不可(實測)：
#   原本是 T = max(days_to_expiry, 1) / 365，到期日當天 days=0 → T=1/365，
#   但實際只剩幾小時。同一條鏈裡「反推IV」跟「算合理價」用的是同一個錯的T，
#   所以偏斜曲線/ATM那兩條路的判斷會自我抵銷(實測偏差 0.0%)；
#   但資料庫5日均那條**完全不抵銷**，因為IV是與T無關的參數：
#       到期日 11:00、市場真實 IV=0.20
#         程式反推出來的 IV = 0.0646        ← 畫面顯示的IV本身就是錯的
#         基準=同鏈曲線   → fair   0.0%     ✅ 抵銷
#         基準=DB 5日均   → cheap -67.7%    ❌ 整條鏈假性便宜
#   而且不管走哪條路，到期日當天畫面上顯示的IV都是錯的。
SETTLEMENT_TIME = dtime(9, 30)

# 曆年基準，跟原本的 days/365 一致(不是交易日基準)
SECONDS_PER_YEAR = 365 * 24 * 3600

# 各盤別的收盤時刻 —— 回補歷史時要知道「那天收盤當下」還剩多少時間。
# 夜盤的 cursor_date 是「歸屬交易日」(8/18夜盤歸在8/19)，所以收盤是當天凌晨05:00。
SESSION_CLOSE_TIME: dict[str, dtime] = {"day": dtime(13, 45), "night": dtime(5, 0)}


def years_to_settlement(expiry_date: date, now: Optional[datetime] = None) -> float:
    """
    到「結算價決定時點」還剩幾年(Black-76 的 T)。

    **可能回傳 0 或負數**，代表結算價已定、時間價值歸零 —— 呼叫端一定要自己處理，
    不要 max(T, 某個下限) 硬擠出一個正數：那正是原本 max(days,1)/365 的錯，
    會讓畫面顯示一個不存在的IV。
    """
    now = now or datetime.now()
    settle = datetime.combine(expiry_date, SETTLEMENT_TIME)
    return (settle - now).total_seconds() / SECONDS_PER_YEAR


# ---------------------------------------------------------------------------
# A. 官方歷史行情查詢 (用於冷啟動回補)
# ---------------------------------------------------------------------------

def fetch_daily_options_report(query_date: date, session: Session) -> pd.DataFrame:
    """
    查詢期交所官方公布的選擇權「單日」收盤行情(日盤或夜盤)。

    注意 query_date 的含意：
    - session='day'：查的就是當天日盤 08:45-13:45
    - session='night'：查的是「歸屬日期」— 例如 8/10 的夜盤，
      實際交易時間是 8/10 15:00 到 8/11 05:00，但查詢時 query_date 要填 8/11
      (期交所是用交易量歸屬日期分類的，這點很容易搞混，一定要注意)

    回傳 DataFrame，欄位至少包含：
      契約、到期月份(週別)、履約價、買賣權、開盤價、最高價、最低價、最後成交價
    """
    url = "https://www.taifex.com.tw/cht/3/optDailyMarketReport"
    payload = {
        "queryDate": query_date.strftime("%Y/%m/%d"),
        "commodity_id": "TXO",
        "MarketCode": "1" if session == "night" else "0",
    }
    resp = requests.post(url, data=payload, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    # 根據公開範例，目標表格通常是回傳list中的第3個(index=2)，
    # 但期交所偶爾調整版面，這裡務必在第一次執行時人工核對。
    df = tables[2]
    df.columns = df.iloc[3]
    df = df.iloc[4:-1, :].copy()
    df = df[df["開盤價"] != "-"]  # 濾掉當天完全沒成交、只有掛牌沒行情的履約價
    return df.reset_index(drop=True)


def backfill_monthly_iv_history(
    db_conn,
    expiry_date: date,
    lookback_trading_days: int = 5,
    r: float = 0.015,
    underlying_close_fn=None,
    max_calendar_days: int = 30,
) -> int:
    """
    冷啟動用：往前抓 lookback_trading_days 個交易日(日盤+夜盤分開)的官方收盤行情，
    反推IV，寫進 option_iv_history。

    這裡簡化處理「交易日」：直接往前推日曆天，跳過發現查無資料(例如假日)的日期，
    直到湊滿 lookback_trading_days 天為止。正式上線建議改用期交所的交易日曆表更精準。
    往前找超過 max_calendar_days 個日曆天就放棄(見下面的迴圈上限說明)。

    ⚠ underlying_close_fn 是必填的：
      簽名 (trade_date, session) -> float，回傳那個交易時段的台指期收盤價。
      沒有它就沒辦法反推出正確的IV，理由見下面 raise 的地方。
    """
    from black76_iv import implied_vol  # 用你已經測過的模組
    from iv_baseline import save_session_close_iv

    if underlying_close_fn is None:
        # 這裡原本是 underlying_price_placeholder = strike，也就是每一檔都用 F=K 反推，
        # 算出來的全部是「價平IV」，完全不是那一檔真正的IV。
        #
        # 為什麼不能讓它就這樣先跑：這些數字會寫進 option_iv_history，而
        # pricing_service.analyze_chain() 的基準優先序是
        #     資料庫5日均 > 偏斜曲線 > ATM單點
        # 也就是說一旦這張表有資料，整條鏈的判斷就會**靜默地**改用這批假IV當基準，
        # 而且畫面上還會標示「資料庫5日IV移動平均(同履約價自己比，不受skew影響)」，
        # 看起來比曲線基準更可信。這比直接報錯危險得多。
        #
        # 補上真正的台指期收盤價之後，把 underlying_close_fn 傳進來就能用了。
        raise NotImplementedError(
            "backfill_monthly_iv_history() 需要 underlying_close_fn 才能用："
            "沒有當日台指期收盤價就只能拿履約價當F，反推出來的IV是假的，"
            "而它會被 analyze_chain() 當成最高優先序的基準。"
            "請提供 (trade_date, session) -> 台指期收盤價 的函式。"
        )

    collected_days = 0
    cursor_date = date.today()
    total_written = 0
    # 迴圈上限：原本只靠 day_found_any 遞增，但抓不到資料的例外被 continue 吞掉了，
    # 所以網路斷線或期交所改版時，這個迴圈會一天一天無限往回走、每天發兩次HTTP請求，
    # 永遠不會停。用日曆天數當硬上限。
    days_walked = 0

    while collected_days < lookback_trading_days:
        if days_walked >= max_calendar_days:
            raise TaifexApiError(
                f"往前找了 {max_calendar_days} 個日曆天只湊到 {collected_days} 個交易日"
                f"(需要 {lookback_trading_days} 個)。可能是網路不通、期交所改版，"
                f"或連假太長 —— 檢查一下 fetch_daily_options_report() 是不是整批失敗。"
            )
        cursor_date -= timedelta(days=1)
        days_walked += 1
        day_found_any = False

        for session in ("day", "night"):
            try:
                df = fetch_daily_options_report(cursor_date, session)
            except Exception:
                continue  # 查無資料(假日/非交易日)，跳過

            if df.empty:
                continue
            day_found_any = True

            records = []
            for _, row in df.iterrows():
                try:
                    strike = float(str(row["履約價"]).replace(",", ""))
                    right = "C" if "買權" in str(row["買賣權"]) else "P"
                    close_price = float(str(row["最後成交價"]).replace(",", ""))
                    if close_price <= 0:
                        continue

                    underlying_close = float(underlying_close_fn(cursor_date, session))
                    if underlying_close <= 0:
                        continue

                    # 那天「收盤當下」還剩多少時間。跟 analyze_chain() 用同一個
                    # 結算時點，兩邊的T基準不一致的話，這批寫進 option_iv_history
                    # 的IV會系統性偏掉 —— 而它是最高優先序的基準。
                    T = years_to_settlement(
                        expiry_date,
                        datetime.combine(cursor_date, SESSION_CLOSE_TIME[session]),
                    )
                    if T <= 0:
                        continue      # 那個時點結算價已定，沒有IV可言
                    iv_result = implied_vol(
                        market_price=close_price,
                        F=underlying_close,
                        K=strike, T=T, r=r, option_type=right,
                    )
                    if not iv_result.converged:
                        continue

                    records.append({
                        "strike_price": strike,
                        "right_type": right,
                        "underlying_price": underlying_close,
                        "close_price": close_price,
                        "implied_vol": iv_result.iv,
                    })
                except (KeyError, ValueError):
                    continue

            if records:
                total_written += save_session_close_iv(
                    db_conn, cursor_date, session, "month", expiry_date, records
                )

        if day_found_any:
            collected_days += 1

    return total_written


# ---------------------------------------------------------------------------
# B. 即時報價 — mis.taifex.com.tw 內部API (已實測驗證)
# ---------------------------------------------------------------------------
#
# 這塊原本是TODO，現在已經實作完成。API是怎麼確認出來的、以及每個規則的依據，
# 都記在下面，之後期交所改版時你可以照同樣方法重新驗證：
#
# 1. mis.taifex.com.tw 是Nuxt(Vue)寫的SPA，前端JS裡直接寫死了 apiBaseUrl 跟端點名，
#    從 /futures/_nuxt/*.js 裡搜 util.format("%s%s", apiBaseUrl, "端點名") 就能撈出全部端點。
# 2. 實際打過每個端點確認回傳格式，下面的欄位名稱都是真實回傳裡有的。
#
# 用到的兩個端點(都是 POST + JSON body)：
#   getCmdyMonthDDLItemByKind → 列出某商品現在掛牌的所有到期別，附真實到期日
#   getQuoteList              → 撈某個到期別底下全部履約價的即時報價
#
# 重要參數(從前端JS的 pageAttr 抓出來的)：
#   MarketType : "0"=日盤, "1"=夜盤(盤後)
#   SymbolType : "O"=選擇權, "F"=期貨
#   KindID     : "1"=股價指數類
#   CID        : 商品代號, 例如 "TXO"(臺指選) / "TXF"(臺指期)
#   ExpireMonth: 到期別代碼, 例如 "202609"(月選) / "202608W4"(週選)
#   RowSize    : "全部" 代表不分頁(注意這是中文字串，送出時要用UTF-8編碼)
#
# ⚠ 注意：TradingRights 參數送 "C" 並不會在server端過濾，回傳仍是call+put全部，
#    所以買賣權要在本地端自己篩(用SymbolID裡的月份碼判斷，見 parse_option_symbol)。

import json
import re

MIS_API_BASE = "https://mis.taifex.com.tw/futures/api"

MIS_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://mis.taifex.com.tw",
    "Referer": "https://mis.taifex.com.tw/futures/",
    "User-Agent": HEADERS["User-Agent"],
}

# 選擇權SymbolID格式(已實測)：前綴 + 履約價 + 月份碼 + 年碼 + "-" + 盤別
#   TXO21800I6-O  → 月選,   履約價21800, I=9月Call, 2026年, 日盤
#   TX440600T6-O  → 週選W4, 履約價40600, T=8月Put,  2026年, 日盤
#   TXO21800U6-N  → 月選,   履約價21800, U=9月Put,  2026年, 夜盤
#
# 前綴會隨到期別變化(月選TXO、週三選TX4、週五選TXX/TXY...)，而且期交所新增週別時
# 還會冒出新前綴，所以這裡「只解析、不組裝」——SymbolID一律從API回傳的資料讀，
# 絕對不要自己拼字串，那樣期交所一改版就會壞掉。
_SYMBOL_RE = re.compile(r"^([A-Z0-9]{3})(\d{4,6})([A-Z])(\d)-([ON])$")

# 期貨SymbolID：商品代號 + 月份碼 + 年碼 + "-" + 盤別後綴，例如 TXFH6-F(日盤) / TXFH6-M(夜盤)。
# 現貨是 TXF-S / TXF-P，沒有月份碼，所以這條regex剛好可以把現貨排除掉。
_FUTURES_RE = re.compile(r"^[A-Z]{2,4}[A-Z]\d-[A-Z]$")

# 月份碼對照(期交所/CME通用慣例，已用 202609→I,U 與 202610→J,V 實測驗證)：
#   買權(Call) A~L 代表 1~12月
#   賣權(Put)  M~X 代表 1~12月
_CALL_CODES = "ABCDEFGHIJKL"
_PUT_CODES = "MNOPQRSTUVWX"

# 到期別代碼格式：YYYYMM(月選) / YYYYMM+週別字母+週序(週選)，例如 202609W1、202609F1。
#   W = 週三結算的週選、F = 週五結算的週選 —— 同一個月會有兩組交錯排列。
#   ⚠ 沒有 W3：當月第三個週三是月選的結算日，那一週的週三合約就是月選本身。
#   2026/09 為例：W1=09/02、F1=09/04、W2=09/09、F2=09/11、月選=09/16、
#                 F3=09/18、W4=09/23、F4=09/25、W5=09/30(有第五週才有)。
#   期交所之後若再加別的星期(例如週一)會冒出新字母，所以這裡不寫死只認W/F：
#   「有字母就是週選」，星期幾一律從真實到期日推，不從字母猜。
_EXPIRY_CODE_RE = re.compile(r"^(\d{6})(?:([A-Z])(\d{1,2}))?$")
_WEEKDAY_ZH = "一二三四五六日"


def parse_expiry_code(code: str) -> tuple[Literal["week", "month"], Optional[str]]:
    """
    拆解到期別代碼 → (week/month, 週別代碼)。

      "202609"   → ("month", None)
      "202609W1" → ("week", "W1")    第1個週三
      "202609F1" → ("week", "F1")    第1個週五

    認不出來的格式一律當週選、週別代碼給None(寧可不標，也不要標錯)。
    """
    m = _EXPIRY_CODE_RE.match(code.strip().upper())
    if not m:
        return "week", None
    _ym, letter, seq = m.groups()
    if letter is None:
        return "month", None
    return "week", f"{letter}{int(seq)}"


class TaifexApiError(RuntimeError):
    """期交所API回傳非預期結果時拋出(RtCode非0、或HTTP層失敗)"""


@dataclass
class RawLiveQuote:
    strike_price: float
    right_type: OptionType
    expiry_type: Literal["week", "month"]
    expiry_date: date
    bid: float
    ask: float
    last_price: float
    # 以下是接真實API之後才有的欄位，都給預設值，
    # 這樣 mock_live_quotes.py 舊的呼叫方式(只給前7個)不用改也還能跑
    volume: int = 0
    open_interest: int = 0
    ref_price: float = 0.0          # 參考價(通常是前一交易日結算價)
    quote_time: str = ""            # 該檔最後更新時間 HHMMSS
    symbol_id: str = ""             # 原始SymbolID，除錯時很有用
    price_source: str = ""          # 價格取自 "last"(成交價) 或 "mid"(買賣中價)


@dataclass
class ContractMonth:
    """一個掛牌中的到期別"""
    code: str                        # API用的代碼, 例如 "202609" / "202609W1"
    expiry_date: date                # 真實到期日(期交所直接給，不用自己算交易日曆)
    expiry_type: Literal["week", "month"]
    display_name: str
    # 週選才有的週別代碼："W1"=第1個週三、"F1"=第1個週五。月選是None。
    # 認不出來的代碼格式也是None(見 parse_expiry_code)
    week_code: Optional[str] = None

    @property
    def weekday_zh(self) -> str:
        """結算日是星期幾。以真實到期日為準，不是從代碼字母猜的"""
        return "週" + _WEEKDAY_ZH[self.expiry_date.weekday()]

    @property
    def kind_label(self) -> str:
        """給人看的類型標籤：月選 / 週選W1(週三) / 週選F1(週五)"""
        if self.expiry_type == "month":
            return "月選"
        if self.week_code:
            return f"週選{self.week_code}({self.weekday_zh})"
        return f"週選({self.weekday_zh})"


def _mis_post(endpoint: str, payload: dict, timeout: int = 15) -> dict:
    """
    打 mis.taifex 的內部API並做基本錯誤檢查。

    注意：payload裡有中文(RowSize="全部")，一定要自己 json.dumps + encode("utf-8")，
    不能直接用 requests 的 json= 參數在某些環境下會用錯編碼，期交所server會回
    "Invalid UTF-8 start byte" 的400錯誤(這個坑我實際踩過)。
    """
    url = f"{MIS_API_BASE}/{endpoint}"
    resp = requests.post(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=MIS_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("RtCode") != "0":
        raise TaifexApiError(
            f"API {endpoint} 回傳失敗: RtCode={data.get('RtCode')} RtMsg={data.get('RtMsg')}"
        )
    return data.get("RtData", {})


def parse_option_symbol(symbol_id: str) -> Optional[tuple[float, OptionType]]:
    """
    從SymbolID解析出 (履約價, 買賣權)。無法解析時回傳None(呼叫端直接跳過該筆)。

    判斷買賣權完全靠月份碼：A~L是買權、M~X是賣權。
    不能用「-O結尾就是Option」來判斷買賣權，那個O是指盤別(日盤)不是買權。
    """
    m = _SYMBOL_RE.match(symbol_id)
    if not m:
        return None

    _prefix, strike_str, month_code, _year_code, _session_code = m.groups()

    if month_code in _CALL_CODES:
        right: OptionType = "C"
    elif month_code in _PUT_CODES:
        right = "P"
    else:
        return None

    return float(strike_str), right


def _to_float(value, default: float = 0.0) -> float:
    """API的數字欄位都是字串，而且沒報價時可能是 "" 或 "0.000"，統一轉換"""
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _pick_price(last: float, bid: float, ask: float) -> tuple[float, str]:
    """
    決定要用哪個價格去反推IV。

    為什麼不能直接用成交價：選擇權很多履約價(尤其深價外/深價內)整天都沒成交，
    CLastPrice會是0，直接拿0去反推IV一定失敗。實測202609那條鏈554檔裡，
    有相當比例的last是0.000，但bid/ask是有掛的。

    規則：
      1. 有成交價就用成交價(最貼近真實市場)
      2. 沒成交價但買賣價都有 → 用中價(mid)，這是業界標準做法
      3. 都沒有 → 回傳0，呼叫端會跳過這檔
    """
    if last > 0:
        return last, "last"
    if bid > 0 and ask > 0:
        return (bid + ask) / 2, "mid"
    return 0.0, ""


# 期交所伺服器的時鐘跟本機不會完全同步。報價時間「比現在晚一點點」是時鐘偏差，
# 不是跨午夜 —— 但如果差一秒就當成跨午夜，age 會直接變成 86399(23.99小時)。
#
# 這個坑剛好打在最不該出錯的地方：CTime 等於「現在這一秒」的，就是當下正在成交、
# 流動性最好的那幾檔。本機時鐘只要慢一秒，它們全部會被當成最陳舊的報價：
#   - 被 max_age_sec 濾掉
#   - 被踢出挑基準的池子(BASELINE_MAX_AGE=900)，基準改用比較舊的報價
#   - 配偏斜曲線時權重掉到 0.5**(86399/900) ≈ 1e-29，等於完全不採計
#   - 畫面標成 stale 淡化顯示
#
# 所以留一段容忍區間：只有差距超過這個秒數才視為跨午夜，微小的負值一律夾成0。
CLOCK_SKEW_TOLERANCE = 300


def quote_age_seconds(quote_time: str, now: Optional[datetime] = None) -> Optional[int]:
    """
    算某筆報價距離現在過了幾秒。無法判斷時回傳None。

    為什麼需要這個(實測踩到的坑)：
    API回傳的CLastPrice是「最後一筆成交價」，但完全沒告訴你那是多久以前成交的。
    夜盤週選流動性很差，實測23:21時抓到的報價裡，有的成交時間是17:17——
    那個價格已經是6小時前的了，中間台指期已經跑掉幾百點，
    拿這種價格去反推IV，算出來的數字完全沒有參考價值
    (實測會看到Put價格不隨履約價遞增這種明顯不合理的現象)。

    所以排程正式跑的時候，建議用這個function過濾掉太舊的報價，例如：
        fresh = [q for q in quotes
                 if (age := quote_age_seconds(q.quote_time)) is not None and age < 300]

    quote_time 是API的CTime欄位，格式為 "HHMMSS"(例如 "231953" = 23:19:53)。

    ⚠ 夜盤會跨午夜：現在是00:30、報價時間是23:50，那是「昨天晚上」的報價，
      只過了40分鐘，不是「還沒發生的未來報價」。下面用「算出來是負的就加一天」
      來處理這個情況 —— 但只有負得夠多才算跨午夜，理由見 CLOCK_SKEW_TOLERANCE。
    """
    if not quote_time or not quote_time.strip().isdigit():
        return None

    s = quote_time.strip().zfill(6)
    try:
        hh, mm, ss = int(s[0:2]), int(s[2:4]), int(s[4:6])
        if hh > 23 or mm > 59 or ss > 59:
            return None
    except ValueError:
        return None

    now = now or datetime.now()
    quoted_secs = hh * 3600 + mm * 60 + ss
    now_secs = now.hour * 3600 + now.minute * 60 + now.second

    age = now_secs - quoted_secs
    if age < -CLOCK_SKEW_TOLERANCE:
        age += 24 * 3600   # 真的跨午夜：報價是昨天的
    return max(age, 0)     # 幾秒的時鐘偏差夾成0，不要變成「未來的報價」


def list_contract_months(
    cid: str = "TXO",
    symbol_type: Literal["O", "F"] = "O",
    session: Session = "day",
    kind_id: str = "1",
) -> list[ContractMonth]:
    """
    列出某商品目前掛牌中的所有到期別，並附上「真實到期日」。

    這個function解決了README裡提到的「交易日曆還沒接」問題：
    期交所在 dispName 欄位直接給了到期日，格式像 "202609(2026/09/16)"，
    所以不需要自己算「第三個星期三」或處理國定假日順延，直接讀官方給的就好。

    回傳範例(2026/08/28實測)：
      202608F4 週選F4(週五) 到期 2026/08/28
      202609W1 週選W1(週三) 到期 2026/09/02
      202609F1 週選F1(週五) 到期 2026/09/04
      202609W2 週選W2(週三) 到期 2026/09/09
      202609   月選         到期 2026/09/16

    週選同時有「週三結算(W系列)」跟「週五結算(F系列)」兩組，
    期交所只掛最近幾個，所以清單裡W跟F是交錯出現的。
    """
    payload = {
        "MarketType": "1" if session == "night" else "0",
        "SymbolType": symbol_type,
        "KindID": kind_id,
        "CID": cid,
        "ExpireMonth": "",
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    rt = _mis_post("getCmdyMonthDDLItemByKind", payload)

    results: list[ContractMonth] = []
    for item in rt.get("Items", []):
        code = str(item.get("item", "")).strip()
        disp = str(item.get("dispName", "")).strip()

        # "現貨" 那筆不是合約，dispName也是空的，直接跳過
        if not code or code == "現貨" or not disp:
            continue

        m = re.search(r"\((\d{4})/(\d{2})/(\d{2})\)", disp)
        if not m:
            continue
        expiry = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        # 純數字YYYYMM是月選；後面帶字母+數字的是週選(W=週三、F=週五)
        expiry_type, week_code = parse_expiry_code(code)

        results.append(ContractMonth(code=code, expiry_date=expiry,
                                     expiry_type=expiry_type, display_name=disp,
                                     week_code=week_code))

    return results


def resolve_expire_month_code(
    expiry_type: Literal["week", "month"],
    expiry_date: date,
    cid: str = "TXO",
    session: Session = "day",
) -> str:
    """
    把「到期類型 + 到期日」轉成API要的ExpireMonth代碼。

    pipeline那邊是用expiry_date在思考的，但API要的是"202608W4"這種代碼，
    這個function負責中間的轉換(去問一次期交所現在有哪些到期別，找出日期對得上的那個)。
    """
    months = list_contract_months(cid=cid, session=session)
    for cm in months:
        if cm.expiry_date == expiry_date and cm.expiry_type == expiry_type:
            return cm.code

    available = ", ".join(f"{c.code}({c.expiry_date}, {c.expiry_type})" for c in months)
    raise TaifexApiError(
        f"找不到 expiry_type={expiry_type} 且到期日={expiry_date} 的掛牌合約。"
        f"目前掛牌中的有: {available}"
    )


@dataclass
class UnderlyingQuote:
    """台指期的即時報價(比單純一個點數多帶了漲跌/時間，網頁上面那條要用)"""
    price: float
    ref_price: float          # 參考價，期交所給的是前一交易日結算價
    quote_time: str           # HHMMSS
    price_source: str         # "last"=成交價 / "mid"=買賣中價
    symbol_id: str = ""


def fetch_underlying_quote(
    expire_month: Optional[str] = None,
    session: Session = "day",
    cid: str = "TXF",
) -> UnderlyingQuote:
    """
    抓台指期(TXF)即時報價，其中的 price 就是Black-76的F。

    expire_month=None 時取「最近月」的期貨(一般情況用這個就對了)。
    如果你想讓月選用同月份期貨定價(理論上更精確)，可以傳 expire_month="202609"。

    為什麼用期貨不用現貨(TXF-S)：Black-76模型的標的就是期貨價，而且選擇權
    跟期貨的到期/結算是連動的，用現貨會多出一段基差(basis)造成IV系統性偏移。

    價格取用順序：成交價 → 買賣中價。收盤後(Status=TC)拿到的是當日最後成交價。
    """
    payload = {
        "MarketType": "1" if session == "night" else "0",
        "SymbolType": "F",
        "KindID": "1",
        "CID": cid,
        "ExpireMonth": expire_month or "",
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    rt = _mis_post("getQuoteList", payload)
    quotes = rt.get("QuoteList", [])
    if not quotes:
        raise TaifexApiError(f"{cid} 期貨報價回傳空清單(可能是非交易時段或商品代號錯誤)")

    # 回傳的第一筆是「現貨」，要濾掉只留期貨合約。
    #
    # ⚠ 不要用結尾後綴來判斷。實測發現後綴會隨盤別變(這個坑我踩過)：
    #        日盤    夜盤
    #   期貨  -F      -M
    #   選擇權 -O      -N
    #   現貨  -S      -P
    # 寫死 endswith("-F") 的話，夜盤就會完全抓不到東西。
    #
    # 改用「有沒有帶合約月份碼」來分：期貨是 TXF+月份碼+年碼+-X (例如 TXFH6-F)，
    # 現貨則是 TXF-S / TXF-P，商品代號後面直接接後綴、沒有月份碼。
    # 這樣不管期交所之後再新增什麼盤別後綴，都不用改這裡。
    futures = [q for q in quotes if _FUTURES_RE.match(str(q.get("SymbolID", "")))]
    if not futures:
        raise TaifexApiError(
            f"{cid} 回傳中找不到期貨合約(只有現貨?) "
            f"實際收到: {[q.get('SymbolID') for q in quotes[:5]]}"
        )

    if expire_month:
        target = futures  # 已經用ExpireMonth過濾過了
    else:
        target = futures[:1]

    for q in target:
        price, src = _pick_price(
            _to_float(q.get("CLastPrice")),
            _to_float(q.get("CBidPrice1")),
            _to_float(q.get("CAskPrice1")),
        )
        if price > 0:
            return UnderlyingQuote(
                price=price,
                ref_price=_to_float(q.get("CRefPrice")),
                quote_time=str(q.get("CTime", "")),
                price_source=src,
                symbol_id=str(q.get("SymbolID", "")),
            )

    raise TaifexApiError(
        f"{cid} 期貨目前沒有有效報價(成交價與買賣價都是空的)，可能還沒開盤"
    )


def fetch_underlying_futures_price(
    expire_month: Optional[str] = None,
    session: Session = "day",
    cid: str = "TXF",
) -> float:
    """只要點數的話用這個(pipeline原本的介面，內容就是 fetch_underlying_quote().price)"""
    return fetch_underlying_quote(expire_month=expire_month, session=session, cid=cid).price


def fetch_live_quotes(
    expiry_type: Literal["week", "month"],
    expiry_date: date,
    session: Session = "day",
    right_type: Optional[OptionType] = None,
    cid: str = "TXO",
    expire_month: Optional[str] = None,
) -> list[RawLiveQuote]:
    """
    抓某個到期日、全部履約價的即時報價(預設call+put都回，可用right_type篩)。

    參數說明：
      expiry_type / expiry_date : pipeline慣用的表達方式，會自動轉成API的ExpireMonth代碼
      session                   : "day"日盤 / "night"夜盤
      right_type                : 指定"C"或"P"只回單邊(省下pipeline自己篩的功夫)
      expire_month              : 如果你已經知道代碼(例如"202608W4")可以直接傳，
                                  可以少打一次月份清單API，排程每分鐘跑時建議快取後傳這個

    注意：沒有成交價的履約價會用買賣中價替代(見_pick_price)，
    完全沒報價的(bid/ask/last全空)會直接被濾掉，不會回傳。
    """
    if expire_month is None:
        expire_month = resolve_expire_month_code(expiry_type, expiry_date, cid=cid, session=session)

    payload = {
        "MarketType": "1" if session == "night" else "0",
        "SymbolType": "O",
        "KindID": "1",
        "CID": cid,
        "ExpireMonth": expire_month,
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    rt = _mis_post("getQuoteList", payload)

    results: list[RawLiveQuote] = []
    for q in rt.get("QuoteList", []):
        symbol_id = str(q.get("SymbolID", ""))
        parsed = parse_option_symbol(symbol_id)
        if parsed is None:
            continue
        strike, right = parsed

        if right_type is not None and right != right_type:
            continue

        bid = _to_float(q.get("CBidPrice1"))
        ask = _to_float(q.get("CAskPrice1"))
        last = _to_float(q.get("CLastPrice"))
        price, source = _pick_price(last, bid, ask)
        if price <= 0:
            continue  # 這檔完全沒報價，跳過(拿去反推IV一定失敗)

        results.append(RawLiveQuote(
            strike_price=strike,
            right_type=right,
            expiry_type=expiry_type,
            expiry_date=expiry_date,
            bid=bid,
            ask=ask,
            last_price=price,
            volume=int(_to_float(q.get("CTotalVolume"))),
            open_interest=int(_to_float(q.get("OpenInterest"))),
            ref_price=_to_float(q.get("CRefPrice")),
            quote_time=str(q.get("CTime", "")),
            symbol_id=symbol_id,
            price_source=source,
        ))

    results.sort(key=lambda x: (x.right_type, x.strike_price))
    return results


# ---------------------------------------------------------------------------
# 自我測試
#   前半段是純邏輯測試(不需網路)，後半段會實際連期交所。
#   跑法： python taifex_fetch.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("Part 1: 離線邏輯測試(不需網路)")
    print("=" * 70)

    # SymbolID解析：涵蓋月選/週選/日盤/夜盤，以及應該要被拒絕的現貨代碼
    cases = [
        ("TXO21800I6-O", (21800.0, "C"), "月選 9月買權 日盤"),
        ("TXO21800U6-O", (21800.0, "P"), "月選 9月賣權 日盤"),
        ("TXO22000V6-N", (22000.0, "P"), "月選 10月賣權 夜盤"),
        ("TX440600T6-O", (40600.0, "P"), "週選W4 8月賣權"),
        ("TXX39800H6-O", (39800.0, "C"), "週選F3 8月買權"),
        ("TXY41200H6-O", (41200.0, "C"), "週選F4 8月買權"),
        ("TXF-S", None, "現貨(應解析失敗)"),
        ("TXFH6-F", None, "期貨(應解析失敗)"),
        ("", None, "空字串(應解析失敗)"),
    ]
    for symbol, expected, desc in cases:
        got = parse_option_symbol(symbol)
        assert got == expected, f"解析錯誤 {symbol}: 期望{expected} 得到{got}"
        print(f"  ✓ {symbol:<15} → {str(got):<18} {desc}")
    print("✅ SymbolID解析測試通過\n")

    # 到期別代碼解析：月選 / 週三選W / 週五選F
    expiry_cases = [
        ("202609",   ("month", None), "月選"),
        ("202612",   ("month", None), "月選(年底)"),
        ("202609W1", ("week", "W1"), "週三選 第1週"),
        ("202609W4", ("week", "W4"), "週三選 第4週"),
        ("202609F1", ("week", "F1"), "週五選 第1週"),
        ("202608F4", ("week", "F4"), "週五選 第4週"),
        ("202609X1", ("week", "X1"), "沒看過的字母也要當週選"),
        ("現貨",      ("week", None), "認不出來 → 週別代碼給None"),
    ]
    for code_, expected, desc in expiry_cases:
        got = parse_expiry_code(code_)
        assert got == expected, f"到期別代碼解析錯誤 {code_}: 期望{expected} 得到{got}"
        print(f"  ✓ {code_:<10} → {str(got):<20} {desc}")

    # kind_label 的星期是從「真實到期日」算的，不是從字母猜的
    _w1 = ContractMonth("202609W1", date(2026, 9, 2), "week", "", "W1")
    _f1 = ContractMonth("202609F1", date(2026, 9, 4), "week", "", "F1")
    _m = ContractMonth("202609", date(2026, 9, 16), "month", "")
    assert _w1.kind_label == "週選W1(週三)", _w1.kind_label
    assert _f1.kind_label == "週選F1(週五)", _f1.kind_label
    assert _m.kind_label == "月選", _m.kind_label
    print(f"  ✓ 顯示標籤 {_w1.kind_label} / {_f1.kind_label} / {_m.kind_label}")
    print("✅ 到期別代碼解析測試通過\n")

    # 取價邏輯
    assert _pick_price(100.0, 98.0, 102.0) == (100.0, "last"), "有成交價時應優先用成交價"
    assert _pick_price(0.0, 98.0, 102.0) == (100.0, "mid"), "沒成交價時應用買賣中價"
    assert _pick_price(0.0, 0.0, 0.0) == (0.0, ""), "完全沒報價時應回傳0"
    assert _pick_price(0.0, 5.0, 0.0) == (0.0, ""), "只有單邊報價時應回傳0"
    print("  ✓ 成交價優先、無成交價改用中價、全無報價回0")
    print("✅ 取價邏輯測試通過\n")

    # 報價新鮮度(含夜盤跨午夜)
    _base = datetime(2026, 8, 18, 23, 30, 0)
    assert quote_age_seconds("233000", _base) == 0
    assert quote_age_seconds("232900", _base) == 60
    assert quote_age_seconds("171754", _base) == 22326          # 約6.2小時前
    assert quote_age_seconds("235000", datetime(2026, 8, 19, 0, 30, 0)) == 2400  # 跨午夜=40分鐘
    assert quote_age_seconds("") is None
    assert quote_age_seconds("abc") is None
    assert quote_age_seconds("996060") is None                  # 不合法時間

    # 時鐘偏差：報價時間比本機快幾秒是時鐘沒對齊，不是「昨天的報價」。
    # 沒有 CLOCK_SKEW_TOLERANCE 的話這裡會得到 86399，等於把當下正在成交的
    # 那幾檔當成最陳舊的報價丟掉(詳見 quote_age_seconds 上面的說明)。
    _noon = datetime(2026, 8, 18, 12, 0, 0)
    assert quote_age_seconds("120001", _noon) == 0, "快1秒不該變成23.99小時前"
    assert quote_age_seconds("120259", _noon) == 0, "快2分59秒仍在容忍範圍內"
    # 超過容忍範圍才算跨午夜(這種只會出現在凌晨，例如 00:30 看到 23:50 的報價)
    assert quote_age_seconds("120501", _noon) == 86099, "超過容忍值就該當成跨午夜"
    print("  ✓ 同日、跨午夜、時鐘偏差、非法輸入都正確處理")
    print("✅ 報價新鮮度計算測試通過\n")

    # 剩餘時間T：算到「到期日09:30結算價決定」，不是到期日整天
    _exp = date(2026, 9, 16)
    _yr = SECONDS_PER_YEAR
    assert abs(years_to_settlement(_exp, datetime(2026, 9, 16, 8, 30)) - 3600 / _yr) < 1e-12, (
        "到期日早上8:30該剩1小時")
    assert years_to_settlement(_exp, datetime(2026, 9, 16, 9, 30)) == 0.0, (
        "09:30整該剛好歸零")
    assert years_to_settlement(_exp, datetime(2026, 9, 16, 11, 0)) < 0, (
        "09:30之後必須是負的 —— 呼叫端要看得出結算價已定，不能被下限蓋掉")
    # 這是B的核心：舊寫法 max(days,1)/365 在到期日當天會把「剩1小時」灌成「剩一整天」
    _old_T = max((_exp - date(2026, 9, 16)).days, 1) / 365
    _new_T = years_to_settlement(_exp, datetime(2026, 9, 16, 8, 30))
    assert _old_T / _new_T > 20, f"舊寫法把1小時灌成{_old_T * 365 * 24:.1f}小時"
    # 到期前一天的夜盤(凌晨那段)也要算得出來
    _night = years_to_settlement(_exp, datetime(2026, 9, 16, 3, 0))
    assert abs(_night * _yr - 6.5 * 3600) < 1e-6, "到期日凌晨3點該剩6.5小時"
    print(f"  ✓ 到期日08:30剩 {_new_T * 365 * 24:.2f} 小時"
          f"(舊寫法會說 {_old_T * 365 * 24:.0f} 小時)、09:30歸零、之後為負")
    print("✅ 剩餘時間T測試通過")
    print()

    print("=" * 70)
    print("Part 2: 實際連線測試(需要網路，連 mis.taifex.com.tw)")
    print("=" * 70)

    try:
        months = list_contract_months("TXO")
        assert months, "沒抓到任何掛牌合約"
        print(f"掛牌到期別共 {len(months)} 個:")
        for m in sorted(months, key=lambda x: x.expiry_date):
            print(f"  {m.code:<10} {m.kind_label:<14} "
                  f"到期 {m.expiry_date}  ({(m.expiry_date - date.today()).days}天)")

        # 週選一定要能分辨週三/週五那組；順便驗證代碼字母跟真實到期日沒有對不起來
        for m in months:
            if m.expiry_type != "week" or not m.week_code:
                continue
            expect_wd = {"W": 2, "F": 4}.get(m.week_code[0])
            if expect_wd is not None and m.expiry_date.weekday() != expect_wd:
                print(f"  ⚠ {m.code} 的到期日 {m.expiry_date} 是{m.weekday_zh}，"
                      f"跟代碼字母對不起來(顯示以到期日為準)")

        F = fetch_underlying_futures_price()
        assert F > 0, "台指期點數應該大於0"
        print(f"\n台指期近月點數 F = {F}")

        target = next(m for m in months if m.expiry_type == "month")
        quotes = fetch_live_quotes("month", target.expiry_date,
                                   right_type="C", expire_month=target.code)
        assert quotes, "沒抓到任何報價"
        assert all(q.right_type == "C" for q in quotes), "right_type篩選失效"
        assert all(q.last_price > 0 for q in quotes), "不該回傳沒有價格的合約"
        assert quotes == sorted(quotes, key=lambda x: (x.right_type, x.strike_price)), "排序錯誤"

        print(f"{target.code} 買權共 {len(quotes)} 檔，最接近價平的5檔:")
        for q in sorted(quotes, key=lambda x: abs(x.strike_price - F))[:5]:
            age = quote_age_seconds(q.quote_time)
            age_s = f"{age}秒前" if age is not None else "無時間"
            print(f"  K={q.strike_price:>7.0f} 價={q.last_price:>9.2f}({q.price_source}) "
                  f"買/賣={q.bid:>8.1f}/{q.ask:>8.1f} 量={q.volume:>5} {age_s}")

        print("\n✅ 實際連線測試通過")

    except Exception as e:
        print(f"\n⚠ 連線測試失敗: {type(e).__name__}: {e}")
        print("  (如果是網路問題可以忽略；Part 1的邏輯測試已經通過)")
