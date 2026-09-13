# -*- coding: utf-8 -*-
"""
pricing_service.py

貴賤判斷的「共用核心」
------------------------------------
把原本寫在 run_live_pipeline.run() 裡的計算流程抽出來，變成一個
不印任何東西、只回傳結構化結果的函式 analyze_chain()。

為什麼要抽出來：
  現在有兩個地方要用同一套邏輯 —— CLI表格(run_live_pipeline.py)跟
  網頁後端(serve_dashboard.py)。如果兩邊各寫一份，
  「基準IV優先序」「陳舊報價過濾」這些踩過坑才調出來的規則遲早會走鐘，
  改了一邊忘了另一邊 —— 那是這個專案最不能出的錯。

流程(跟原本完全一樣，只是不印字)：
  抓報價鏈 → 過濾 → 每檔反推IV → 挑baseline(DB 5日均 > 偏斜曲線 > ATM單點)
  → 算合理價 → 判斷貴/合理/便宜

回傳的 ChainAnalysis 帶著所有「原本用print講出來的診斷資訊」
(基準來源、配適品質、濾掉幾檔、幾檔算不出IV…)，呼叫端要印要塞JSON都可以。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

from black76_iv import implied_vol, evaluate_option
from iv_baseline import get_weekly_baseline_iv, LiveQuote
from iv_skew import fit_skew_curve, quality_weight, SkewPoint, SkewCurve
from db import OptionDB
from taifex_fetch import (
    ContractMonth,
    RawLiveQuote,
    SETTLEMENT_TIME,
    fetch_live_quotes,
    quote_age_seconds,
    years_to_settlement,
)

R = 0.015              # 無風險利率假設
THRESHOLD_PCT = 0.05   # 偏離超過±5%才算貴/便宜

# 挑基準IV時的報價品質門檻。
# 這兩個數字是實測踩坑後定的：夜盤週選最接近價平的那檔是「3.6小時前、成交1口」，
# 拿它當基準會讓整條鏈幾乎每檔都被誤標成偏貴。詳見 README「基準IV本身也會被污染」。
STALE_AGE = 600          # 超過這個秒數的報價視為陳舊(CLI標*，前端淡化顯示)

# 挑基準用的報價年齡上限。**刻意等於 STALE_AGE**，不是另一個數字：
# 原本是 900 > STALE_AGE(600)，於是畫面上標 * 淡化、明白跟使用者說「這筆不可信」的
# 報價，背地裡仍然可以被拿去當整條鏈的基準 —— 兩個門檻的語意互相矛盾。
# 基準的殺傷力比單一檔報價大得多(它是所有判斷的比較標準)，所以基準池不可以比
# 「畫面上說可信」還寬鬆。要調的話往嚴的方向調，不要把這兩個數字再拆開。
BASELINE_MAX_AGE = STALE_AGE
BASELINE_MIN_VOL = 1     # 且至少要有成交量

# 資料庫5日均基準的新鮮度門檻：最新一筆超過這個天數，整組不採用(退回偏斜曲線)。
# 硬規則2 要求基準池「夠新鮮」，但那道守門原本只套在即時報價那條路上；
# DB 這條是最高優先序、還會把 out_of_range 取消掉標成可信，卻完全沒有對應檢查 ——
# 收盤後寫入的排程一斷，整條鏈就被過期的IV靜默評價。
# 給5天(一個正常週末是3天，含一天國定假日是4天)；再長的連假會退回曲線，
# 那是安全的方向 —— 寧可用當下這批報價配出來的曲線，也不要用上週的IV。
BASELINE_DB_MAX_STALE_DAYS = 5

# 退回「ATM單點」當基準時，離價平多遠就不能當真(|ln(K/F)|)。
# 曲線那條路靠 curve.in_range() 擋外插；ATM單點對每個履約價都給同一個IV，
# 比曲線更沒有外插概念，需要的守門只會更多不會更少。
ATM_BASELINE_K_RANGE = 0.15


class NoQuoteDataError(RuntimeError):
    """過濾後沒有報價、或整條鏈都反推不出IV —— 算不出結果但不是程式錯誤"""


class ContractSettledError(NoQuoteDataError):
    """
    結算價已定，這條鏈沒有時間價值可以判斷了(不是錯誤，是這個合約的生命結束了)。

    刻意繼承 NoQuoteDataError：兩個呼叫端都已經在處理那個例外，
    CLI 會印訊息收工、網頁會只標這一邊算不出來而另一邊照常顯示 ——
    正是我們要的行為，不必兩邊各補一段。

    為什麼不乾脆給它一個 T 的下限硬算下去：09:30 之後的價格裡沒有時間價值，
    硬反推會擠出一個不存在的IV，然後畫面照常上色 —— 那比什麼都不顯示更危險。
    """


@dataclass
class StrikeEval:
    """單一履約價的判斷結果"""
    strike: float
    market_price: float
    price_source: str          # "last"=實際成交價 / "mid"=買賣中價
    volume: int
    quote_time: str
    age_sec: Optional[int]
    implied_vol: float
    baseline_iv: float
    fair_price: float
    deviation_pct: float
    status: Literal["expensive", "fair", "cheap"]
    reliable: bool
    # 不可信的兩個原因，分開記。一檔可能「同時」兩個都成立 ——
    # 原本沒記，CLI 只能用 n_unreliable - n_out_of_range 反推 n_tiny，
    # 重疊的那幾檔只算進 out_of_range，n_tiny 就低報了。
    out_of_range: bool = False   # 基準是外插來的/離價平太遠，這一檔的基準本身不能用
    tiny_fair: bool = False      # 合理價過小(深度價外)，百分比偏差失去意義

    @property
    def stale(self) -> bool:
        return self.age_sec is not None and self.age_sec > STALE_AGE


@dataclass
class ChainAnalysis:
    """一條鏈(單一到期別 + 單一買賣權)的完整判斷結果 + 診斷資訊"""
    contract: ContractMonth
    session: Literal["day", "night"]
    right_type: Literal["C", "P"]
    underlying_price: float
    days_to_expiry: int
    time_to_expiry: float
    evals: list[StrikeEval] = field(default_factory=list)   # 依履約價排序

    # 基準IV相關
    baseline_source: str = ""
    baseline_iv_atm: float = 0.0
    baseline_quality_note: str = ""
    curve: Optional[SkewCurve] = None
    curve_rejected: bool = False        # 有配但品質不佳，已退回ATM單點
    db_baseline_count: int = 0
    db_baseline_stale_days: Optional[int] = None   # DB有資料但太舊、整組退掉時，記下最新一筆幾天前

    # 資料品質診斷
    total_before_filter: int = 0
    dropped_volume: int = 0
    dropped_stale: int = 0
    n_failed_iv: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """全鏈的狀態分布(含不可信的那幾檔)"""
        c = {"expensive": 0, "fair": 0, "cheap": 0}
        for e in self.evals:
            c[e.status] += 1
        return c

    @property
    def reliable_counts(self) -> dict[str, int]:
        """
        只統計可信的那些。畫面/CLI 的「偏貴X / 便宜Y」要用這個。

        不可信的檔(合理價過小、或基準是曲線外插來的)在表格裡已經被淡化、不上色了，
        統計行卻把它們算進去的話，表頭跟表格會互相矛盾 —— 使用者看到「偏貴6檔」
        然後在表格裡只找得到2檔紅的，會以為畫面壞了。
        """
        c = {"expensive": 0, "fair": 0, "cheap": 0}
        for e in self.evals:
            if e.reliable:
                c[e.status] += 1
        return c

    @property
    def n_unreliable(self) -> int:
        return sum(1 for e in self.evals if not e.reliable)

    # 下面兩個直接從 evals 數，不用計數器累加。
    # 計數器版本踩過兩次坑：先是加的時機不對(在 db_baseline 覆寫前就 +=1，
    # 結果 n_out_of_range > n_unreliable)，後來是 CLI 用減法反推 n_tiny、
    # 重疊的檔會低報。從結果數就兩個都不可能發生。
    # 注意 out_of_range 記的是「db_baseline 覆寫之後」的最終值 ——
    # 5日均不受配適範圍限制，被它覆寫的那幾檔不算落在範圍外。
    @property
    def n_out_of_range(self) -> int:
        return sum(1 for e in self.evals if e.out_of_range)

    @property
    def n_tiny(self) -> int:
        """合理價過小的檔數。跟 n_out_of_range 會重疊，兩者相加不等於 n_unreliable"""
        return sum(1 for e in self.evals if e.tiny_fair)

    @property
    def atm_strike(self) -> Optional[float]:
        """履約價離台指期最近的那一檔(用實際掛牌履約價，不是把F四捨五入)"""
        if not self.evals:
            return None
        return min((e.strike for e in self.evals),
                   key=lambda k: abs(k - self.underlying_price))

    def window(self, n: Optional[int]) -> list[StrikeEval]:
        """只取ATM上下各n檔(全鏈幾百檔，畫面/JSON都不需要全給)"""
        if n is None or not self.evals:
            return self.evals
        strikes = [e.strike for e in self.evals]
        ci = strikes.index(self.atm_strike)
        return self.evals[max(0, ci - n): ci + n + 1]


def _within_atm_range(strike: float, F: float, k_range: float) -> bool:
    """
    這個履約價離價平夠近、近到可以直接套用ATM那一點的IV嗎(|ln(K/F)| <= k_range)。

    只有在沒有曲線可用時才會走到這裡。深價外的IV其實會回升(微笑的翹尾)，
    拿ATM那個偏低的IV去評價，那幾檔會整批被標成「偏貴」——
    就是 iv_skew.SkewCurve.in_range 檔頭記的那個坑(深價外37檔100%誤判)。
    """
    if strike <= 0 or F <= 0:
        return False
    return abs(math.log(strike / F)) <= k_range


def analyze_chain(
    contract: ContractMonth,
    right_type: Literal["C", "P"],
    session: Literal["day", "night"],
    underlying_price: float,
    quotes: Optional[list[RawLiveQuote]] = None,
    min_volume: int = 0,
    max_age_sec: Optional[int] = None,
    use_skew_curve: bool = True,
    skew_k_range: Optional[float] = 0.15,
    db: Optional[OptionDB] = None,
    threshold_pct: float = THRESHOLD_PCT,
) -> ChainAnalysis:
    """
    對一條鏈做完整的貴賤判斷。

    underlying_price 由呼叫端傳進來(而不是這裡自己抓)，有兩個理由：
      1. 同一次更新裡Call跟Put必須用「同一個F」，不然兩邊的合理價會對不起來
      2. 一次更新只打一次台指期API

    quotes 也可以先抓好傳進來(例如一次抓回call+put再自己分邊)，省一次API往返；
    傳進來的清單會自動依 right_type 篩過。
    """
    F = underlying_price
    days_to_expiry = max((contract.expiry_date - date.today()).days, 0)

    # T 算到結算價決定的那一刻(到期日 09:30)，不是「到期日整天」。
    # 理由跟實測數字見 taifex_fetch.years_to_settlement 上面那段。
    T = years_to_settlement(contract.expiry_date)
    if T <= 0:
        raise ContractSettledError(
            f"{contract.display_name} 的結算價已於 {contract.expiry_date} "
            f"{SETTLEMENT_TIME.strftime('%H:%M')} 決定"
            f"(TXO最後結算價=到期日開盤後30分鐘的加權指數平均)，"
            f"時間價值歸零，不再做貴賤判斷。"
        )

    result = ChainAnalysis(
        contract=contract,
        session=session,
        right_type=right_type,
        underlying_price=F,
        days_to_expiry=days_to_expiry,
        time_to_expiry=T,
    )

    # 1) 報價鏈
    if quotes is None:
        quotes = fetch_live_quotes(
            contract.expiry_type, contract.expiry_date,
            session=session, right_type=right_type, expire_month=contract.code,
        )
    else:
        quotes = [q for q in quotes if q.right_type == right_type]

    result.total_before_filter = len(quotes)

    # 2) 過濾(預設不濾；盤中要看訊號建議帶 min_volume / max_age_sec)
    if min_volume > 0:
        before = len(quotes)
        quotes = [q for q in quotes if q.volume >= min_volume]
        result.dropped_volume = before - len(quotes)

    if max_age_sec is not None:
        before = len(quotes)
        fresh = []
        for q in quotes:
            age = quote_age_seconds(q.quote_time)
            if age is not None and age <= max_age_sec:
                fresh.append(q)
        quotes = fresh
        result.dropped_stale = before - len(quotes)

    if not quotes:
        raise NoQuoteDataError("套用過濾條件後沒有剩下任何報價，試著放寬 min_volume / max_age")

    # 3) 每檔反推IV
    live_objs: list[LiveQuote] = []
    iv_map: dict[float, float] = {}
    for q in quotes:
        res = implied_vol(q.last_price, F, q.strike_price, T, R, right_type)
        if res.converged:
            iv_map[q.strike_price] = res.iv
            live_objs.append(LiveQuote(q.strike_price, right_type, q.last_price, res.iv))
        else:
            result.n_failed_iv += 1

    if not live_objs:
        raise NoQuoteDataError("所有履約價都無法反推IV，資料可能異常")

    # 4) ATM單點基準 —— 只用「夠新鮮 + 有成交量」的報價去挑。
    #    基準是整條鏈的比較標準，被陳舊報價污染的殺傷力比單一檔報價爛大得多。
    quality_objs: list[LiveQuote] = []
    for q in quotes:
        if q.strike_price not in iv_map:
            continue
        age = quote_age_seconds(q.quote_time)
        if q.volume >= BASELINE_MIN_VOL and age is not None and age <= BASELINE_MAX_AGE:
            quality_objs.append(LiveQuote(q.strike_price, right_type,
                                          q.last_price, iv_map[q.strike_price]))

    baseline_pool = quality_objs if quality_objs else live_objs
    result.baseline_quality_note = (
        f"(取自 {len(quality_objs)} 檔新鮮報價)" if quality_objs
        else "(⚠ 沒有夠新鮮的報價，改用全部報價，可靠度低)"
    )
    result.baseline_iv_atm = get_weekly_baseline_iv(baseline_pool, underlying_price=F)

    # 5) 偏斜曲線基準：用曲線值當各履約價的基準，skew成分自動被吸收
    curve = None
    if use_skew_curve:
        points = [
            SkewPoint(
                strike_price=q.strike_price,
                implied_vol=iv_map[q.strike_price],
                weight=quality_weight(q.volume, quote_age_seconds(q.quote_time)),
            )
            for q in quotes if q.strike_price in iv_map
        ]
        curve = fit_skew_curve(points, F, k_range=skew_k_range)
        if curve is not None and not curve.is_reliable():
            result.curve_rejected = True
            curve = None
    result.curve = curve

    # 6) 月選：資料庫裡同履約價的5日IV移動平均(最理想的基準，拿自己跟自己比)
    db_baseline: dict[float, float] = {}
    if db is not None and contract.expiry_type == "month":
        try:
            db_baseline = db.get_baseline_iv_batch(
                right_type, session, contract.expiry_date, lookback_days=5
            )
            # 撈回來還要看「這條鏈的歷史最新寫到哪一天」。
            # db.get_baseline_iv_batch() 只保證每一筆都在時間下限內(不會混進兩個月前的)，
            # 但排程斷掉時整組會一起變舊 —— 上週五寫完就沒再寫，撈回來的5筆全是上週的，
            # 每一筆都通過下限，平均值卻已經不能代表今天的市場。
            # 而這是最高優先序的基準，還會把 out_of_range 取消掉，所以寧可整組不採用。
            if db_baseline:
                latest = db.get_iv_history_latest_date(
                    right_type, session, contract.expiry_date
                )
                # 撈得到資料卻查不到最新日期(理論上不會發生)也一樣不採用 ——
                # 但要記成 -1 而不是 None，不然下面的訊息不會印，變成靜默退掉。
                stale_days = (date.today() - latest).days if latest is not None else -1
                if stale_days < 0 or stale_days > BASELINE_DB_MAX_STALE_DAYS:
                    result.db_baseline_stale_days = stale_days
                    db_baseline = {}
        except Exception:
            db_baseline = {}   # 讀不到就退回即時算法，不要讓整條鏈算不出來
    result.db_baseline_count = len(db_baseline)

    if db_baseline:
        result.baseline_source = (f"資料庫5日IV移動平均 ({len(db_baseline)}檔有歷史，"
                                  f"同履約價自己比，不受skew影響)")
    elif curve is not None:
        result.baseline_source = f"偏斜曲線 (skew curve, {curve.degree}次)"
    elif contract.expiry_type == "month":
        result.baseline_source = "ATM單點 (尚未接資料庫，暫代5日均)"
    else:
        result.baseline_source = "ATM單點"

    # 退掉的理由要講出來，不要靜默退回。baseline_source 是CLI跟前端都會顯示的欄位，
    # baseline_quality_note 在有曲線時不印。
    if result.db_baseline_stale_days is not None:
        how_old = (f"{result.db_baseline_stale_days}天前"
                   if result.db_baseline_stale_days >= 0 else "日期查不到")
        result.baseline_source += (
            f" ⚠ 資料庫5日均最新一筆是{how_old}"
            f"(超過{BASELINE_DB_MAX_STALE_DAYS}天，整組不採用；收盤寫入的排程可能斷了)"
        )

    # 7) 對整條鏈做判斷。基準優先序：資料庫5日均 > 偏斜曲線 > ATM單點
    #    ATM單點路徑用的範圍跟曲線配適同一個參數(--skew-range 一次放寬兩邊)
    atm_k_range = skew_k_range if skew_k_range is not None else ATM_BASELINE_K_RANGE
    qmap = {q.strike_price: q for q in quotes}
    for k in sorted(iv_map):
        q = qmap[k]

        baseline_iv = result.baseline_iv_atm
        out_of_range = False
        if curve is not None:
            baseline_iv = curve.iv_at(k)
            # 配適範圍外的基準是外插來的，不能當真
            # (不標記的話深價外會100%被誤判成偏貴，見 iv_skew.SkewCurve.in_range)
            out_of_range = not curve.in_range(k)
        else:
            # 沒有曲線(配不出來、或 is_reliable() 不過而被退回ATM單點)時，
            # 資料品質其實比有曲線時更差，守門不能反而消失 ——
            # 原本 out_of_range 只在 curve is not None 時才可能成立，
            # 於是「最該擋的情況」變成一個守門都沒有。
            out_of_range = not _within_atm_range(k, F, atm_k_range)
        if k in db_baseline:
            baseline_iv = db_baseline[k]     # 5日均不受配適範圍限制，可以信
            out_of_range = False

        verdict = evaluate_option(
            q.last_price, F, k, T, R, right_type, baseline_iv, threshold_pct=threshold_pct
        )
        # evaluate_option() 判不可信的理由只有一個：合理價小到百分比偏差失去意義。
        # 一定要在下面那行覆寫之前抓下來，不然兩個原因會混在一起分不開。
        tiny_fair = not verdict.reliable
        if out_of_range:
            verdict.reliable = False

        result.evals.append(StrikeEval(
            strike=k,
            market_price=q.last_price,
            price_source=q.price_source,
            volume=q.volume,
            quote_time=q.quote_time,
            age_sec=quote_age_seconds(q.quote_time),
            implied_vol=iv_map[k],
            baseline_iv=baseline_iv,
            fair_price=verdict.fair_price,
            deviation_pct=verdict.deviation_pct,
            status=verdict.status,
            reliable=verdict.reliable,
            out_of_range=out_of_range,
            tiny_fair=tiny_fair,
        ))

    return result


def pick_contract(
    months: list[ContractMonth],
    expire_month: Optional[str] = None,
    expiry_type: Optional[Literal["week", "month"]] = None,
) -> Optional[ContractMonth]:
    """
    從掛牌清單挑一個到期別。
      expire_month 有給就找那個代碼；沒給就挑最近到期的
      (可用 expiry_type 限定只看週選或月選)。找不到回傳 None。
    """
    if expire_month:
        return next((m for m in months if m.code == expire_month), None)
    pool = [m for m in months if expiry_type is None or m.expiry_type == expiry_type]
    if not pool:
        return None
    # 結算價已定的不能當預設值。期交所到期後不會馬上下架 —— 實測 2026-09-12
    # 掛牌清單裡第一個就是前一天到期的 202609F2，挑到它的話 analyze_chain()
    # 會直接丟 ContractSettledError，整個畫面預設就是一片錯誤訊息。
    # (使用者自己指定 expire_month 時不擋，那是他主動要看的，該讓他看到原因)
    alive = [m for m in pool if years_to_settlement(m.expiry_date) > 0]
    return min(alive or pool, key=lambda m: m.expiry_date)


if __name__ == "__main__":
    # 離線自我測試：用合成報價鏈直接跑 analyze_chain()(quotes 傳進去就不會連網)。
    # 管線跑的時候記得帶 PYTHONIOENCODING=utf-8，cp950 印不出 ✅。
    from datetime import datetime, timedelta
    from black76_iv import black76_price
    from db import IvHistoryRow

    F_TEST = 47300.0
    TRUE_IV = 0.20
    expiry = date.today() + timedelta(days=30)
    contract = ContractMonth(code=expiry.strftime("%Y%m"), expiry_date=expiry,
                             expiry_type="month", display_name="測試月選")
    T_TEST = years_to_settlement(expiry)

    def hhmmss_ago(sec: int) -> str:
        return (datetime.now() - timedelta(seconds=sec)).strftime("%H%M%S")

    def make_quote(k: float, iv: float = TRUE_IV, age: int = 30, vol: int = 10) -> RawLiveQuote:
        # 期交所最小跳動 0.1：深價外理論價 1e-10 在市場上會掛成 0.1，
        # 這樣才產生得出「IV反推得出來、但基準合理價 < 1」那種 tiny_fair 的檔
        price = max(round(black76_price(F_TEST, k, T_TEST, R, iv, "C"), 1), 0.1)
        return RawLiveQuote(strike_price=k, right_type="C", expiry_type="month",
                            expiry_date=expiry, bid=0.0, ask=0.0, last_price=price,
                            volume=vol, quote_time=hhmmss_ago(age), price_source="last")

    print("=" * 72)
    print("測試1(H)：不可信的兩個原因分開數，重疊的檔不能漏")
    print("=" * 72)
    wide = [make_quote(F_TEST + i * 500) for i in range(-50, 51)]
    res = analyze_chain(contract, "C", "day", F_TEST, quotes=wide, use_skew_curve=False)

    for e in res.evals:
        # 不可信 <=> 兩個原因至少一個成立(不會有「不可信但說不出為什麼」的檔)
        assert (not e.reliable) == (e.out_of_range or e.tiny_fair), e
    overlap = sum(1 for e in res.evals if e.out_of_range and e.tiny_fair)
    old_n_tiny = res.n_unreliable - res.n_out_of_range     # 修正前CLI的算法
    print(f"  評價{len(res.evals)}檔  不可信{res.n_unreliable}  "
          f"範圍外{res.n_out_of_range}  合理價過小{res.n_tiny}  重疊{overlap}")
    print(f"  舊算法 n_tiny = {res.n_unreliable} - {res.n_out_of_range} = {old_n_tiny}")
    assert overlap > 0, "合成資料沒有產生重疊的檔，這個測試測不到H"
    assert res.n_tiny == old_n_tiny + overlap, "n_tiny 應該剛好比舊算法多出重疊的那幾檔"
    assert res.n_out_of_range <= res.n_unreliable and res.n_tiny <= res.n_unreliable
    print("✅ 重疊的檔兩邊都有算到\n")

    print("=" * 72)
    print("測試2(H)：DB 5日均覆寫之後，out_of_range 要跟著取消、計數不能殘留")
    print("=" * 72)
    db = OptionDB.connect_sqlite(":memory:")
    db.create_tables()
    db.save_iv_history(
        IvHistoryRow(trade_date=date.today() - timedelta(days=d), session="day",
                     expiry_type="month", expiry_date=expiry, strike_price=q.strike_price,
                     right_type="C", underlying_price=F_TEST, close_price=q.last_price,
                     implied_vol=TRUE_IV)
        for q in wide for d in (1, 2, 3)
    )
    res_db = analyze_chain(contract, "C", "day", F_TEST, quotes=wide,
                           use_skew_curve=False, db=db)
    print(f"  基準: {res_db.baseline_source}")
    print(f"  不可信{res_db.n_unreliable}  範圍外{res_db.n_out_of_range}  合理價過小{res_db.n_tiny}")
    assert res_db.db_baseline_count > 0, "DB基準沒有被採用，測不到覆寫"
    assert res_db.n_out_of_range == 0, "全部履約價都有5日均，不該有任何檔還標範圍外"
    assert res_db.n_tiny == res_db.n_unreliable, "剩下的不可信只可能是合理價過小"
    print("✅ 覆寫後計數跟著歸零\n")

    print("=" * 72)
    print("測試3(F)：「不可信 + 合理」這一格確實會出現(前端必須給它 ? 徽章)")
    print("=" * 72)
    unknown = [e for e in res.evals if e.status == "fair" and not e.reliable]
    print(f"  status=fair 但 reliable=False 的有 {len(unknown)} 檔"
          f"(例：{unknown[0].strike:.0f}，範圍外={unknown[0].out_of_range})" if unknown else "")
    assert unknown, "合成資料沒產生這一格，前端的 ? 徽章測不到真實情境"
    print("✅ 後端會送出這一格，畫面不能把它畫成合理\n")

    print("=" * 72)
    print("測試4(G)：畫面標陳舊的報價不能被拿去當基準")
    print("=" * 72)
    assert BASELINE_MAX_AGE <= STALE_AGE, "基準池不能比畫面上的『可信』還寬鬆"
    # 價平那檔 700 秒前、IV 0.40(舊門檻900會收進基準池)，其他檔都新鮮、IV 0.20
    stale_age = (STALE_AGE + 900) // 2
    near = [make_quote(F_TEST + i * 100, iv=0.40 if i == 0 else TRUE_IV,
                       age=stale_age if i == 0 else 30)
            for i in range(-10, 11)]
    res_g = analyze_chain(contract, "C", "day", F_TEST, quotes=near, use_skew_curve=False)
    atm = next(e for e in res_g.evals if e.strike == F_TEST)
    print(f"  價平那檔 age={atm.age_sec}秒 stale={atm.stale}  "
          f"基準ATM IV={res_g.baseline_iv_atm:.4f} {res_g.baseline_quality_note}")
    assert atm.stale, "測試資料設錯：價平那檔應該被標成陳舊"
    assert abs(res_g.baseline_iv_atm - TRUE_IV) < 0.01, \
        "陳舊的價平報價被拿去當基準了(BASELINE_MAX_AGE 比 STALE_AGE 寬)"
    print("✅ 基準跳過陳舊的價平報價\n")

    print("全部通過")
