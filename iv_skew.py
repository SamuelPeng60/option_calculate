# -*- coding: utf-8 -*-
"""
iv_skew.py

波動率偏斜曲線(skew curve)配適
------------------------------------
解決的問題
------------------------------------
原本週選的baseline是「取ATM那一檔的IV，套用到整條鏈」。
但真實市場的IV本來就會隨履約價變化(這叫波動率偏斜/微笑)，實測台指選的數字：

    履約價 44100 → IV 0.3133
    履約價 44450 → IV 0.2707  (ATM)
    履約價 44800 → IV 0.2516

這是選擇權市場的正常結構，不是定價錯誤。但如果拿ATM的0.2707當整條鏈的基準，
44800就會被算成「便宜-12.6%」、44100被算成「偏貴+7.0%」——這兩個判斷都是假訊號，
它們只是位在微笑曲線的不同位置而已。

做法
------------------------------------
改成「對整條鏈的IV配一條平滑曲線，用曲線值當各履約價的baseline」，
判斷的就變成「這一檔偏離『它自己該有的IV水準』多少」，skew成分自動被吸收掉。

技術選擇與理由
------------------------------------
1. 在「對數價性」k = ln(K/F) 的空間配適，不是直接用履約價。
   理由：這樣曲線形狀跟指數點數高低無關(台指在17000或45000都適用同一組參數)，
   也是業界標準做法。ATM時 k=0，價外k>0，價內k<0。

2. 預設配適二次多項式 IV = a + b*k + c*k²。
   - 常數項a ≈ ATM的IV水準
   - 一次項b = 偏斜(skew)，台股通常是負的(價外賣權貴、價外買權便宜)
   - 二次項c = 微笑的彎曲程度
   只用3個參數，不會過度配適(overfit)。用cubic spline那種柔軟的曲線反而危險，
   它會把「真正的定價異常」也一起吸收進曲線裡，那就失去偵測的意義了。

3. 穩健配適(robust fitting)：先配一次 → 算殘差 → 把離群點剔掉 → 重配。
   這一步是必要的，不是加分項：實測發現低流動性合約的報價可能是好幾小時前的，
   IV會亂跳(看過同一條鏈裡出現0.1966和0.2912)。
   如果不剔除離群值，這些爛資料會把曲線拉歪，那就變成「用被污染的基準去比」，
   跟原本ATM基準被污染是同一種錯誤。

4. 用MAD(絕對中位差)estimate離群門檻，不用標準差。
   理由：標準差本身就會被離群值影響(離群值越誇張、標準差越大、越抓不出離群值)，
   MAD對離群值不敏感，這是穩健統計的標準做法。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np


@dataclass
class SkewPoint:
    """配適用的單點輸入"""
    strike_price: float
    implied_vol: float
    weight: float = 1.0     # 報價品質權重，越可靠越大


@dataclass
class SkewCurve:
    """配適完成的偏斜曲線"""
    coeffs: list[float]              # 多項式係數(numpy慣例：高次在前)
    F: float                         # 配適當下的標的期貨價
    degree: int
    n_used: int                      # 實際參與配適的點數
    n_dropped: int                   # 被當成離群值剔除的點數
    rmse: float                      # 配適殘差(參與配適的點)
    k_min: float                     # 配適資料涵蓋的log-moneyness範圍
    k_max: float
    iv_min: float                    # 觀測到的IV範圍(用來夾住外插值)
    iv_max: float
    dropped_strikes: list[float] = field(default_factory=list)

    def iv_at(self, strike: float) -> float:
        """
        回傳某履約價在曲線上的IV。

        超出配適範圍時會外插，但多項式外插很容易噴出離譜的值(甚至負的)，
        所以這裡做了兩層保護：
          1. k夾在配適範圍內(等於用邊界值當外插，比讓二次曲線自由發散安全)
          2. 算出來的IV再夾在實際觀測到的IV範圍內
        """
        if strike <= 0 or self.F <= 0:
            return float("nan")

        k = math.log(strike / self.F)
        k = max(self.k_min, min(self.k_max, k))      # 保護1：不讓它外插太遠

        iv = float(np.polyval(self.coeffs, k))
        iv = max(self.iv_min, min(self.iv_max, iv))  # 保護2：夾在觀測範圍
        return iv

    def in_range(self, strike: float) -> bool:
        """
        這個履約價是否落在曲線「實際配適過」的範圍內。

        為什麼需要這個(實測踩到的坑)：
        配適時會用 k_range 限制範圍(避免深價內那些IV高達1.37的僵滯報價把曲線拉歪)，
        但 iv_at() 對範圍外的履約價是「把k夾在邊界」再算，那等於外插。

        實測發現這樣會產生系統性誤判：台指選深度價外的IV其實會回升(微笑的翹尾，
        實測價外深區均IV 0.3027 > 近價外區 0.2888)，但配出來的曲線斜率是負的、
        夾在邊界的基準偏低，結果深價外 37 檔100%全被標成「偏貴」——全是假訊號。

        外插區本來就是多項式最不可信的地方。所以呼叫端應該用這個函式判斷，
        範圍外的合約標成「無法判斷」，而不是拿夾住的值假裝那是有效基準。
        """
        if strike <= 0 or self.F <= 0:
            return False
        k = math.log(strike / self.F)
        return self.k_min <= k <= self.k_max

    def atm_iv(self) -> float:
        """曲線在價平(k=0)的值，也就是常數項"""
        return self.iv_at(self.F)

    @property
    def skew_slope(self) -> float:
        """
        一次項係數 = 偏斜斜率(IV對log-moneyness的變化率)。
        台股指數選擇權通常是負的：越價外的買權IV越低。
        """
        # np.polyval的係數是高次在前，一次項在倒數第二個
        return float(self.coeffs[-2]) if len(self.coeffs) >= 2 else 0.0

    def is_reliable(self, min_points: int = 6, max_rmse: float = 0.05) -> bool:
        """
        這條曲線可不可信。不可信時呼叫端應該退回用ATM單點基準(並在畫面標示)。
        """
        return self.n_used >= min_points and self.rmse <= max_rmse


def quality_weight(
    volume: int = 0,
    age_sec: Optional[int] = None,
    half_life_sec: float = 900.0,
) -> float:
    """
    把「成交量」與「報價新鮮度」換算成配適權重。

    - 成交量用 log1p：成交1000口比100口可靠，但不是可靠10倍，取對數壓縮差距
    - 新鮮度用指數衰減：每過 half_life_sec 權重減半
      (實測看過6小時前的報價，那種要幾乎完全不採信)
    - 沒有時間資訊時(age_sec=None)給一個中性的折扣，不完全信任也不完全丟棄
    """
    w = math.log1p(max(volume, 0)) + 0.1      # +0.1 讓零成交量的點還有一點點權重

    if age_sec is None:
        w *= 0.3
    else:
        w *= 0.5 ** (max(age_sec, 0) / half_life_sec)

    return max(w, 1e-6)


def fit_skew_curve(
    points: Sequence[SkewPoint],
    F: float,
    degree: int = 2,
    max_iter: int = 3,
    outlier_mad_mult: float = 3.0,
    min_points: int = 6,
    k_range: Optional[float] = None,
) -> Optional[SkewCurve]:
    """
    配適偏斜曲線。點數不足或資料退化時回傳None(呼叫端要退回ATM基準)。

    參數：
      points            : 每個履約價的IV與品質權重
      F                 : 標的期貨價(算log-moneyness用)
      degree            : 多項式次數，預設2(二次微笑)
      max_iter          : 穩健重配次數
      outlier_mad_mult  : 殘差超過幾倍MAD就當離群值剔除
      min_points        : 至少要幾個點才配(太少配出來沒意義)
      k_range           : 只用 |ln(K/F)| <= k_range 的點配適。
                          深度價外的IV雜訊很大，限制範圍可以讓曲線更貼近有意義的區間。
                          None代表不限制。
    """
    if F <= 0:
        return None

    # 整理有效點
    ks: list[float] = []
    ivs: list[float] = []
    ws: list[float] = []
    strikes: list[float] = []

    for p in points:
        if p.strike_price <= 0 or p.implied_vol is None:
            continue
        if not (0 < p.implied_vol < 5):     # IV超過500%當異常
            continue
        k = math.log(p.strike_price / F)
        if k_range is not None and abs(k) > k_range:
            continue
        if not math.isfinite(k) or p.weight <= 0:
            continue
        ks.append(k)
        ivs.append(float(p.implied_vol))
        ws.append(float(p.weight))
        strikes.append(float(p.strike_price))

    if len(ks) < max(min_points, degree + 1):
        return None

    k_arr = np.asarray(ks, dtype=float)
    iv_arr = np.asarray(ivs, dtype=float)
    w_arr = np.asarray(ws, dtype=float)
    s_arr = np.asarray(strikes, dtype=float)

    active = np.ones(len(k_arr), dtype=bool)
    dropped_strikes: list[float] = []
    coeffs = None

    for _ in range(max(1, max_iter)):
        if active.sum() < degree + 1:
            break

        # numpy的polyfit權重是「乘在殘差上」，所以要開根號才等價於一般的加權最小平方
        try:
            coeffs = np.polyfit(
                k_arr[active], iv_arr[active], degree, w=np.sqrt(w_arr[active])
            )
        except Exception:
            return None

        resid = iv_arr - np.polyval(coeffs, k_arr)

        # 用MAD估離群門檻(對離群值不敏感，見檔頭說明)
        med = np.median(resid[active])
        mad = np.median(np.abs(resid[active] - med))
        # 0.6745 是把MAD換算成常態分布下標準差的係數
        sigma = mad / 0.6745 if mad > 0 else float(np.std(resid[active]))
        if sigma <= 0:
            break

        new_active = active & (np.abs(resid - med) <= outlier_mad_mult * sigma)

        # 不能剔到點數不夠，也沒有變化就停
        if new_active.sum() < max(min_points, degree + 1):
            break
        if np.array_equal(new_active, active):
            break
        active = new_active

    if coeffs is None or active.sum() < degree + 1:
        return None

    # 最後用剩下的點再配一次，確保係數跟active集合一致
    try:
        coeffs = np.polyfit(k_arr[active], iv_arr[active], degree, w=np.sqrt(w_arr[active]))
    except Exception:
        return None

    resid_final = iv_arr[active] - np.polyval(coeffs, k_arr[active])
    rmse = float(np.sqrt(np.mean(resid_final ** 2)))

    dropped_strikes = sorted(float(x) for x in s_arr[~active])

    return SkewCurve(
        coeffs=[float(c) for c in coeffs],
        F=float(F),
        degree=degree,
        n_used=int(active.sum()),
        n_dropped=int((~active).sum()),
        rmse=rmse,
        k_min=float(k_arr[active].min()),
        k_max=float(k_arr[active].max()),
        iv_min=float(iv_arr[active].min()),
        iv_max=float(iv_arr[active].max()),
        dropped_strikes=dropped_strikes,
    )


# ---------------------------------------------------------------------------
# 自我測試(不需網路)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random

    print("=" * 72)
    print("測試1：能不能從有雜訊的資料裡還原出已知的偏斜曲線")
    print("=" * 72)

    random.seed(42)
    F_true = 45000.0
    # 設定一條已知的「真實」曲線：ATM IV=0.27，負偏斜，帶一點微笑
    a_true, b_true, c_true = 0.27, -0.35, 1.2

    def true_iv(K: float) -> float:
        k = math.log(K / F_true)
        return a_true + b_true * k + c_true * k * k

    pts = []
    for i in range(-15, 16):
        K = F_true + i * 100
        iv = true_iv(K) + random.uniform(-0.003, 0.003)   # 加小雜訊
        pts.append(SkewPoint(K, iv, weight=1.0))

    curve = fit_skew_curve(pts, F_true)
    assert curve is not None, "配適失敗"
    print(f"  真實參數: a={a_true:.4f} b={b_true:.4f} c={c_true:.4f}")
    print(f"  配適結果: a={curve.coeffs[2]:.4f} b={curve.coeffs[1]:.4f} c={curve.coeffs[0]:.4f}")
    print(f"  ATM IV: 真實={a_true:.4f} 配適={curve.atm_iv():.4f}")
    print(f"  RMSE={curve.rmse:.5f}  使用{curve.n_used}點")
    assert abs(curve.atm_iv() - a_true) < 0.005, "ATM IV還原誤差過大"
    assert abs(curve.skew_slope - b_true) < 0.05, "偏斜斜率還原誤差過大"
    print("✅ 能正確還原已知曲線\n")

    print("=" * 72)
    print("測試2：離群值(模擬陳舊報價)會不會把曲線拉歪")
    print("=" * 72)

    pts_dirty = list(pts)
    # 塞進3個離譜的點，模擬「幾小時前的僵滯報價」
    bad = [(44700.0, 0.196), (44800.0, 0.412), (45300.0, 0.155)]
    for K, iv in bad:
        pts_dirty.append(SkewPoint(K, iv, weight=1.0))

    curve_dirty = fit_skew_curve(pts_dirty, F_true)
    assert curve_dirty is not None
    print(f"  塞入 {len(bad)} 個離群點: {[f'K={k:.0f} IV={v}' for k, v in bad]}")
    print(f"  剔除了 {curve_dirty.n_dropped} 個點: {[f'{s:.0f}' for s in curve_dirty.dropped_strikes]}")
    print(f"  ATM IV: 真實={a_true:.4f} 配適={curve_dirty.atm_iv():.4f}")

    # 對照組：不做穩健剔除(max_iter=1且門檻放到無限大)
    curve_naive = fit_skew_curve(pts_dirty, F_true, max_iter=1, outlier_mad_mult=1e9)
    print(f"  若不剔除離群值，ATM IV會變成 {curve_naive.atm_iv():.4f} "
          f"(偏離 {abs(curve_naive.atm_iv() - a_true):.4f})")

    assert abs(curve_dirty.atm_iv() - a_true) < 0.01, "穩健配適沒擋住離群值"
    assert curve_dirty.n_dropped >= 2, "應該要剔除掉那些離群點"
    assert abs(curve_dirty.atm_iv() - a_true) < abs(curve_naive.atm_iv() - a_true), \
        "穩健配適應該要比不剔除更接近真實值"
    print("✅ 穩健配適成功擋掉離群值\n")

    print("=" * 72)
    print("測試3：ATM單點基準 vs 曲線基準，對skew的處理差異")
    print("=" * 72)

    atm_baseline = true_iv(F_true)   # 用ATM那一檔的IV當整條鏈基準(舊做法)
    print(f"  {'履約價':>8} {'實際IV':>8} {'ATM基準':>9} {'舊偏差':>9} {'曲線基準':>9} {'新偏差':>9}")
    max_old = max_new = 0.0
    for K in (44300, 44600, 45000, 45400, 45700):
        iv = true_iv(K)
        old_dev = (iv - atm_baseline) / atm_baseline * 100
        new_dev = (iv - curve.iv_at(K)) / curve.iv_at(K) * 100
        max_old = max(max_old, abs(old_dev))
        max_new = max(max_new, abs(new_dev))
        print(f"  {K:>8} {iv:>8.4f} {atm_baseline:>9.4f} {old_dev:>8.1f}% "
              f"{curve.iv_at(K):>9.4f} {new_dev:>8.1f}%")
    print(f"\n  這批資料本身沒有任何定價異常(完全照真實曲線生成)，")
    print(f"  理想情況下所有偏差都該接近0%：")
    print(f"    舊做法(ATM單點)最大偏差 = {max_old:.1f}%  ← 全是skew造成的假訊號")
    print(f"    新做法(曲線)  最大偏差 = {max_new:.1f}%")
    assert max_new < 1.0, "曲線基準不該產生假訊號"
    assert max_new < max_old, "曲線基準應該要優於ATM單點基準"
    print("✅ 曲線基準成功消除skew造成的假訊號\n")

    print("=" * 72)
    print("測試4：真正的定價異常仍然要被抓出來")
    print("=" * 72)

    pts_mis = list(pts)
    # 把某一檔的IV硬拉高15%，模擬「這檔真的被買貴了」
    K_mis = 45200.0
    iv_mis = true_iv(K_mis) * 1.15
    pts_mis = [p for p in pts_mis if p.strike_price != K_mis]
    pts_mis.append(SkewPoint(K_mis, iv_mis, weight=1.0))

    curve_mis = fit_skew_curve(pts_mis, F_true)
    dev = (iv_mis - curve_mis.iv_at(K_mis)) / curve_mis.iv_at(K_mis) * 100
    print(f"  K={K_mis:.0f} 被人為拉高15%: IV={iv_mis:.4f} vs 曲線={curve_mis.iv_at(K_mis):.4f}")
    print(f"  曲線基準算出的偏差 = {dev:.1f}%  (應該要明顯>0，代表有抓到)")
    assert dev > 8, "真正的定價異常應該要被偵測出來"
    print("✅ 曲線基準仍能抓出真正的異常(沒有把異常一起吸收掉)\n")

    print("=" * 72)
    print("測試5：品質權重與邊界處理")
    print("=" * 72)

    w_fresh_liquid = quality_weight(volume=1000, age_sec=10)
    w_fresh_thin = quality_weight(volume=1, age_sec=10)
    w_stale_liquid = quality_weight(volume=1000, age_sec=6 * 3600)
    w_unknown = quality_weight(volume=100, age_sec=None)
    print(f"  新鮮+大量(1000口,10秒)  權重={w_fresh_liquid:.4f}")
    print(f"  新鮮+稀少(1口,10秒)     權重={w_fresh_thin:.4f}")
    print(f"  陳舊+大量(1000口,6小時) 權重={w_stale_liquid:.6f}")
    print(f"  無時間資訊(100口)       權重={w_unknown:.4f}")
    assert w_fresh_liquid > w_fresh_thin, "成交量大的應該權重高"
    assert w_fresh_liquid > w_stale_liquid, "新鮮的應該權重高"
    assert w_stale_liquid < 0.01, "6小時前的報價權重應該要極低"

    assert fit_skew_curve([], 45000.0) is None, "空輸入應回傳None"
    assert fit_skew_curve(pts, 0) is None, "F<=0應回傳None"
    few = [SkewPoint(45000 + i * 100, 0.27, 1.0) for i in range(3)]
    assert fit_skew_curve(few, 45000.0) is None, "點數不足應回傳None"

    # 外插保護：遠離配適範圍不該噴出負值或離譜值
    far_low = curve.iv_at(1000.0)
    far_high = curve.iv_at(999999.0)
    print(f"  極端外插: K=1000 → IV={far_low:.4f} / K=999999 → IV={far_high:.4f}")
    assert curve.iv_min <= far_low <= curve.iv_max, "外插值應被夾在觀測範圍內"
    assert curve.iv_min <= far_high <= curve.iv_max, "外插值應被夾在觀測範圍內"
    print("✅ 品質權重與邊界處理正確\n")

    print("=" * 72)
    print("全部測試通過 ✅")
    print("=" * 72)
