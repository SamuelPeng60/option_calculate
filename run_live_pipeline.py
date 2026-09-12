# -*- coding: utf-8 -*-
"""
run_live_pipeline.py

端對端 pipeline — 真實資料版
------------------------------------
跟 run_demo_pipeline.py 是同一條流程，差別只在資料來源：
  run_demo_pipeline.py → mock_live_quotes.py  (假資料，離線可跑，驗證計算邏輯)
  run_live_pipeline.py → taifex_fetch.py      (期交所真實即時報價)

流程：
  抓台指期點數 → 抓選擇權報價鏈 → 每檔反推IV → 取baseline_iv
  → 算合理價 → 判斷貴/合理/便宜 → 印出表格

用法：
  python run_live_pipeline.py                    # 自動判斷盤別，抓最近月的月選Call
  python run_live_pipeline.py --right P          # 看賣權
  python run_live_pipeline.py --expiry 202608W4  # 指定到期別(週選)
  python run_live_pipeline.py --list             # 只列出目前掛牌的到期別
  python run_live_pipeline.py --session night    # 強制用夜盤資料
  python run_live_pipeline.py --min-volume 10 --max-age 300
                                                 # 只看有成交量、且報價在5分鐘內的合約
                                                 # (盤中建議這樣用，理由見下)

資料品質提醒：
  期交所API給的是「最後成交價」，但不會告訴你那是多久以前成交的。低流動性的履約價
  可能拿到好幾小時前的價格，那期間台指期已經跑掉一段，反推出來的IV沒有參考價值。
  表格裡的「報價年齡」欄位會顯示每檔的實際新鮮度，超過10分鐘會標*。

注意：這支程式會實際對外連 mis.taifex.com.tw，需要網路。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time as dtime
from typing import Literal, Optional

# 計算流程本身放在 pricing_service.py，這支程式只負責「印成表格」。
# 抽出去的原因：網頁後端(serve_dashboard.py)要用同一套邏輯，
# 兩邊各寫一份的話，基準IV優先序那些踩坑調出來的規則遲早會走鐘。
# 註：月選的5日均是由 db.get_baseline_iv_batch() 批次撈(一次SQL撈完整條鏈)，
# 不是逐檔呼叫 iv_baseline.get_monthly_baseline_iv()，那樣每分鐘會打幾百次DB往返。
from pricing_service import (
    analyze_chain,
    pick_contract,
    NoQuoteDataError,
    STALE_AGE,
    R,               # 這兩個常數搬到 pricing_service 了，
    THRESHOLD_PCT,   # 這裡重新匯出，原本 from run_live_pipeline import R 的寫法不會壞
)
from db import OptionDB, LiveQuoteRow
from taifex_fetch import (
    list_contract_months,
    fetch_underlying_futures_price,
    TaifexApiError,
)


# ---------------------------------------------------------------------------
# 盤別判斷
# ---------------------------------------------------------------------------
# README裡列為「還沒做」的項目之一。這裡先給一個直接照交易時間判斷的版本，
# 因為要抓即時報價一定得先決定 MarketType(0=日盤/1=夜盤)，不能不處理。
#
# 台指選擇權交易時間：
#   日盤 08:45 - 13:45
#   夜盤 15:00 - 次日 05:00   ← 會跨日，這是最容易寫錯的地方
#
# 週末已處理(見 is_market_open)。注意凌晨那段的歸屬：00:00~05:00 是「前一天」15:00
# 開盤的夜盤延續，所以週六凌晨是有開的(週五夜盤)，而週一凌晨沒有(週日不開盤)。
#
# 尚未處理(之後要補)：
#   - 國定假日/颱風假(期交所有正式的交易日曆，接上去才會準)
#   - 夜盤的「歸屬交易日」：例如 8/18 夜盤跨到 8/19 凌晨，這段時間的資料
#     在期交所是歸在 8/19 這個交易日底下的，寫進資料庫時要注意別記成8/18

def detect_session(now: Optional[datetime] = None) -> Literal["day", "night"]:
    """依現在時間判斷該抓日盤還是夜盤資料"""
    now = now or datetime.now()
    t = now.time()
    if dtime(8, 45) <= t <= dtime(13, 45):
        return "day"
    if t >= dtime(15, 0) or t <= dtime(5, 0):
        return "night"
    # 13:45~15:00 這段休息時間、或05:00~08:45，兩盤都沒開。
    # 這時抓到的會是上一個盤的最後狀態，用日盤資料比較符合直覺。
    return "day"


def is_market_open(now: Optional[datetime] = None) -> bool:
    """
    現在是不是真的在交易時段內(用來提醒使用者拿到的可能是收盤後的靜態資料)。

    只看時鐘不看日期的話，整個週末都會回報「交易中」，畫面上就不會出現
    「⚠ 非交易時段」，週五的收盤價會被當成即時報價呈現 —— 而那是畫面最醒目的位置。

    星期的判斷要分三段，不能只寫 weekday() <= 4：
      08:45~13:45  日盤        → 週一~週五
      15:00~23:59  夜盤前半段  → 週一~週五(開盤日)
      00:00~05:00  夜盤後半段  → 是「前一天」開的盤，所以是週二~週六
                                 (週六凌晨 = 週五夜盤，有開；週一凌晨 = 週日，沒開)

    還沒處理國定假日/颱風假，那要接期交所的交易日曆才會準。
    """
    now = now or datetime.now()
    t = now.time()
    wd = now.weekday()                      # 0=週一 ... 6=週日

    if dtime(8, 45) <= t <= dtime(13, 45):
        return wd <= 4
    if t >= dtime(15, 0):
        return wd <= 4
    if t <= dtime(5, 0):
        return 1 <= wd <= 5                 # 開盤日是前一天，所以往後挪一天
    return False                            # 13:45~15:00、05:00~08:45 兩盤都沒開


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _format_age(age: Optional[int]) -> str:
    """把報價年齡秒數印成好讀的形式"""
    if age is None:
        return "-"
    if age < 60:
        return f"{age}秒"
    if age < 3600:
        return f"{age // 60}分"
    return f"{age / 3600:.1f}時"


def run(
    right_type: Literal["C", "P"] = "C",
    expire_month: Optional[str] = None,
    session: Optional[Literal["day", "night"]] = None,
    strike_window: int = 10,
    min_volume: int = 0,
    max_age_sec: Optional[int] = None,
    use_skew_curve: bool = True,
    skew_k_range: Optional[float] = 0.15,
    db: Optional[OptionDB] = None,
    write_db: bool = False,
) -> None:
    session = session or detect_session()

    print("=" * 78)
    print(f"台指選擇權 貴賤判斷 — 真實即時資料  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    print("=" * 78)
    print(f"盤別: {'日盤' if session == 'day' else '夜盤'}"
          f"{'' if is_market_open() else '  ⚠ 目前非交易時段，抓到的是上一個交易時段的最後報價'}")

    # 1) 決定要看哪個到期別
    months = list_contract_months("TXO", session=session)
    if not months:
        print("查不到任何掛牌合約，可能是期交所維護中。")
        return

    # 沒指定就抓最近到期的月選(月選才有5日IV歷史可用)
    target = pick_contract(months, expire_month, None if expire_month else "month")
    if target is None:
        if expire_month:
            print(f"找不到到期別 {expire_month}。目前掛牌中的有:")
        else:
            print("查不到月選合約。目前掛牌中的有:")
        for m in sorted(months, key=lambda x: x.expiry_date):
            print(f"   {m.code:<10} {m.kind_label:<14} 到期 {m.expiry_date}")
        return

    # 2) 台指期點數 (Black-76 的 F)
    try:
        F = fetch_underlying_futures_price(session=session)
    except TaifexApiError as e:
        print(f"抓台指期報價失敗: {e}")
        return

    print(f"到期別: {target.code} ({target.kind_label})  "
          f"到期日 {target.expiry_date} {target.weekday_zh}  "
          f"剩餘 {(target.expiry_date - date.today()).days} 天")
    print(f"台指期點數 F = {F}")
    print(f"買賣權: {'買權 Call' if right_type == 'C' else '賣權 Put'}\n")

    # 3~6) 抓報價鏈、反推IV、挑基準、判斷 —— 全部在 pricing_service 裡
    try:
        res = analyze_chain(
            target, right_type, session, underlying_price=F,
            min_volume=min_volume, max_age_sec=max_age_sec,
            use_skew_curve=use_skew_curve, skew_k_range=skew_k_range, db=db,
        )
    except TaifexApiError as e:
        print(f"抓選擇權報價失敗: {e}")
        return
    except NoQuoteDataError as e:
        print(e)
        return

    if res.dropped_volume or res.dropped_stale:
        parts = []
        if res.dropped_volume:
            parts.append(f"成交量<{min_volume} 濾掉{res.dropped_volume}檔")
        if res.dropped_stale:
            parts.append(f"報價超過{max_age_sec}秒 濾掉{res.dropped_stale}檔")
        print(f"資料過濾: {' / '.join(parts)}  "
              f"(原始{res.total_before_filter}檔 → 剩{len(res.evals) + res.n_failed_iv}檔)")

    curve = res.curve
    if res.curve_rejected:
        print("⚠ 偏斜曲線配適品質不佳，退回用ATM單點基準")

    # 用曲線時，品質說明由曲線那行負責(配適點數/剔除數)，
    # 這裡的ATM池說明只有在真的用ATM單點時才有意義
    print(f"基準IV來源: {res.baseline_source}"
          f"{'' if curve is not None else ' ' + res.baseline_quality_note}")
    if curve is not None:
        print(f"曲線: ATM IV={curve.atm_iv():.4f}  偏斜斜率={curve.skew_slope:+.4f}  "
              f"配適{curve.n_used}點 RMSE={curve.rmse:.4f}"
              + (f"  (剔除{curve.n_dropped}個離群點)" if curve.n_dropped else ""))
        print(f"      偏斜斜率為負代表越價外的IV越低，這是台股指數選擇權的常態")
    else:
        print(f"ATM 基準IV = {res.baseline_iv_atm:.4f}")
    if res.n_failed_iv:
        print(f"(有 {res.n_failed_iv} 檔無法反推IV已略過，"
              f"多半是完全沒成交、報價僵滯的深價內/價外合約)")
    print()

    # 7) 寫進資料庫(option_live_quote)，前端就是讀這張表。
    #    注意寫的是「全鏈」不是畫面上那幾檔 —— 畫面只印ATM附近避免洗版，
    #    但資料庫要有全部，前端才能自己篩。
    if write_db and db is not None:
        try:
            rows_to_save = [
                LiveQuoteRow(
                    expiry_type=target.expiry_type,
                    expiry_date=target.expiry_date,
                    strike_price=e.strike,
                    right_type=right_type,
                    session=session,
                    underlying_price=F,
                    market_price=e.market_price,
                    fair_price=e.fair_price,
                    implied_vol=e.implied_vol,
                    baseline_iv=e.baseline_iv,
                    deviation_pct=e.deviation_pct,
                    status=e.status,
                    volume=e.volume,
                    quote_time=e.quote_time,
                    price_source=e.price_source,
                    reliable=e.reliable,
                )
                for e in res.evals
            ]
            n_saved = db.save_live_quotes(rows_to_save)
            print(f"已寫入資料庫 option_live_quote: {n_saved} 筆\n")
        except Exception as e:
            print(f"⚠ 寫入資料庫失敗: {type(e).__name__}: {e}\n")

    # 8) 只顯示ATM上下各strike_window檔，不然幾百檔洗版
    shown = res.window(strike_window)
    counts = res.reliable_counts   # 只算可信的，跟表格裡上色的那幾檔對得起來

    print(f"{'履約價':>9} {'市場價':>9} {'來源':>5} {'即時IV':>8} {'基準IV':>8} "
          f"{'合理價':>9} {'偏差':>8} {'狀態':>6} {'成交量':>7} {'報價年齡':>8}")
    print("-" * 88)

    stale_shown = 0
    for e in shown:
        label = {"expensive": "偏貴", "fair": "合理", "cheap": "便宜"}[e.status]
        fair_disp = f"{e.fair_price}" if e.status != "fair" else "-"
        # 合理價太小、百分比失去意義的合約標上"?"，不要讓人誤以為那是可用的訊號
        dev_disp = f"{e.deviation_pct:>7.1f}%" + ("?" if not e.reliable else "")

        # 報價超過10分鐘就標星號提醒：這種價格算出來的IV參考價值低
        age_disp = _format_age(e.age_sec)
        if e.stale:
            age_disp += "*"
            stale_shown += 1

        mark = " ←ATM" if e.strike == res.atm_strike else ""

        print(f"{e.strike:>9.0f} {e.market_price:>9.2f} {e.price_source:>5} "
              f"{e.implied_vol:>8.4f} {e.baseline_iv:>8.4f} {fair_disp:>9} {dev_disp:>8} "
              f"{label:>6} {e.volume:>7} {age_disp:>8}{mark}")

    print("-" * 88)
    print(f"顯示 {len(shown)} 檔(全鏈共 {len(res.evals)} 檔已判斷) | "
          f"偏貴 {counts['expensive']} / 合理 {counts['fair']} / 便宜 {counts['cheap']}"
          + (f"  (另有 {res.n_unreliable} 檔不可信，未計入)" if res.n_unreliable else ""))
    print("\n※ 價格來源 last=實際成交價, mid=買賣中價(該檔今日尚無成交)")
    if stale_shown:
        print(f"※ 有 {stale_shown} 檔報價超過{STALE_AGE // 60}分鐘(標*)，"
              f"那個價格可能是好幾小時前成交的，")
        print(f"   期間台指期已經跑掉一段，算出的IV參考價值低。可加 --max-age 300 過濾。")
    if res.n_unreliable:
        print(f"※ 有 {res.n_unreliable} 檔標記為不可信(標?)，資料庫裡 reliable=0，"
              f"前端可用 only_reliable 濾掉。原因有兩種：")
        if res.n_out_of_range:
            if res.curve is not None:
                print(f"   - {res.n_out_of_range} 檔落在偏斜曲線的配適範圍外(基準是外插來的)。"
                      f"可用 --skew-range 放寬範圍")
            else:
                print(f"   - {res.n_out_of_range} 檔離價平太遠，而這次沒有曲線可用、"
                      f"基準只有ATM單點那一個IV，對深價外不成立。"
                      f"可用 --skew-range 放寬範圍")
        n_tiny = res.n_unreliable - res.n_out_of_range
        if n_tiny > 0:
            print(f"   - {n_tiny} 檔的合理價過小(深度價外)，百分比偏差失去意義")
    print("※ 判斷僅供參考，不構成投資建議；沒成交量的報價可靠度低")


def main() -> None:
    # Windows的終端機預設是cp950(Big5)，印到 ⚠ 這種不在Big5裡的字會直接
    # UnicodeEncodeError 讓程式掛掉(表格印到一半中斷)。印不出來的字用?代替就好。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="台指選擇權貴賤判斷 — 真實即時資料版")
    p.add_argument("--right", choices=["C", "P"], default="C", help="買權C或賣權P (預設C)")
    p.add_argument("--expiry", default=None, help="到期別代碼，例如 202609 或 202608W4")
    p.add_argument("--session", choices=["day", "night"], default=None, help="強制指定盤別")
    p.add_argument("--window", type=int, default=10, help="ATM上下各顯示幾檔 (預設10)")
    p.add_argument("--min-volume", type=int, default=0, help="只看成交量>=N的合約")
    p.add_argument("--max-age", type=int, default=None,
                   help="只看報價在N秒內的合約(例如300=5分鐘)，用來濾掉陳舊成交價")
    p.add_argument("--baseline", choices=["curve", "atm"], default="curve",
                   help="基準IV算法: curve=偏斜曲線(預設,已消除skew) / atm=ATM單點(舊做法)")
    p.add_argument("--skew-range", type=float, default=0.15,
                   help="配曲線時只用 |ln(K/F)|<=N 的履約價 (預設0.15，約價平±15%%)")
    p.add_argument("--list", action="store_true", help="只列出目前掛牌的到期別")
    # 資料庫相關
    p.add_argument("--db", default=None,
                   help="連資料庫。給 'env' 讀環境變數(正式用MySQL)，"
                        "或給一個檔案路徑則用SQLite(本機測試用)")
    p.add_argument("--write-db", action="store_true",
                   help="把判斷結果寫進 option_live_quote (需搭配 --db)")
    p.add_argument("--init-db", action="store_true",
                   help="建立資料表後結束 (需搭配 --db)")
    args = p.parse_args()

    # 建立資料庫連線
    db = None
    if args.db:
        try:
            db = (OptionDB.connect_from_env() if args.db == "env"
                  else OptionDB.connect_sqlite(args.db))
        except Exception as e:
            print(f"資料庫連線失敗: {type(e).__name__}: {e}")
            print("MySQL請確認 TAIFEX_DB_* 環境變數設定正確，或改用SQLite路徑做本機測試。")
            return

    if args.init_db:
        if db is None:
            print("--init-db 需要搭配 --db 指定資料庫")
            return
        db.create_tables()
        print(f"資料表建立完成 (後端: {db.dialect})")
        print(f"  option_iv_history : {db.count('option_iv_history')} 筆")
        print(f"  option_live_quote : {db.count('option_live_quote')} 筆")
        db.close()
        return

    if args.write_db and db is None:
        print("--write-db 需要搭配 --db 指定資料庫")
        return

    if args.list:
        session = args.session or detect_session()
        try:
            months = list_contract_months("TXO", session=session)
        except Exception as e:
            print(f"抓掛牌清單失敗: {type(e).__name__}: {e}")
            if db is not None:
                db.close()
            return
        print(f"目前掛牌中的台指選擇權到期別 ({'日盤' if session == 'day' else '夜盤'}):\n")
        # 週選有兩組：W系列=週三結算、F系列=週五結算，會交錯掛牌，
        # 所以類型欄位一定要把週別代碼跟星期印出來，不能只寫「週選」
        print(f"{'代碼':<12} {'類型':<16} {'到期日':<12} {'星期':<6} {'剩餘天數':>8}")
        print("-" * 58)
        for m in sorted(months, key=lambda x: x.expiry_date):
            days = (m.expiry_date - date.today()).days
            print(f"{m.code:<12} {m.kind_label:<16} "
                  f"{str(m.expiry_date):<12} {m.weekday_zh:<6} {days:>8}")
        print("\n提示: W=週三結算、F=週五結算；當月第三個週三是月選，所以不會有W3。")
        if db is not None:
            db.close()   # 這個分支是提早return，不會走到下面的 finally
        return

    try:
        run(right_type=args.right, expire_month=args.expiry, session=args.session,
            strike_window=args.window, min_volume=args.min_volume,
            max_age_sec=args.max_age, use_skew_curve=(args.baseline == "curve"),
            skew_k_range=args.skew_range, db=db, write_db=args.write_db)
    except TaifexApiError as e:
        print(f"期交所API錯誤: {e}")
    except Exception as e:  # 網路斷線之類的，給個好讀的訊息而不是一長串traceback
        print(f"執行失敗: {type(e).__name__}: {e}")
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
