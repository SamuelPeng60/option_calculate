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
    fetch_live_quotes,
    quote_age_seconds,
)

R = 0.015              # 無風險利率假設
THRESHOLD_PCT = 0.05   # 偏離超過±5%才算貴/便宜

# 挑基準IV時的報價品質門檻。
# 這兩個數字是實測踩坑後定的：夜盤週選最接近價平的那檔是「3.6小時前、成交1口」，
# 拿它當基準會讓整條鏈幾乎每檔都被誤標成偏貴。詳見 README「基準IV本身也會被污染」。
BASELINE_MAX_AGE = 900   # 挑基準用的報價，最多接受15分鐘前的
BASELINE_MIN_VOL = 1     # 且至少要有成交量

STALE_AGE = 600          # 超過這個秒數的報價視為陳舊(CLI標*，前端淡化顯示)

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
    n_out_of_range: int = 0

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
    T = max(days_to_expiry, 1) / 365

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

        # ⚠ 計數器一定要等 db_baseline 覆寫完才加。
        #   原本是在 in_range 判斷當下就 += 1，但下面 db_baseline 又把旗標取消掉，
        #   計數器卻沒退回去 —— 實測121檔全鏈、DB有全部履約價的5日均時，
        #   n_out_of_range=47 但 n_unreliable=0，於是 CLI 的
        #   n_tiny = n_unreliable - n_out_of_range 變成 -47，
        #   「合理價過小」那個真正的原因永遠印不出來，畫面上還會謊報47檔在配適範圍外。
        if out_of_range:
            result.n_out_of_range += 1

        verdict = evaluate_option(
            q.last_price, F, k, T, R, right_type, baseline_iv, threshold_pct=threshold_pct
        )
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
    return min(pool, key=lambda m: m.expiry_date)
