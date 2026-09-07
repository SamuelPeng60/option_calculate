# -*- coding: utf-8 -*-
"""
run_demo_pipeline.py

端對端demo：用模擬資料把整條pipeline跑一次
------------------------------------
流程完全比照正式設計：
  模擬抓即時報價 → 對每檔算即時IV → 撈baseline_iv(月選5日均/週選ATM)
  → 算合理價 → 判斷貴/合理/便宜 → 印出畫面預想的表格

之後把 mock_fetch_underlying_futures_price / mock_fetch_live_quotes
換成 taifex_fetch.py 裡真正接上API的版本，其他程式碼完全不用改，
這就是先用模擬資料的意義：先把「線路」接好。
"""

import sqlite3
from datetime import date, timedelta

from black76_iv import implied_vol, evaluate_option
from iv_baseline import get_weekly_baseline_iv, LiveQuote
from mock_live_quotes import (
    mock_fetch_underlying_futures_price,
    mock_fetch_live_quotes,
    mock_iv_history,
)

R = 0.015  # 無風險利率假設


def demo_monthly():
    print("=" * 70)
    print("【月選 Demo】9月 Call，用5日IV移動平均當baseline")
    print("=" * 70)

    expiry_date = date.today() + timedelta(days=30)
    F = mock_fetch_underlying_futures_price()
    print(f"目前台指期點數(模擬): {F}\n")

    # 故意讓 17600 偏貴、17300 偏便宜，其他隨機小雜訊，用來驗證畫面顯示邏輯
    quotes = mock_fetch_live_quotes(
        expiry_type="month", expiry_date=expiry_date, right_type="C",
        underlying_price=F, true_iv=0.15,
        force_mispriced_strikes={17600: 0.09, 17300: -0.08},
    )

    # 建一個sqlite模擬DB，幫每個履約價塞5天假的IV歷史(月選baseline要用)
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE option_iv_history (
        trade_date TEXT, session TEXT, expiry_type TEXT, expiry_date TEXT,
        strike_price REAL, right_type TEXT, underlying_price REAL,
        close_price REAL, implied_vol REAL)""")
    for q in quotes:
        for i, iv in enumerate(mock_iv_history(q.strike_price, days=5, center_iv=0.15)):
            d = date.today() - timedelta(days=5 - i)
            conn.execute(
                "INSERT INTO option_iv_history VALUES (?,?,?,?,?,?,?,?,?)",
                (d.isoformat(), "day", "month", expiry_date.isoformat(),
                 q.strike_price, "C", F, 100.0, iv),
            )
    conn.commit()

    print(f"{'履約價':>8} {'市場價':>8} {'即時IV':>8} {'基準IV':>8} {'合理價':>8} {'偏差':>8} {'狀態':>8}")
    for q in quotes:
        iv_res = implied_vol(q.last_price, F, q.strike_price, (expiry_date - date.today()).days / 365, R, "C")
        if not iv_res.converged:
            continue

        cur = conn.execute(
            "SELECT implied_vol FROM option_iv_history WHERE strike_price=? AND right_type='C' "
            "ORDER BY trade_date DESC LIMIT 5", (q.strike_price,)
        )
        rows = [r[0] for r in cur.fetchall()]
        baseline_iv = sum(rows) / len(rows)

        T = (expiry_date - date.today()).days / 365
        verdict = evaluate_option(q.last_price, F, q.strike_price, T, R, "C", baseline_iv, threshold_pct=0.05)

        status_label = {"expensive": "偏貴", "fair": "合理", "cheap": "便宜"}[verdict.status]
        fair_display = f"{verdict.fair_price}" if verdict.status != "fair" else "-"
        print(f"{q.strike_price:>8.0f} {q.last_price:>8.1f} {iv_res.iv:>8.4f} "
              f"{baseline_iv:>8.4f} {fair_display:>8} {verdict.deviation_pct:>7.1f}% {status_label:>8}")

    print()


def demo_weekly():
    print("=" * 70)
    print("【週選 Demo】本週 Put，用當週ATM IV當baseline")
    print("=" * 70)

    expiry_date = date.today() + timedelta(days=4)
    F = mock_fetch_underlying_futures_price()
    print(f"目前台指期點數(模擬): {F}\n")

    quotes = mock_fetch_live_quotes(
        expiry_type="week", expiry_date=expiry_date, right_type="P",
        underlying_price=F, true_iv=0.18,
        force_mispriced_strikes={17400: 0.07},
    )

    T = (expiry_date - date.today()).days / 365
    live_quote_objs = []
    iv_map = {}
    for q in quotes:
        iv_res = implied_vol(q.last_price, F, q.strike_price, T, R, "P")
        if iv_res.converged:
            iv_map[q.strike_price] = iv_res.iv
            live_quote_objs.append(LiveQuote(q.strike_price, "P", q.last_price, iv_res.iv))

    baseline_iv = get_weekly_baseline_iv(live_quote_objs, underlying_price=F)
    print(f"當週ATM IV(baseline) = {baseline_iv:.4f}\n")

    print(f"{'履約價':>8} {'市場價':>8} {'即時IV':>8} {'合理價':>8} {'偏差':>8} {'狀態':>8}")
    for q in quotes:
        if q.strike_price not in iv_map:
            continue
        verdict = evaluate_option(q.last_price, F, q.strike_price, T, R, "P", baseline_iv, threshold_pct=0.05)
        status_label = {"expensive": "偏貴", "fair": "合理", "cheap": "便宜"}[verdict.status]
        fair_display = f"{verdict.fair_price}" if verdict.status != "fair" else "-"
        print(f"{q.strike_price:>8.0f} {q.last_price:>8.1f} {iv_map[q.strike_price]:>8.4f} "
              f"{fair_display:>8} {verdict.deviation_pct:>7.1f}% {status_label:>8}")


if __name__ == "__main__":
    demo_monthly()
    demo_weekly()
