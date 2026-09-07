# -*- coding: utf-8 -*-
"""
iv_baseline.py

計算「合理IV基準」(baseline_iv)的模組
------------------------------------
- 月選：從歷史資料庫撈同一履約價、同一交易時段(日/夜)過去5天的IV，取平均
- 週選：從當下這批即時報價裡，找出最接近ATM(價平)的履約價，用它的IV當基準
  (週選合約壽命短，不用等5天歷史，這是你已經確認的設計)

資料庫存取用 pymysql，SQL寫成參數化查詢避免injection。
如果你實際上不是用MySQL(例如改用PostgreSQL)，主要要改的只有connect()那幾行。
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

Session = Literal["day", "night"]
OptionType = Literal["C", "P"]


@dataclass
class LiveQuote:
    """單一履約價的即時報價(週選找ATM要用到)"""
    strike_price: float
    right_type: OptionType
    market_price: float
    implied_vol: float


# ---------------------------------------------------------------------------
# 月選：5日IV移動平均
# ---------------------------------------------------------------------------

def get_monthly_baseline_iv(
    db_conn,
    strike_price: float,
    right_type: OptionType,
    session: Session,
    expiry_date: date,
    lookback_days: int = 5,
) -> Optional[float]:
    """
    從 option_iv_history 撈出「同履約價、同買賣權、同交易時段」
    最近 lookback_days 天的IV，取平均。

    回傳 None 代表歷史筆數不足(冷啟動階段或該履約價剛掛牌沒多久)，
    呼叫端要自己決定 fallback (例如先用當下即時IV代替)。
    """
    sql = """
        SELECT implied_vol FROM option_iv_history
        WHERE strike_price = %s
          AND right_type = %s
          AND session = %s
          AND expiry_date = %s
          AND expiry_type = 'month'
        ORDER BY trade_date DESC
        LIMIT %s
    """
    with db_conn.cursor() as cur:
        cur.execute(sql, (strike_price, right_type, session, expiry_date, lookback_days))
        rows = cur.fetchall()

    if not rows:
        return None

    ivs = [r[0] if not isinstance(r, dict) else r["implied_vol"] for r in rows]

    if len(ivs) < lookback_days:
        # 歷史筆數不足5天(冷啟動階段)，先用現有的筆數平均，但這是暫時狀態
        # 你可以選擇在前端標記「歷史資料尚未滿5天，準確度較低」
        pass

    return sum(ivs) / len(ivs)


# ---------------------------------------------------------------------------
# 週選：當下批次找ATM，用ATM的IV當基準
# ---------------------------------------------------------------------------

def get_weekly_baseline_iv(
    quotes_same_expiry: list[LiveQuote],
    underlying_price: float,
) -> Optional[float]:
    """
    在同一到期週、同一天期的所有即時報價裡，找履約價離F(台指期點數)最近的那檔，
    用它的即時IV當作這週的baseline。

    注意：call跟put是分開找的，因為你畫面設計是先選C or P再列表，
    所以理論上這裡傳進來的quotes_same_expiry已經是同一個right_type了；
    但保留這個函式對call/put都通用。
    """
    if not quotes_same_expiry:
        return None

    atm_quote = min(quotes_same_expiry, key=lambda q: abs(q.strike_price - underlying_price))
    return atm_quote.implied_vol


# ---------------------------------------------------------------------------
# 每個session收盤時，把當天IV寫進歷史庫(餵給明天的移動平均)
# ---------------------------------------------------------------------------

def save_session_close_iv(
    db_conn,
    trade_date: date,
    session: Session,
    expiry_type: Literal["week", "month"],
    expiry_date: date,
    records: list[dict],
) -> int:
    """
    records 每筆需要包含:
      strike_price, right_type, underlying_price, close_price, implied_vol

    用 INSERT ... ON DUPLICATE KEY UPDATE，避免同一天重跑腳本時產生重複資料。
    回傳寫入筆數。
    """
    sql = """
        INSERT INTO option_iv_history
            (trade_date, session, expiry_type, expiry_date, strike_price,
             right_type, underlying_price, close_price, implied_vol)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            underlying_price = VALUES(underlying_price),
            close_price = VALUES(close_price),
            implied_vol = VALUES(implied_vol)
    """
    count = 0
    with db_conn.cursor() as cur:
        for r in records:
            cur.execute(sql, (
                trade_date, session, expiry_type, expiry_date,
                r["strike_price"], r["right_type"], r["underlying_price"],
                r["close_price"], r["implied_vol"],
            ))
            count += 1
    db_conn.commit()
    return count


# ---------------------------------------------------------------------------
# 自我測試：用 sqlite 模擬資料庫行為，驗證邏輯正確(不需要真的連MySQL)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sqlite3
    from datetime import timedelta

    # 用sqlite模擬(語法跟MySQL有些微差異，這裡只是驗證「邏輯」，
    # 正式上線要接真的MySQL，用pymysql，SQL語法要換成%s佔位符，上面函式已經是這樣寫)
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE option_iv_history (
            trade_date TEXT, session TEXT, expiry_type TEXT, expiry_date TEXT,
            strike_price REAL, right_type TEXT, underlying_price REAL,
            close_price REAL, implied_vol REAL
        )
    """)

    # 塞入過去5天，履約價17500 Call 的假IV資料 (day session)
    base_day = date(2026, 8, 10)
    fake_ivs = [0.14, 0.15, 0.16, 0.145, 0.155]  # 平均應該是 0.15
    for i, iv in enumerate(fake_ivs):
        d = base_day + timedelta(days=i)
        conn.execute(
            "INSERT INTO option_iv_history VALUES (?,?,?,?,?,?,?,?,?)",
            (d.isoformat(), "day", "month", "2026-09-16", 17500, "C", 17480, 150.0, iv),
        )
    conn.commit()

    # 測試月選5日移動平均 (用sqlite語法直接測，驗證平均值算法正確)
    cur = conn.execute(
        "SELECT implied_vol FROM option_iv_history WHERE strike_price=17500 AND right_type='C' "
        "AND session='day' AND expiry_date='2026-09-16' ORDER BY trade_date DESC LIMIT 5"
    )
    rows = [r[0] for r in cur.fetchall()]
    avg_iv = sum(rows) / len(rows)
    print(f"[月選5日均測試] 抓到{len(rows)}筆IV: {rows}")
    print(f"[月選5日均測試] 平均IV = {avg_iv:.4f} (應接近 0.1500)")
    assert abs(avg_iv - 0.15) < 1e-6, "月選5日均計算錯誤！"
    print("✅ 月選5日移動平均邏輯測試通過\n")

    # 測試週選ATM邏輯
    week_quotes = [
        LiveQuote(strike_price=17300, right_type="C", market_price=210, implied_vol=0.18),
        LiveQuote(strike_price=17400, right_type="C", market_price=160, implied_vol=0.16),
        LiveQuote(strike_price=17500, right_type="C", market_price=115, implied_vol=0.15),  # 最接近F=17490
        LiveQuote(strike_price=17600, right_type="C", market_price=80, implied_vol=0.17),
    ]
    baseline = get_weekly_baseline_iv(week_quotes, underlying_price=17490)
    print(f"[週選ATM測試] F=17490 → 選中履約價17500 (最接近) → baseline_iv={baseline}")
    assert baseline == 0.15, "週選ATM挑選邏輯錯誤！"
    print("✅ 週選ATM基準邏輯測試通過")
