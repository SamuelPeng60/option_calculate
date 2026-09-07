# -*- coding: utf-8 -*-
"""
black76_iv.py

台指選擇權(TXO) 核心計算模組
------------------------------------
用途：
1. 用 Black-76 模型，由 (台指期點數, 履約價, 到期時間, 無風險利率, IV) 算出理論價格
2. 由 (市場成交/報價價格) 反推隱含波動率 (Implied Volatility)

為什麼用 Black-76 而不是標準 Black-Scholes？
- TXO 選擇權你要求的定價基準是「台指期點數」(F)，Black-76 就是專門為
  「標的是期貨價格」設計的模型，不需要額外處理股利殖利率(q)，
  因為期貨價格本身已經反映了持有成本(cost of carry)。
- 這跟你在前面確認的「根據目前的台指期點數即時更新」的設計是一致的。

模型假設（先講清楚，之後有需要再調整）：
- 歐式選擇權（TXO 本身就是歐式，符合）
- 無風險利率先給一個可調參數，你可以用央行短率或固定電匯利率近似值代入
- 沒有處理美式提前履約（TXO是歐式，不需要）
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
import math
from scipy.stats import norm
from scipy.optimize import brentq

OptionType = Literal["C", "P"]


# ---------------------------------------------------------------------------
# 1. Black-76 定價公式
# ---------------------------------------------------------------------------

def black76_price(
    F: float,          # 台指期點數 (underlying futures price)
    K: float,           # 履約價
    T: float,            # 到期時間，用「年」為單位 (例如 7天到期 = 7/365)
    r: float,             # 無風險利率 (年化，例如 0.015 代表 1.5%)
    sigma: float,          # 波動率 (年化，例如 0.15 代表 15%)
    option_type: OptionType,  # "C" or "P"
) -> float:
    """回傳 Black-76 理論價格（選擇權點數，尚未乘上契約乘數）"""
    if T <= 0 or sigma <= 0:
        # 到期或波動率為0時，回傳內含價值(避免除以0)
        intrinsic = max(F - K, 0.0) if option_type == "C" else max(K - F, 0.0)
        return intrinsic

    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    discount = math.exp(-r * T)

    if option_type == "C":
        return discount * (F * norm.cdf(d1) - K * norm.cdf(d2))
    elif option_type == "P":
        return discount * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
    else:
        raise ValueError(f"option_type 必須是 'C' 或 'P'，收到: {option_type}")


def black76_vega(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega：價格對波動率的敏感度，反推IV時要用它輔助收斂判斷/加速"""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return F * math.exp(-r * T) * norm.pdf(d1) * math.sqrt(T)


# ---------------------------------------------------------------------------
# 2. 反推隱含波動率 (Implied Volatility)
# ---------------------------------------------------------------------------

@dataclass
class IVResult:
    iv: float | None          # 反推出的年化IV，失敗時為 None
    converged: bool           # 是否成功收斂
    reason: str = ""          # 失敗時的原因說明


def implied_vol(
    market_price: float,
    F: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType,
    vol_bounds: tuple[float, float] = (0.001, 5.0),  # IV搜尋範圍 0.1% ~ 500%
) -> IVResult:
    """
    用 brentq (二分逼近法的加強版) 反推IV。
    這個方法比 Newton-Raphson 更穩，不會因為初始值沒選好而發散，
    適合放進每分鐘跑一次、要處理幾十檔合約的排程裡，不用擔心單一檔卡住整批。
    """
    if T <= 0:
        return IVResult(None, False, "已到期或到期時間<=0，無法反推IV")
    if market_price <= 0:
        return IVResult(None, False, "市場價格<=0，無法反推IV")

    # 檢查市場價格是否低於「理論價格下界」(低於下界代表不管IV設多少都算不出這個價格，
    # 通常是報價異常或資料過舊)。
    #
    # ⚠ 這裡的下界必須是「折現後」的內含價值，不能用未折現的 max(F-K, 0)：
    #   Black-76 在 sigma→0 時，價格會收斂到 exp(-r*T) * max(F-K, 0)，
    #   而不是 max(F-K, 0)。因為期貨式選擇權的payoff是在「到期日」才交割，
    #   要折現回今天，所以理論最低價本來就比未折現的內含價值低一些。
    #
    #   用未折現的當下界會誤判：天期越長折現差距越大(用r=1.5%實測，
    #   1天差0.4點、30天差12點、210天差87點)，那些價格明明有合法IV的
    #   長天期深價內合約會被錯誤地當成「資料異常」而被丟掉。
    #   (實測台指選全到期別共5檔受影響，都是遠月深價內)
    intrinsic = max(F - K, 0.0) if option_type == "C" else max(K - F, 0.0)
    price_floor = math.exp(-r * T) * intrinsic
    if market_price < price_floor - 1e-6:
        return IVResult(
            None, False,
            f"市場價格({market_price})低於理論價格下界({price_floor:.4f}，"
            f"即折現後內含價值)，資料可能異常"
        )

    def objective(sigma: float) -> float:
        return black76_price(F, K, T, r, sigma, option_type) - market_price

    lo, hi = vol_bounds
    try:
        f_lo, f_hi = objective(lo), objective(hi)
        if f_lo * f_hi > 0:
            # 邊界內找不到根，通常代表報價本身有問題(例如買賣價差異常大、成交價過舊)
            return IVResult(None, False, "在搜尋範圍內找不到收斂的IV，報價可能過舊或異常")
        iv = brentq(objective, lo, hi, xtol=1e-6, maxiter=100)
        return IVResult(round(iv, 6), True)
    except Exception as e:
        return IVResult(None, False, f"求解失敗: {e}")


# ---------------------------------------------------------------------------
# 3. 依「合理IV」算出合理價格 + 判斷貴/合理/便宜
# ---------------------------------------------------------------------------

# 合理價低於這個點數時，「百分比偏差」就失去意義。
# 台指選最小跳動是0.1點，合理價算出來只有0.001點的深度價外合約，
# 市場掛0.1點就等於「貴100倍」，但那不是定價異常，只是價外樂透票的正常現象。
MIN_COMPARABLE_FAIR_PRICE = 1.0

# 偏差百分比的上限。真正有意義的訊號不會超過這個數量級，
# 設上限是為了避免極端值污染資料庫欄位跟前端畫面。
MAX_DEVIATION_PCT = 999.99


@dataclass
class PricingVerdict:
    market_price: float
    fair_price: float
    baseline_iv: float
    deviation_pct: float          # (market - fair) / fair * 100，已限制在±999.99
    status: Literal["expensive", "fair", "cheap"]
    # 合理價太小、百分比失去意義時為False。
    # 預設True是為了讓既有呼叫端(run_demo_pipeline等)不用改也能跑。
    reliable: bool = True


def evaluate_option(
    market_price: float,
    F: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType,
    baseline_iv: float,      # 你資料庫算好的「5日IV移動平均」或「當週ATM IV」
    threshold_pct: float = 0.05,  # 目前先用5%
    min_comparable_fair: float = MIN_COMPARABLE_FAIR_PRICE,
) -> PricingVerdict:
    fair_price = black76_price(F, K, T, r, baseline_iv, option_type)

    # ⚠ 這裡的防護不能只寫 `fair_price <= 0`(實測踩到的坑)：
    #   深度價外又快到期的合約，理論價會是「極小的正數」而不是0。
    #   實測 K=55000、F=45085、剩1天 的買權，理論價是 3.0e-35，
    #   通過了 <=0 的檢查，接著除下去就變成 3.3e+35 這種天文數字——
    #   任何DECIMAL欄位都存不下，前端也只會顯示一堆亂七八糟的數字。
    #
    #   所以改成「合理價小於一個有意義的門檻就不算百分比」，
    #   並且無論如何都把結果夾在±999.99%以內。
    reliable = True
    if fair_price < min_comparable_fair:
        reliable = False
        if fair_price <= 0:
            deviation = 0.0
        else:
            # 仍然算出方向(市價比理論價高還是低)，但百分比不可信，
            # 呼叫端要看 reliable 決定要不要採用
            deviation = (market_price - fair_price) / fair_price
    else:
        deviation = (market_price - fair_price) / fair_price

    if deviation > threshold_pct:
        status = "expensive"
    elif deviation < -threshold_pct:
        status = "cheap"
    else:
        status = "fair"

    deviation_pct = max(-MAX_DEVIATION_PCT, min(MAX_DEVIATION_PCT, deviation * 100))

    return PricingVerdict(
        market_price=market_price,
        fair_price=round(fair_price, 2),
        baseline_iv=baseline_iv,
        deviation_pct=round(deviation_pct, 2),
        status=status,
        reliable=reliable,
    )


# ---------------------------------------------------------------------------
# 自我測試：確保 定價 <-> 反推IV 兩個方向對得起來
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    F = 17500       # 假設台指期點數
    K = 17500        # 價平履約價
    T = 7 / 365        # 7天後到期
    r = 0.015            # 無風險利率 1.5%
    true_sigma = 0.15      # 假設真實IV是15%

    # Step 1: 用已知IV算出理論價格
    call_price = black76_price(F, K, T, r, true_sigma, "C")
    put_price = black76_price(F, K, T, r, true_sigma, "P")
    print(f"[定價測試] Call理論價={call_price:.2f}  Put理論價={put_price:.2f}")

    # Step 2: 拿這個價格反推IV，應該要接近 0.15
    call_iv = implied_vol(call_price, F, K, T, r, "C")
    put_iv = implied_vol(put_price, F, K, T, r, "P")
    print(f"[反推IV測試] Call反推IV={call_iv.iv}  (應接近 0.15)")
    print(f"[反推IV測試] Put反推IV={put_iv.iv}  (應接近 0.15)")

    assert abs(call_iv.iv - true_sigma) < 1e-4, "Call IV反推誤差過大！"
    assert abs(put_iv.iv - true_sigma) < 1e-4, "Put IV反推誤差過大！"
    print("\n✅ 定價與反推IV 互相驗證通過")

    # Step 3: 模擬「市場報價比理論價貴」的情境，測試貴/便宜判斷
    market_price_expensive = call_price * 1.08   # 市場價比理論價貴8%
    verdict = evaluate_option(
        market_price=market_price_expensive,
        F=F, K=K, T=T, r=r, option_type="C",
        baseline_iv=true_sigma,
        threshold_pct=0.05,
    )
    print(f"\n[貴/便宜判斷測試] 市場價={verdict.market_price:.2f} "
          f"合理價={verdict.fair_price} 偏差={verdict.deviation_pct}% "
          f"狀態={verdict.status}")
    assert verdict.status == "expensive"
    print("✅ 貴/合理/便宜判斷邏輯測試通過")

    # Step 4: 價格下界的邊界測試
    #   重點：長天期深價內合約，市場價落在「折現後內含價值」與「未折現內含價值」之間時，
    #   是合法報價(存在對應的IV)，不可以被當成資料異常擋掉。
    print("\n[價格下界測試]")
    F_d, K_d, r_d = 45000.0, 35000.0, 0.015
    for days in (1, 30, 210):
        T_d = days / 365
        undiscounted = F_d - K_d                       # 未折現內含價值
        floor = math.exp(-r_d * T_d) * undiscounted    # 正確的理論下界

        # (a) 剛好在下界之上一點點 → 應該要能反推出IV
        ok_price = floor + max(undiscounted * 1e-6, 0.01)
        res_ok = implied_vol(ok_price, F_d, K_d, T_d, r_d, "C")

        # (b) 明顯低於下界 → 應該被判定為異常
        bad_price = floor * 0.99
        res_bad = implied_vol(bad_price, F_d, K_d, T_d, r_d, "C")

        gap = undiscounted - floor
        print(f"  {days:>3}天: 未折現內含={undiscounted:.2f} 折現後下界={floor:.2f} "
              f"(差{gap:.2f}點) | 下界之上→{'可反推' if res_ok.converged else '被擋掉'} "
              f"| 低於下界→{'可反推' if res_bad.converged else '正確擋掉'}")

        assert res_ok.converged, (
            f"{days}天: 價格{ok_price:.2f}在理論下界之上，應該要能反推IV卻失敗了"
            f"(原因: {res_ok.reason})"
        )
        assert not res_bad.converged, f"{days}天: 價格低於理論下界，應該要被擋掉"

    # 用未折現內含價值當下界的話，這個價格會被誤判成異常(修正前的行為)
    T_long = 210 / 365
    between = math.exp(-r_d * T_long) * 10000 + 50   # 介於折現下界與未折現內含價值之間
    assert between < 10000, "測試前提: 這個價格應低於未折現內含價值"
    res_between = implied_vol(between, F_d, K_d, T_long, r_d, "C")
    assert res_between.converged, "介於折現下界與未折現內含價值之間的合法報價被誤擋"
    print(f"  ✓ 價格{between:.2f}(低於未折現內含10000但高於折現下界) "
          f"→ 正確反推出IV={res_between.iv}")
    print("✅ 價格下界測試通過")

    # Step 5: 深度價外的「除以極小數」防護
    #   實測真實資料時發現：深價外又快到期的合約，理論價是極小的正數(不是0)，
    #   舊的 fair_price<=0 防護擋不住，除下去會爆出 1e+35 這種數字。
    print("\n[深度價外防護測試]")
    F_o, r_o, T_o = 45085.0, 0.015, 1 / 365
    K_o = 55000.0        # 遠遠價外
    fair_tiny = black76_price(F_o, K_o, T_o, r_o, 0.30, "C")
    print(f"  K={K_o:.0f} F={F_o} 剩1天 → 理論價={fair_tiny:.3e} (極小正數，不是0)")
    assert 0 < fair_tiny < 1e-10, "測試前提: 理論價應該是極小的正數"

    v_tiny = evaluate_option(0.1, F_o, K_o, T_o, r_o, "C", 0.30)
    print(f"  市場價0.1 → 偏差={v_tiny.deviation_pct}%  reliable={v_tiny.reliable}")
    assert abs(v_tiny.deviation_pct) <= MAX_DEVIATION_PCT, "偏差必須被夾在上限內"
    assert not v_tiny.reliable, "合理價過小時應標記為不可信"

    # 正常的合約不該被誤標成不可信
    v_normal = evaluate_option(300.0, 45085.0, 45000.0, 29 / 365, r_o, "C", 0.28)
    print(f"  正常合約(K=45000,29天) 偏差={v_normal.deviation_pct}% reliable={v_normal.reliable}")
    assert v_normal.reliable, "正常合約不該被標記為不可信"

    # 邊界：剛好在門檻附近
    for fair_target, want_reliable in ((0.5, False), (2.0, True)):
        # 反推一個能產生指定理論價的情境不容易，直接用參數控制門檻來驗證邏輯
        v = evaluate_option(1.0, F_o, 45000.0, 29 / 365, r_o, "C", 0.28,
                            min_comparable_fair=1e9 if not want_reliable else 0.01)
        assert v.reliable == want_reliable
    print("  ✓ 門檻邏輯正確，且偏差一律夾在 ±999.99% 內")
    print("✅ 深度價外防護測試通過")
